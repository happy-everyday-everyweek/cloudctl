"""控制通道：WebSocket 与 GitHub 两条平行通道。

两条通道地位对等：各自独立重连、独立暂存，都能下发命令与规则，
也都能上报事件与结果。命令按 id 去重，避免两条通道同时送达时重复执行。
设备本地控制台（devsrv.py）与局域网互联（mesh.py）都不依赖这两条通道。

GitHub 通道的容错前提（国内网络经常连不上）：
抓不到新规则时继续用本地缓存 rules.remote.json；支持镜像前缀（gh_proxy）；
失败后指数退避到 gh_backoff_max_s 并加随机抖动；遇到速率限制或 403 自动拉长间隔；
上报失败写入 spool.gh.jsonl，等网络恢复后补齐。
"""
from __future__ import annotations

import asyncio
import base64
import json
import platform
import random
import time
from typing import Any, Awaitable, Callable

from .config import Config

SEND_TIMEOUT = 20
MAX_WS_SIZE = 48 * 1024 * 1024
DEDUP_MAX = 800
GH_BATCH = 25
GH_FETCH_TIMEOUT = 20


def _requests():
    try:
        import requests  # type: ignore
        return requests
    except Exception:
        return None


class ControlChannel:
    """同时维护两条对等的外联通道。"""

    def __init__(self, cfg: Config,
                 on_message: Callable[[dict], Awaitable[dict | None]],
                 on_rules: Callable[[dict, int], Awaitable[None]], log) -> None:
        self.cfg = cfg
        self.on_message = on_message
        self.on_rules = on_rules
        self.log = log
        self.ws = None
        self._stop = asyncio.Event()
        self._ws_q: asyncio.Queue[dict] = asyncio.Queue(maxsize=2000)
        self._gh_q: asyncio.Queue[dict] = asyncio.Queue(maxsize=2000)
        self._seen: dict[str, float] = {}
        self._gh_rule_ver = -1
        self.gh_fails = 0
        self.gh_mirror_ok = False
        self.links: dict[str, dict[str, Any]] = {
            "ws": {"enabled": bool(cfg.server_url), "connected": False, "last_error": "", "fails": 0, "sent": 0, "recv": 0},
            "gh": {"enabled": bool(cfg.gh_rules_repo), "connected": False, "last_error": "", "fails": 0, "sent": 0, "recv": 0},
        }

    # --- 发送 ---
    async def send(self, obj: dict) -> None:
        for name, q in (("ws", self._ws_q), ("gh", self._gh_q)):
            if not self.links[name]["enabled"]:
                continue
            try:
                q.put_nowait(obj)
            except asyncio.QueueFull:
                self.log.warning("%s 通道队列已满，丢弃 type=%s", name, obj.get("type"))

    @property
    def online(self) -> bool:
        return bool(self.links["ws"]["connected"] or self.links["gh"]["connected"])

    def status(self) -> dict:
        return {"ws": dict(self.links["ws"]), "gh": dict(self.links["gh"]),
                "online": self.online, "gh_fails": self.gh_fails,
                "gh_mirror": self.gh_mirror_ok, "spool": self._spool_size()}

    def _spool_path(self, name: str):
        return self.cfg.home_path / f"spool.{name}.jsonl"

    def _spool_size(self) -> dict:
        out = {}
        for name in ("ws", "gh"):
            p = self._spool_path(name)
            out[name] = p.stat().st_size if p.exists() else 0
        return out

    def _ws_url(self) -> str:
        base = (self.cfg.server_url or "").strip()
        if not base:
            return ""
        if base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        elif base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        if not base.endswith("/ws/agent"):
            base = base.rstrip("/") + "/ws/agent"
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}device_id={self.cfg.device_id}&token={self.cfg.server_token}"

    # --- 主循环 ---
    async def run(self) -> None:
        tasks = []
        if self.links["ws"]["enabled"]:
            tasks.append(asyncio.create_task(self._ws_loop(), name="channel-ws"))
        else:
            self.log.info("WebSocket 通道未配置（server_url 为空）")
        if self.links["gh"]["enabled"]:
            tasks.append(asyncio.create_task(self._gh_loop(), name="channel-gh"))
        else:
            self.log.info("GitHub 通道未配置（gh_rules_repo 为空）")
        if not tasks:
            self.log.warning("两条外联通道均未配置，仅本地控制台与局域网互联可用")
            await self._stop.wait()
            return
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()

    def _backoff(self, name: str) -> float:
        n = int(self.links[name].get("fails", 0)) + 1
        self.links[name]["fails"] = n
        return float(min(self.cfg.reconnect_max_s, self.cfg.reconnect_min_s * min(n, 8)))

    # --- WebSocket 通道 ---
    async def _ws_loop(self) -> None:
        while not self._stop.is_set():
            url = self._ws_url()
            try:
                await self._ws_session(url)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.links["ws"]["connected"] = False
                self.links["ws"]["last_error"] = str(e)
                self.log.warning("WebSocket 通道断开：%s", e)
            await asyncio.sleep(self._backoff("ws"))

    async def _ws_session(self, url: str) -> None:
        import websockets  # type: ignore
        self.log.info("WebSocket 通道连接 %s", url.split("?")[0])
        async with websockets.connect(url, max_size=MAX_WS_SIZE, ping_interval=25, ping_timeout=25) as ws:
            self.ws = ws
            self.links["ws"].update({"connected": True, "last_error": "", "fails": 0})
            await ws.send(json.dumps(self.hello(), ensure_ascii=False))
            await self._flush_spool("ws")
            reader = asyncio.create_task(self._ws_reader(ws), name="ws-reader")
            writer = asyncio.create_task(self._ws_writer(ws), name="ws-writer")
            hb = asyncio.create_task(self._heartbeat(ws), name="ws-heartbeat")
            done, pending = await asyncio.wait({reader, writer, hb}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                if t.cancelled():
                    continue
                exc = t.exception()
                if exc:
                    raise exc
        self.ws = None
        self.links["ws"]["connected"] = False

    async def _ws_reader(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if isinstance(msg, dict):
                await self._inbound(msg, "ws")

    async def _ws_writer(self, ws) -> None:
        while True:
            obj = await self._ws_q.get()
            await asyncio.wait_for(ws.send(json.dumps(obj, ensure_ascii=False)), SEND_TIMEOUT)
            self.links["ws"]["sent"] += 1

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(max(5, self.cfg.heartbeat_s))
            await ws.send(json.dumps({"type": "pong", "ts": int(time.time())}))

    def hello(self) -> dict[str, Any]:
        return {"type": "hello", "device_id": self.cfg.device_id, "name": self.cfg.device_name,
                "group": self.cfg.group, "ver": "1.2.0", "devconsole": self.cfg.devconsole_url,
                "mesh": {"group": self.cfg.mesh_scope, "port": int(self.cfg.mesh_port)},
                "os": f"{platform.system()}-{platform.release()}", "host": platform.node(),
                "caps": ["devconsole", "mesh", "desktop", "shell", "files", "capture", "scan", "git", "rules"],
                "ts": int(time.time())}

    # --- GitHub 通道（高容错） ---
    def _gh_bases(self) -> list[str]:
        prim = self.cfg.gh_raw_base.rstrip("/")
        out = []
        proxy = (self.cfg.gh_proxy or "").strip().rstrip("/")
        if proxy:
            if self.gh_mirror_ok:
                out.append(f"{proxy}/{prim}")
                out.append(prim)
            else:
                out.append(prim)
                out.append(f"{proxy}/{prim}")
        else:
            out.append(prim)
        return out

    async def _gh_loop(self) -> None:
        cfg = self.cfg
        repo = cfg.gh_rules_repo.strip("/")
        if not cfg.gh_token:
            self.log.warning("GitHub 通道未配置 gh_token，只能匿名读取，建议补上细粒度令牌")
        self.log.info("GitHub 通道已启动，仓库 %s，轮询 %ss，镜像 %s",
                      repo, cfg.gh_poll_s, cfg.gh_proxy or "无")
        await self._gh_load_cache()
        while not self._stop.is_set():
            ok = False
            try:
                req = _requests()
                if req is None:
                    self.log.warning("requests 不可用，GitHub 通道休眠 5 分钟")
                    await asyncio.sleep(300)
                    continue
                ok = await self._gh_poll_once(req, repo)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.links["gh"]["last_error"] = str(e)
                self.log.debug("GitHub 通道异常：%s", e)
            if ok:
                if self.gh_fails:
                    self.log.info("GitHub 通道恢复正常")
                self.gh_fails = 0
                self.links["gh"].update({"connected": True, "last_error": "", "fails": 0})
                await asyncio.sleep(max(10, cfg.gh_poll_s))
            else:
                self.gh_fails += 1
                self.links["gh"].update({"connected": False, "fails": self.gh_fails})
                if self.gh_fails in (1, 5) or self.gh_fails % 10 == 0:
                    self.log.warning("GitHub 通道不可达（连续 %s 次）：%s；本地缓存规则仍生效",
                                     self.gh_fails, self.links["gh"].get("last_error") or "网络不可达")
                await asyncio.sleep(self._gh_delay())

    def _gh_delay(self) -> float:
        base = max(10, int(self.cfg.gh_poll_s))
        wait = min(float(self.cfg.gh_backoff_max_s), base * (2 ** min(self.gh_fails, 6)))
        return wait * (0.8 + random.random() * 0.4)

    async def _gh_poll_once(self, req, repo: str) -> bool:
        cfg = self.cfg
        rules = await asyncio.to_thread(self._gh_fetch_json, req, f"{repo}/main/{cfg.gh_rules_path}")
        touched = False
        if isinstance(rules, dict):
            touched = True
            ver = int(rules.get("version") or 0)
            if ver != self._gh_rule_ver:
                self._gh_rule_ver = ver
                self.links["gh"]["recv"] += 1
                self._gh_write_cache(rules)
                await self.on_rules(rules, ver)
                self.log.info("GitHub 通道加载规则 version=%s", ver)
        payload = await asyncio.to_thread(
            self._gh_fetch_json, req, f"{repo}/main/{cfg.gh_cmd_dir}/{cfg.device_id}.json")
        if isinstance(payload, dict):
            touched = True
            for cmd in payload.get("commands") or []:
                if isinstance(cmd, dict):
                    await self._inbound(cmd, "gh")
        if touched:
            await self._gh_flush(req, repo)
        return touched

    def _gh_fetch_json(self, req, rel_path: str):
        last = ""
        for base in self._gh_bases():
            url = f"{base}/{rel_path}"
            try:
                r = req.get(url, headers=self._gh_headers(), timeout=GH_FETCH_TIMEOUT)
            except Exception as e:
                last = str(e)
                continue
            if r.status_code == 200:
                if base != self.cfg.gh_raw_base.rstrip("/"):
                    self.gh_mirror_ok = True
                try:
                    return r.json()
                except Exception as e:
                    last = f"JSON 解析失败 {e}"
                    continue
            last = f"HTTP {r.status_code}"
            if r.status_code == 403 and (r.headers.get("X-RateLimit-Remaining") == "0"):
                last = "触发速率限制"
        self.links["gh"]["last_error"] = last or "网络不可达"
        return None

    def _gh_headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "User-Agent": "cloudctl-agent"}
        if self.cfg.gh_token:
            h["Authorization"] = f"Bearer {self.cfg.gh_token}"
        return h

    # --- 规则缓存：抓不到就用上一次的 ---
    async def _gh_load_cache(self) -> None:
        p = self.cfg.rules_cache
        if not p.exists():
            return
        try:
            cached = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return
        ver = int(cached.get("version") or 0)
        self._gh_rule_ver = ver - 1
        self.log.info("已载入上次从 GitHub 拉到的规则缓存 version=%s", ver)

    def _gh_write_cache(self, rules: dict) -> None:
        try:
            self.cfg.rules_cache.write_text(
                json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    async def _gh_flush(self, req, repo: str) -> None:
        batch: list[dict] = []
        spool = self._spool_path("gh")
        if spool.exists():
            try:
                for line in spool.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        batch.append(json.loads(line))
                spool.unlink(missing_ok=True)
            except Exception:
                pass
        while len(batch) < GH_BATCH and not self._gh_q.empty():
            batch.append(self._gh_q.get_nowait())
        if not batch or not self.cfg.gh_token:
            return
        path = f"{self.cfg.gh_outbox_dir}/{self.cfg.device_id}.jsonl"
        if await asyncio.to_thread(self._gh_append, req, repo, path, batch):
            self.links["gh"]["sent"] += len(batch)
        else:
            self._spool_append("gh", batch)

    def _gh_append(self, req, repo: str, path: str, batch: list[dict]) -> bool:
        prim = self.cfg.gh_api_base.rstrip("/")
        bases = [prim]
        proxy = (self.cfg.gh_proxy or "").strip().rstrip("/")
        if proxy:
            bases.append(f"{proxy}/{prim}")
        for base in bases:
            api = f"{base}/repos/{repo}/contents/{path}"
            try:
                sha = None
                body = ""
                g = req.get(api, headers=self._gh_headers(), timeout=GH_FETCH_TIMEOUT)
                if g.status_code == 200:
                    j = g.json()
                    sha = j.get("sha")
                    body = base64.b64decode(j.get("content", "")).decode("utf-8", "replace")
                for obj in batch:
                    body += json.dumps(obj, ensure_ascii=False) + "\n"
                if len(body) > 900 * 1024:
                    body = "\n".join(body.splitlines()[-2000:]) + "\n"
                payload: dict[str, Any] = {
                    "message": f"agent {self.cfg.device_id}: report {len(batch)} item(s)",
                    "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
                }
                if sha:
                    payload["sha"] = sha
                r = req.put(api, headers=self._gh_headers(), json=payload, timeout=30)
                if r.status_code in (200, 201):
                    return True
            except Exception as e:
                self.log.debug("GitHub outbox 写入失败：%s", e)
        return False

    # --- 入站与去重 ---
    def _is_dup(self, obj: dict) -> bool:
        cid = obj.get("id")
        if not cid:
            return False
        now = time.time()
        if len(self._seen) > DEDUP_MAX:
            for k, t in list(self._seen.items()):
                if now - t > 600:
                    self._seen.pop(k, None)
        key = str(cid)
        if key in self._seen:
            return True
        self._seen[key] = now
        return False

    async def _inbound(self, msg: dict, source: str) -> None:
        mtype = msg.get("type")
        if mtype == "rules":
            await self.on_rules(msg.get("rules") or {}, int(msg.get("version") or 0))
            return
        if mtype == "ping":
            await self.send({"type": "pong", "ts": int(time.time())})
            return
        if mtype == "frame":
            return
        if self._is_dup(msg):
            self.log.debug("命令 %s 已由另一条通道执行，跳过", msg.get("id"))
            return
        self.links[source]["recv"] += 1
        result = await self.on_message(msg)
        if result is not None:
            await self.send(result)

    # --- 离线暂存 ---
    def _spool_append(self, name: str, items: list[dict] | dict) -> None:
        seq = items if isinstance(items, list) else [items]
        try:
            with open(self._spool_path(name), "a", encoding="utf-8") as f:
                for obj in seq:
                    f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except Exception:
            pass

    async def _flush_spool(self, name: str) -> None:
        p = self._spool_path(name)
        if not p.exists():
            return
        try:
            lines = [x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
            p.unlink(missing_ok=True)
        except Exception:
            return
        q = self._ws_q if name == "ws" else self._gh_q
        for line in lines[-1000:]:
            try:
                q.put_nowait(json.loads(line))
            except Exception:
                continue
        self.log.info("%s 通道补齐离线暂存 %s 条", name, min(len(lines), 1000))
