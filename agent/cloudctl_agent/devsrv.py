"""设备本地服务：控制台 + HTTP API + 桌面流。

安全修复：以前只要没配令牌，令牌会回退到 device_id（可枚举），而绑定地址可以改成 0.0.0.0，
相当于把一个公开端口暴露出去。现在规定：绑定非回环地址（0.0.0.0 / 局域网 IP）时，
必须显式配置 devsrv_token 或 server_token，否则拒绝启动并写入错误日志。
默认仍只绑 127.0.0.1。
"""
from __future__ import annotations

import asyncio
import hmac
import io
import json
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import ops

STATIC = Path(__file__).resolve().parent / "devstatic"
BOUNDARY = "cloudctlframe"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class DeviceServer:
    def __init__(self, cfg, rules, capture, injector, sync, log,
                 get_router: Callable[[], Any] = lambda: None,
                 get_loop: Callable[[], Any] = lambda: None,
                 on_report: Callable[[], dict] | None = None,
                 agent_version: str = "0.0.0") -> None:
        self.cfg = cfg
        self.rules = rules
        self.capture = capture
        self.injector = injector
        self.sync = sync
        self.log = log
        self.get_router = get_router
        self.get_loop = get_loop
        self.on_report = on_report
        self.version = agent_version
        self.token = cfg.devsrv_secret
        self.explicit_token = bool(cfg.devsrv_has_explicit_token)
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.started_ts = 0.0
        self.requests = 0
        self.errors = 0

    # ------------------------------------------------------------ 生命周期
    def start(self) -> dict:
        if not self.cfg.devsrv_enabled:
            return {"enabled": False}
        if self.httpd is not None:
            return {"enabled": True, "already": True}
        host = self.cfg.devsrv_bind or "127.0.0.1"
        if host not in LOOPBACK and not self.explicit_token:
            msg = (f"拒绝启动：绑定到 {host} 但未配置 devsrv_token/server_token。"
                   "开放到局域网或公网前必须先设令牌。")
            self.log.error(msg)
            return {"enabled": False, "err": msg}
        handler = _make_handler(self)
        self.httpd = ThreadingHTTPServer((host, int(self.cfg.devsrv_port)), handler)
        self.httpd.daemon_threads = True
        self.started_ts = time.time()
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="devsrv", daemon=True)
        self.thread.start()
        self.log.info("设备本地服务已监听 http://%s:%s（%s）", host, self.cfg.devsrv_port,
                      "带令牌" if self.explicit_token else "仅本机、无显式令牌")
        return {"enabled": True, "url": f"http://{host}:{self.cfg.devsrv_port}"}

    def stop(self) -> dict:
        if self.httpd is None:
            return {"stopped": False}
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
        self.httpd = None
        return {"stopped": True}

    def status(self) -> dict:
        return {
            "enabled": bool(self.cfg.devsrv_enabled),
            "listening": self.httpd is not None,
            "bind": self.cfg.devsrv_bind,
            "port": int(self.cfg.devsrv_port),
            "uptime_s": int(time.time() - self.started_ts) if self.started_ts else 0,
            "requests": self.requests,
            "errors": self.errors,
            "auth": "token" if self.explicit_token else "local-only",
            "version": self.version,
        }

    # ------------------------------------------------------------ 鉴权
    def check_token(self, got: str) -> bool:
        if not self.token:
            return False
        return hmac.compare_digest(str(got or ""), str(self.token))

    # ------------------------------------------------------------ 能力
    def run_cmd(self, op: str, args: dict, timeout: float = 120.0) -> dict:
        router = self.get_router()
        loop = self.get_loop()
        if router is not None and loop is not None and loop.is_running():
            msg = {"type": "cmd", "id": uuid.uuid4().hex, "op": op, "args": args or {}}
            fut = asyncio.run_coroutine_threadsafe(router.handle(msg), loop)
            return fut.result(timeout=timeout)
        return self._fallback(op, args or {})

    def _fallback(self, op: str, args: dict) -> dict:
        try:
            data: Any
            if op == "agent.info":
                data = self.info()
            elif op == "sys.info":
                data = ops.sys_info()
            elif op == "file.list":
                data = ops.file_list(args.get("path") or ".")
            elif op == "scan.now":
                files = self.sync.scanner.walk()
                data = {"count": len(files), "bytes": sum(f.size for f in files)}
            else:
                return {"ok": False, "err": f"未启动控制内核，无法执行 {op}"}
            return {"type": "result", "ok": True, "data": data}
        except Exception as e:
            return {"type": "result", "ok": False, "err": str(e)}

    def info(self) -> dict:
        """agent.info 的唯一组装点，避免与 Router 各写一份。"""
        idx = getattr(self.sync, "index", None)
        return {"device_id": self.cfg.device_id, "device_name": self.cfg.device_name,
                "version": self.version, "rules_version": self.rules.version,
                "devserver": self.status(),
                "capture": {"camera": self.rules.camera.get("enabled"),
                            "audio_trigger": self.rules.audio.get("enabled"),
                            "threshold_db": self.rules.audio.get("threshold_db")},
                "report": self.rules.report,
                "update": self.rules.update,
                "db": self.sync.db.summary(),
                "index": idx.summary() if idx is not None else {}}

    def report_now(self) -> dict:
        if self.on_report is None:
            return {"ok": False, "err": "未装配上报入口"}
        return self.on_report()

    def snapshot(self, quality: int = 60, monitor: int = 0) -> bytes:
        from .capture import ScreenSource

        img = ScreenSource(monitor).grab()
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=int(quality))
        return buf.getvalue()


