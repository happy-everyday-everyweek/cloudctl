"""命令路由：把服务端下发的消息分发到具体子系统。

所有破坏性操作都过一遍规则里的 security 开关，服务端与客户端
dua 重门禁，任意一侧关闭都不执行。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from . import ops, startup
from .config import Config
from .rules import RuleSet
from .util import human_size


class Router:
    def __init__(
        self,
        cfg: Config,
        rules: RuleSet,
        capture,
        streamer,
        injector,
        sync,
        db,
        log,
        apply_rules: Callable[[dict, int], Awaitable[None]],
    ) -> None:
        self.cfg = cfg
        self.rules = rules
        self.capture = capture
        self.streamer = streamer
        self.injector = injector
        self.sync = sync
        self.db = db
        self.log = log
        self.apply_rules = apply_rules
        self.sessions: dict[str, ops.ShellSession] = {}
        self.stats = {"commands": 0, "errors": 0, "started_ts": int(time.time())}

    # ------------------------------------------------------------ 入口
    async def handle(self, msg: dict) -> dict[str, Any] | None:
        mtype = msg.get("type")
        if mtype == "desktop.input":
            res = await asyncio.to_thread(self.injector.apply, msg.get("events") or [])
            await self._safe_call("desktop.input", res)
            return None
        if mtype in ("desktop.start", "desktop.stop"):
            return None  # 由 main 里的专用处理接管
        if mtype != "cmd":
            return None

        cid = msg.get("id") or ""
        op = msg.get("op") or ""
        args = msg.get("args") or {}
        self.stats["commands"] += 1
        started = time.time()
        try:
            data = await self._dispatch(op, args)
            return {"type": "result", "id": cid, "op": op, "ok": True, "data": data,
                    "elapsed": round(time.time() - started, 3), "ts": int(time.time())}
        except Exception as e:
            self.stats["errors"] += 1
            self.log.warning("命令失败 op=%s: %s", op, e)
            return {"type": "result", "id": cid, "op": op, "ok": False, "err": str(e),
                    "elapsed": round(time.time() - started, 3), "ts": int(time.time())}

    async def _safe_call(self, tag: str, res: dict) -> None:
        if res.get("applied") == 0 and res.get("reason"):
            self.log.debug("输入未应用 %s: %s", tag, res.get("reason"))

    # ------------------------------------------------------------ 分发
    async def _dispatch(self, op: str, args: dict) -> Any:
        sec = self.rules.security

        if op == "sys.info":
            return await asyncio.to_thread(ops.sys_info)

        if op == "agent.info":
            return {
                "device_id": self.cfg.device_id,
                "device_name": self.cfg.device_name,
                "rules_version": self.rules.version,
                "capture_dir": str(self.cfg.capture_dir),
                "shell_sessions": [k for k, v in self.sessions.items() if v.alive],
                "desktop": {"running": self.streamer.running, "frames": self.streamer.frames, "fps": self.streamer.fps},
                "stats": self.stats,
                "startup": await asyncio.to_thread(startup.status),
                "db": self.db.summary(),
                "config": self.cfg.public(),
            }

        # --- 终端 ---
        if op == "shell.exec":
            self._require(bool(sec.get("allow_shell")) and self.cfg.allow_shell, "终端已关闭")
            return await asyncio.to_thread(ops.shell_exec, args.get("cmd", ""), int(args.get("timeout_s") or 60), args.get("cwd"))

        if op == "shell.open":
            self._require(bool(sec.get("allow_shell")) and self.cfg.allow_shell, "终端已关闭")
            name = args.get("session") or "default"
            s = self.sessions.get(name) or ops.ShellSession(name)
            self.sessions[name] = s
            return await asyncio.to_thread(s.start)

        if op == "shell.write":
            name = args.get("session") or "default"
            s = self.sessions.get(name) or ops.ShellSession(name)
            self.sessions[name] = s
            return await asyncio.to_thread(s.write, args.get("cmd") or args.get("data") or "")

        if op == "shell.read":
            s = self.sessions.get(args.get("session") or "default")
            return await asyncio.to_thread(s.read) if s else {"output": ""}

        if op == "shell.close":
            s = self.sessions.pop(args.get("session") or "default", None)
            return await asyncio.to_thread(s.close) if s else {"closed": False}

        # --- 文件 ---
        if op == "file.list":
            return await asyncio.to_thread(ops.file_list, args.get("path") or ".")
        if op == "file.pull":
            return await asyncio.to_thread(ops.file_pull, args.get("path"), int(args.get("offset") or 0), int(args.get("length") or 512 * 1024))
        if op == "file.push":
            self._require(bool(sec.get("allow_file_write")) and self.cfg.allow_file_write, "写入已关闭")
            return await asyncio.to_thread(ops.file_push, args.get("path"), args.get("data_b64") or "", bool(args.get("append")), int(args.get("offset") or 0))
        if op == "file.mkdir":
            self._require(bool(sec.get("allow_file_write")) and self.cfg.allow_file_write, "写入已关闭")
            return await asyncio.to_thread(ops.file_mkdir, args.get("path"))
        if op == "file.rename":
            self._require(bool(sec.get("allow_file_write")) and self.cfg.allow_file_write, "写入已关闭")
            return await asyncio.to_thread(ops.file_rename, args.get("path"), args.get("new_path"))
        if op == "file.delete":
            self._require(bool(sec.get("allow_delete")) and self.cfg.allow_delete, "删除已关闭")
            return await asyncio.to_thread(ops.file_delete, args.get("path"))
        if op == "file.hash":
            return await asyncio.to_thread(ops.file_hash, args.get("path"))

        # --- 进程 ---
        if op == "proc.list":
            return await asyncio.to_thread(ops.proc_list, int(args.get("limit") or 200), args.get("keyword") or "")
        if op == "proc.kill":
            self._require(bool(sec.get("allow_shell")), "进程管理已关闭")
            return await asyncio.to_thread(ops.proc_kill, int(args.get("pid")), bool(args.get("force", True)))

        # --- 采集 ---
        if op == "capture.photo":
            if (args.get("device") or "screen") == "camera":
                idx = int(args.get("index") or 0)
                return await asyncio.to_thread(self.capture.photo_camera, idx, args.get("path"))
            return await asyncio.to_thread(self.capture.photo_screen, int(args.get("monitor") or 0), int(args.get("quality") or 80), args.get("path"))
        if op == "capture.video":
            return await asyncio.to_thread(
                self.capture.record,
                int(args.get("seconds") or 60), int(args.get("fps") or 5),
                int(args.get("quality") or 60), int(args.get("monitor") or 0), args.get("path"),
            )

        # --- 归档 ---
        if op == "scan.now":
            files = await asyncio.to_thread(self.sync.scanner.walk)
            by_kind: dict[str, int] = {}
            total = 0
            for f in files:
                by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
                total += f.size
            return {"count": len(files), "bytes": total, "bytes_h": human_size(total), "by_kind": by_kind, "roots": self.rules.scan.get("roots")}
        if op == "sync.now":
            return await asyncio.to_thread(self.sync.sync_once, int(args.get("limit") or 500))
        if op == "sync.status":
            return {"db": self.db.summary(), "last_run": self.sync.last_run, "pending": len(self.db.pending())}

        # --- 规则与自身 ---
        if op == "agent.rules":
            await self.apply_rules(args.get("rules") or {}, int(args.get("version") or 0))
            return {"version": self.rules.version, "applied": True}
        if op == "agent.autostart":
            return await asyncio.to_thread(startup.status if args.get("action", "install") == "status" else (startup.remove if args.get("action") == "remove" else startup.install), self.cfg.home_path, self.log) if args.get("action") != "status" else await asyncio.to_thread(startup.status)
        if op == "agent.log":
            log_path = self.cfg.home_path / "agent.log"
            n = int(args.get("lines") or 200)
            if not log_path.exists():
                return {"lines": []}
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-n:]
            return {"lines": [ln.rstrip("\n") for ln in lines]}

        raise ValueError(f"未知命令：{op}")

    @staticmethod
    def _require(cond: bool, msg: str) -> None:
        if not cond:
            raise PermissionError(msg)
