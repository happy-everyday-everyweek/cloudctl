"""控制通道：WebSocket 主通道 + GitHub 仓库文件备用通道。

主通道：agent 主动拨出 wss 长连接，服务端通过该连接下发命令与规则。
备用通道：服务端不可达时，agent 轮询仓库里的规则文件与命令文件，
结果写回 outbox 目录。两条通道共用同一套消息格式与路由。
"""
from __future__ import annotations

import asyncio
import base64
import json
import platform
import time
from typing import Any, Awaitable, Callable

from .config import Config
from .util import read_json, write_json

SEND_TIMEOUT = 20
MAX_WS_SIZE = 48 * 1024 * 1024


def _requests():
    try:
        import requests  # type: ignore

        return requests
    except Exception:
        return None


class ControlChannel:
    def __init__(
        self,
        cfg: Config,
        on_message: Callable[[dict], Awaitable[dict | None]],
        on_rules: Callable[[dict, int], Awaitable[None]],
        log,
    ) -> None:
        self.cfg = cfg
        self.on_message = on_message
        self.on_rules = on_rules
        self.log = log
        self.ws = None
        self._outbox: asyncio.Queue[dict] = asyncio.Queue(maxsize=2000)
        self._stop = asyncio.Event()
        self.connected = False
        self.last_error = ""

    # ------------------------------------------------------------ 发送
    async def send(self, obj: dict) -> None:
        try:
            self._outbox.put_nowait(obj)
        except asyncio.QueueFull:
            self.log.warning("发送队列已满，丢弃消息 type=%s", obj.get("type"))

    def _ws_url(self) -> str:
        base = (self.cfg.server_url or "").strip()
        if not base:
            return ""
        if base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        elif base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        if not base.endswith("/ws/agent"):
            base = base.rstrip("/") + "/ws/agent"
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}device_id={self.cfg.device_id}&token={self.cfg.server_token}"

    # ------------------------------------------------------------ 主循环
    async def run(self) -> None:
        tasks = [asyncio.create_task(self._writer(), name="writer")]
        if self.cfg.gh_rules_repo:
            tasks.append(asyncio.create_task(self._github_fallback(), name="gh-fallback"))
        try:
            while not self._stop.is_set():
                url = self._ws_url()
                if not url:
                    self.log.info("未配置 server_url，仅运行备用通道与本地规则")
                    await asyncio.sleep(30)
                    continue
                try:
                    await self._session(url)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.last_error = str(e)
                    self.connected = False
                    self.log.warning("主通道断开：%s", e)
                delay = min(self.cfg.reconnect_max_s, max(self.cfg.reconnect_min_s, self.cfg.reconnect_min_s * 2))
                await asyncio.sleep(delay)
        finally:
            for t in tasks:
                t.cancel()

    async def _session(self, url: str) -> None:
        import websockets  # type: ignore

        self.log.info("连接云控服务端 %s", url.split("?")[0])
        async with websockets.connect(url, max_size=MAX_WS_SIZE, ping_interval=25, ping_timeout=25) as ws:
            self.ws = ws
            self.connected = True
            await ws.send(json.dumps(self.hello(), ensure_ascii=False))
            hb = asyncio.create_task(self._heartbeat(ws), name="heartbeat")
            try:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    await self._handle_inbound(msg, ws)
            finally:
                hb.cancel()
                self.connected = False
                self.ws = None

    def hello(self) -> dict[str, Any]:
        return {
            "type": "hello",
            "device_id": self.cfg.device_id,
            "name": self.cfg.device_name,
            "group": self.cfg.group,
            "ver": "1.0.0",
            "os": f"{platform.system()}-{platform.release()}",
            "host": platform.node(),
            "caps": ["desktop", "shell", "files", "capture", "scan", "git", "rules"],
            "ts": int(time.time()),
        }

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(max(5, self.cfg.heartbeat_s))
            try:
                await ws.send(json.dumps({"type": "pong", "ts": int(time.time())}))
            except Exception:
                return

    async def _handle_inbound(self, msg: dict, ws) -> None:
        mtype = msg.get("type")
        if mtype == "rules":
            await self.on_rules(msg.get("rules") or {}, int(msg.get("version") or 0))
            return
        if mtype == "ping":
            await self.send({"type": "pong", "ts": int(time.time())})
            return
        if mtype == "frame":  # 服务端不推帧，忽略
            return
        result = await self.on_message(msg)
        if result is not None:
            await self.send(result)

    async def _writer(self) -> None:
        while True:
            obj = await self._outbox.get()
            ws = self.ws
            if ws is None:
                if obj.get("type") in ("result", "event"):
                    self._spool_to_disk(obj)
                continue
            try:
                await asyncio.wait_for(ws.send(json.dumps(obj, ensure_ascii=False)), SEND_TIMEOUT)
            except Exception as e:
                self.log.warning("发送失败：%s", e)
                self._spool_to_disk(obj)

    def _spool_to_disk(self, obj: dict) -> None:
        """离线暂存，避免断网时上报丢失。"""
        path = self.cfg.home_path / "spool.jsonl"
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except Exception:
            pass

    async def flush_spool(self) -> None:
        path = self.cfg.home_path / "spool.jsonl"
        if not path.exists() or self.ws is None:
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        path.unlink(missing_ok=True)
        for line in lines[-500:]:
            try:
                await self.send(json.loads(line))
            except Exception:
                continue

    # ------------------------------------------------------------ 备用通道
    async def _github_fallback(self) -> None:
        cfg = self.cfg
        repo = cfg.gh_rules_repo.strip("/")
        if not repo:
            return
        api = f"https://api.github.com/repos/{repo}/contents"
        raw = f"https://raw.githubusercontent.com/{repo}/main"
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "cloudctl-agent"}
        if cfg.gh_token:
            headers["Authorization"] = f"Bearer {cfg.gh_token}"
        seen_rule_ver = -1
        while not self._stop.is_set():
            try:
                req = _requests()
                if req is None:
                    await asyncio.sleep(60)
                    continue
                # 1) 规则文件
                r = await asyncio.to_thread(
                    req.get, f"{raw}/{cfg.gh_rules_path}", timeout=20
                )
                if r.status_code == 200:
                    data = r.json()
                    ver = int(data.get("version") or 0)
                    if ver != seen_rule_ver:
                        seen_rule_ver = ver
                        await self.on_rules(data, ver)
                        self.log.info("备用通道加载规则 version=%s", ver)
                # 2) 命令文件
                cmd_path = f"{cfg.gh_cmd_dir}/{cfg.device_id}.json"
                r2 = await asyncio.to_thread(req.get, f"{raw}/{cmd_path}", timeout=20)
                if r2.status_code == 200:
                    payload = r2.json() or {}
                    for cmd in payload.get("commands") or []:
                        if cmd.get("_done"):
                            continue
                        result = await self.on_message(cmd)
                        if result is not None:
                            await self._gh_write_outbox(f"{cfg.gh_outbox_dir}/{cfg.device_id}.jsonl", result)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log.debug("备用通道轮询失败：%s", e)
            await asyncio.sleep(max(10, cfg.gh_poll_s))

    async def _gh_write_outbox(self, path: str, result: dict) -> None:
        """把结果追加写入仓库 outbox 文件（读-改-写）。"""
        cfg = self.cfg
        req = _requests()
        if req is None or not cfg.gh_token:
            return
        api = f"https://api.github.com/repos/{cfg.gh_rules_repo}/contents/{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {cfg.gh_token}",
            "User-Agent": "cloudctl-agent",
        }

        def _do() -> None:
            sha = None
            body = ""
            g = req.get(api, headers=headers, timeout=20)
            if g.status_code == 200:
                j = g.json()
                sha = j.get("sha")
                body = base64.b64decode(j.get("content", "")).decode("utf-8", "replace")
            body += json.dumps(result, ensure_ascii=False) + "\n"
            if len(body) > 900 * 1024:
                body = "\n".join(body.splitlines()[-2000:]) + "\n"
            payload = {
                "message": f"agent {cfg.device_id}: result {result.get('id', '-')}",
                "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
            }
            if sha:
                payload["sha"] = sha
            req.put(api, headers=headers, json=payload, timeout=30)

        try:
            await asyncio.to_thread(_do)
        except Exception as e:
            self.log.debug("outbox 写入失败：%s", e)
