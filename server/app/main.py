"""云控服务端：设备连接、命令下发、规则管理、网页控制台。

启动：
    python -m app.main --host 0.0.0.0 --port 8787
环境变量：
    CLOUDCTL_AGENT_TOKEN    agent 连接令牌
    CLOUDCTL_CONSOLE_TOKEN  控制台登录令牌
    CLOUDCTL_DB             数据库路径（默认 ./cloudctl.db）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from .store import Store

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

AGENT_TOKEN = os.environ.get("CLOUDCTL_AGENT_TOKEN", "change-me-agent")
CONSOLE_TOKEN = os.environ.get("CLOUDCTL_CONSOLE_TOKEN", "change-me-console")
DB_PATH = Path(os.environ.get("CLOUDCTL_DB", str(BASE_DIR.parent / "cloudctl.db")))

store = Store(DB_PATH)
app = FastAPI(title="cloudctl server", version="1.0.0")


# ------------------------------------------------------------------ 连接管理
class Hub:
    def __init__(self) -> None:
        self.agents: dict[str, WebSocket] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.consoles: set[WebSocket] = set()
        self.watching: dict[WebSocket, str] = {}

    async def register_agent(self, device_id: str, ws: WebSocket) -> None:
        old = self.agents.get(device_id)
        if old is not None:
            try:
                await old.close(code=4000, reason="replaced")
            except Exception:
                pass
        self.agents[device_id] = ws
        await self.broadcast_consoles({"type": "device.online", "device_id": device_id, "ts": int(time.time())})

    def unregister_agent(self, device_id: str, ws: WebSocket) -> None:
        if self.agents.get(device_id) is ws:
            self.agents.pop(device_id, None)

    async def send_to_agent(self, device_id: str, msg: dict) -> bool:
        ws = self.agents.get(device_id)
        if ws is None:
            return False
        try:
            await ws.send_text(json.dumps(msg, ensure_ascii=False))
            return True
        except Exception:
            self.agents.pop(device_id, None)
            return False

    async def request(self, device_id: str, op: str, args: dict, timeout: float = 120.0) -> dict:
        cid = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[cid] = fut
        ok = await self.send_to_agent(device_id, {"type": "cmd", "id": cid, "op": op, "args": args})
        if not ok:
            self.pending.pop(cid, None)
            raise HTTPException(503, "设备未连接")
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise HTTPException(504, "设备响应超时") from None
        finally:
            self.pending.pop(cid, None)

    def resolve(self, cid: str, payload: dict) -> None:
        fut = self.pending.get(cid)
        if fut and not fut.done():
            fut.set_result(payload)

    async def broadcast_consoles(self, msg: dict) -> None:
        dead = []
        text = json.dumps(msg, ensure_ascii=False)
        for ws in list(self.consoles):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.consoles.discard(ws)
            self.watching.pop(ws, None)

    async def relay_frames(self, device_id: str, msg: dict) -> None:
        text = json.dumps(msg, ensure_ascii=False)
        dead = []
        for ws, watch in list(self.watching.items()):
            if watch != device_id:
                continue
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.consoles.discard(ws)
            self.watching.pop(ws, None)


hub = Hub()


# ------------------------------------------------------------------ 鉴权
def _token_from(req: Request) -> str:
    return (
        req.headers.get("x-token")
        or req.query_params.get("token")
        or (req.cookies.get("cloudctl_token") or "")
    )


async def console_guard(req: Request) -> None:
    if _token_from(req) != CONSOLE_TOKEN:
        raise HTTPException(401, "控制台令牌无效")


# ------------------------------------------------------------------ 接口
@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "agents": list(hub.agents.keys()), "ts": int(time.time())}


@app.post("/api/login")
async def login(req: Request) -> JSONResponse:
    body = await req.json()
    if (body or {}).get("token") != CONSOLE_TOKEN:
        raise HTTPException(401, "令牌无效")
    resp = JSONResponse({"ok": True})
    resp.set_cookie("cloudctl_token", CONSOLE_TOKEN, httponly=True, samesite="lax", max_age=7 * 86400)
    return resp


@app.get("/api/devices", dependencies=[Depends(console_guard)])
async def api_devices() -> dict:
    devices = store.list_devices()
    for d in devices:
        d["online"] = d["device_id"] in hub.agents
    return {"devices": devices, "count": len(devices), "online": len(hub.agents)}


@app.get("/api/devices/{device_id}", dependencies=[Depends(console_guard)])
async def api_device(device_id: str) -> dict:
    d = store.get_device(device_id)
    if not d:
        raise HTTPException(404, "设备不存在")
    d["online"] = device_id in hub.agents
    d["rules"] = store.rules_for_device(device_id, d.get("grp") or "default")
    return d


@app.post("/api/devices/{device_id}/cmd", dependencies=[Depends(console_guard)])
async def api_cmd(device_id: str, req: Request) -> dict:
    body = await req.json()
    op = (body or {}).get("op")
    args = (body or {}).get("args") or {}
    timeout = float((body or {}).get("timeout") or 120)
    if not op:
        raise HTTPException(400, "缺少 op")
    try:
        res = await hub.request(device_id, op, args, timeout=timeout)
    except HTTPException as e:
        store.audit(device_id, op, args, False, {"error": e.detail})
        raise
    store.audit(device_id, op, args, bool(res.get("ok")), res.get("data") if res.get("ok") else res.get("err"))
    return res


@app.post("/api/devices/{device_id}/rules", dependencies=[Depends(console_guard)])
async def api_set_rules(device_id: str, req: Request) -> dict:
    body = await req.json()
    scope = (body or {}).get("scope") or "device"
    value = device_id if scope == "device" else ((body or {}).get("value") or "all")
    ver = store.set_rules(scope, value, (body or {}).get("rules") or {})
    payload = store.get_rules(scope, value) or {}
    targets = [device_id] if scope == "device" else [d["device_id"] for d in store.list_devices()]
    pushed = 0
    for dev in targets:
        if await hub.send_to_agent(dev, {"type": "rules", "version": ver, "rules": payload}):
            pushed += 1
    return {"scope": scope, "value": value, "version": ver, "pushed": pushed}


@app.get("/api/rules", dependencies=[Depends(console_guard)])
async def api_rules() -> dict:
    return {"rules": store.list_rules()}


@app.get("/api/audit", dependencies=[Depends(console_guard)])
async def api_audit(device_id: str = "", limit: int = 100) -> dict:
    return {"audit": store.list_audit(device_id, limit)}


# ------------------------------------------------------------------ 设备通道
@app.websocket("/ws/agent")
async def ws_agent(ws: WebSocket) -> None:
    await ws.accept()
    qp = ws.query_params
    if qp.get("token") != AGENT_TOKEN:
        await ws.close(code=4401, reason="bad token")
        return
    device_id = qp.get("device_id") or "unknown"
    info: dict[str, Any] = {}
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype = msg.get("type")
            if mtype == "hello":
                info = msg
                store.upsert_device(
                    device_id, msg.get("name") or device_id, msg.get("group") or "default",
                    msg.get("os") or "", msg.get("ver") or "",
                    ws.client.host if ws.client else "", msg,
                )
                await hub.register_agent(device_id, ws)
                rules = store.rules_for_device(device_id, msg.get("group") or "default")
                if rules:
                    await ws.send_text(json.dumps(
                        {"type": "rules", "version": rules.get("version", 0), "rules": rules}, ensure_ascii=False
                    ))
                continue
            if mtype == "result":
                hub.resolve(msg.get("id") or "", msg)
                store.touch(device_id)
                await hub.broadcast_consoles({"type": "result", "device_id": device_id, "result": msg})
                continue
            if mtype == "frame" or mtype == "stream.end":
                await hub.relay_frames(device_id, msg)
                continue
            if mtype == "pong":
                store.touch(device_id)
                continue
            if mtype == "event":
                store.touch(device_id)
                await hub.broadcast_consoles({"type": "event", "device_id": device_id, "event": msg})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        hub.unregister_agent(device_id, ws)
        await hub.broadcast_consoles({"type": "device.offline", "device_id": device_id, "ts": int(time.time())})


# ------------------------------------------------------------------ 控制台通道
@app.websocket("/ws/console")
async def ws_console(ws: WebSocket) -> None:
    await ws.accept()
    if ws.query_params.get("token") != CONSOLE_TOKEN:
        await ws.close(code=4401, reason="bad token")
        return
    hub.consoles.add(ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype = msg.get("type")
            device_id = msg.get("device_id") or ""
            if mtype == "watch":
                hub.watching[ws] = device_id
                await ws.send_text(json.dumps({"type": "watch.ok", "device_id": device_id}))
            elif mtype == "desktop.start":
                hub.watching[ws] = device_id
                await hub.send_to_agent(device_id, {
                    "type": "desktop.start", "id": uuid.uuid4().hex, "args": msg.get("args") or {}
                })
            elif mtype == "desktop.stop":
                hub.watching.pop(ws, None)
                await hub.send_to_agent(device_id, {"type": "desktop.stop", "id": uuid.uuid4().hex})
            elif mtype == "desktop.input":
                await hub.send_to_agent(device_id, {"type": "desktop.input", "events": msg.get("events") or []})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        hub.consoles.discard(ws)
        hub.watching.pop(ws, None)


# ------------------------------------------------------------------ 控制台页面
@app.get("/")
async def index() -> Any:
    f = STATIC_DIR / "index.html"
    if not f.exists():
        return JSONResponse({"error": "缺少控制台页面"}, status_code=500)
    return FileResponse(f)


@app.get("/api/config.json")
async def client_config() -> dict:
    return {"agenthint": "agents connect to wss://<host>/ws/agent"}


def main() -> None:
    ap = argparse.ArgumentParser("cloudctl-server")
    ap.add_argument("--host", default=os.environ.get("CLOUDCTL_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("CLOUDCTL_PORT", "8787")))
    args = ap.parse_args()
    print(f"cloudctl server -> http://{args.host}:{args.port}  db={DB_PATH}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", ws_max_size=48 * 1024 * 1024)


if __name__ == "__main__":
    main()