# ------------------------------------------------------------------ 路由
class _Handler(BaseHTTPRequestHandler):
    server_version = "cloudctl-dev"
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        self.dev: DeviceServer = kw.pop("_dev")
        super().__init__(*a, **kw)

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _token_ok(self, q: dict) -> bool:
        got = (self.headers.get("X-Token") or q.get("token", [""])[0] or self._cookie_token())
        return self.dev.check_token(got)

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "cloudctl_dev_token":
                return v
        return ""

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # --- GET ---
    def do_GET(self) -> None:
        self.dev.requests += 1
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html", "text/html; charset=utf-8")
            if path == "/console.js":
                return self._static("console.js", "application/javascript; charset=utf-8")
            if path == "/api/dev/login":
                tok = q.get("token", [""])[0]
                if not self.dev.check_token(tok):
                    return self._json({"ok": False, "err": "令牌无效"}, 401)
                return self._send(200, b'{"ok":true}', extra={
                    "Set-Cookie": f"cloudctl_dev_token={tok}; Path=/; HttpOnly; SameSite=Lax"})
            if not self._token_ok(q):
                if path.startswith("/api/"):
                    return self._json({"ok": False, "err": "需要令牌"}, 401)
                return self._static("index.html", "text/html; charset=utf-8")
            if path == "/api/dev/status":
                return self._json({"ok": True, "data": self.dev.status()})
            if path == "/api/dev/info":
                return self._json({"ok": True, "data": self.dev.info()})
            if path == "/api/dev/log":
                return self._json({"ok": True, "data": self._tail(int(q.get("lines", ["200"])[0]))})
            if path == "/api/dev/report":
                return self._json({"ok": True, "data": self.dev.report_now()})
            if path == "/api/dev/frame.jpg":
                return self._frame(q)
            if path == "/api/dev/stream.mjpg":
                return self._mjpeg(q)
            return self._json({"ok": False, "err": "未知路径"}, 404)
        except Exception as e:
            self.dev.errors += 1
            return self._json({"ok": False, "err": str(e)}, 500)

    def _static(self, name: str, ctype: str) -> None:
        f = STATIC / name
        if not f.exists():
            return self._send(200, b"device console missing", "text/plain; charset=utf-8")
        return self._send(200, f.read_bytes(), ctype)

    def _tail(self, lines: int) -> dict:
        p = self.dev.cfg.home_path / "agent.log"
        if not p.exists():
            return {"lines": []}
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return {"lines": [x.rstrip() for x in f.readlines()[-max(1, min(lines, 2000)):]]}

    def _frame(self, q: dict) -> None:
        quality = int(q.get("q", ["60"])[0])
        monitor = int(q.get("monitor", ["0"])[0])
        self._send(200, self.dev.snapshot(quality, monitor), "image/jpeg")

    def _mjpeg(self, q: dict) -> None:
        fps = max(1, min(15, int(q.get("fps", ["5"])[0])))
        quality = max(20, min(90, int(q.get("q", ["55"])[0])))
        monitor = int(q.get("monitor", ["0"])[0])
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        interval = 1.0 / fps
        try:
            while True:
                t0 = time.time()
                jpg = self.dev.snapshot(quality, monitor)
                head = (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpg)}\r\n\r\n").encode()
                self.wfile.write(head + jpg + b"\r\n")
                self.wfile.flush()
                time.sleep(max(0.0, interval - (time.time() - t0)))
        except Exception:
            return

    # --- POST ---
    def do_POST(self) -> None:
        self.dev.requests += 1
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/dev/login":
                body = self._body()
                if not self.dev.check_token(body.get("token")):
                    return self._json({"ok": False, "err": "令牌无效"}, 401)
                return self._send(200, b'{"ok":true}', extra={
                    "Set-Cookie": f"cloudctl_dev_token={body.get('token')}; Path=/; HttpOnly; SameSite=Lax"})
            if not self._token_ok(q):
                return self._json({"ok": False, "err": "需要令牌"}, 401)
            body = self._body()
            if path == "/api/dev/cmd":
                op = body.get("op") or ""
                return self._json(self.dev.run_cmd(op, body.get("args") or {},
                                                   float(body.get("timeout") or 120)))
            if path == "/api/dev/input":
                return self._json({"ok": True, "data": self.dev.injector.apply(body.get("events") or [])})
            if path == "/api/dev/rules":
                rules = body.get("rules") or {}
                ver = int(body.get("version") or 0)
                changed = self.dev.rules.merge(rules, ver)
                if changed:
                    self.dev.rules.save(self.dev.cfg.rules_file)
                return self._json({"ok": True, "data": {"changed": changed,
                                                          "version": self.dev.rules.version}})
            if path == "/api/dev/scan":
                return self._json(self.dev.run_cmd("scan.now", {}, 300))
            if path == "/api/dev/sync":
                return self._json(self.dev.run_cmd("sync.now", body.get("args") or {}, 1800))
            if path == "/api/dev/report":
                return self._json({"ok": True, "data": self.dev.report_now()})
            return self._json({"ok": False, "err": "未知路径"}, 404)
        except Exception as e:
            self.dev.errors += 1
            return self._json({"ok": False, "err": str(e)}, 500)


def _make_handler(dev: DeviceServer):
    def _factory(*a, **kw):
        return _Handler(*a, _dev=dev, **kw)

    return _factory
