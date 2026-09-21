"""agent 入口：装配各子系统、启动本地控制台与两条外联通道。

命令行：
    agent --run            常驻运行（外联通道 + 本地控制台）
    agent --console        仅本地控制台（不建外联通道）
    agent --install        安装开机自启
    agent --uninstall      移除开机自启
    agent --status         查看状态
    agent --scan           只跑一轮扫描归档
    agent --photo          立即拍一张屏幕图
    agent --rules PATH     应用规则文件
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import threading
import time
from pathlib import Path

from .capture import CaptureService
from .channel import ControlChannel
from .config import Config
from .desktop import DesktopStreamer, InputInjector
from .devsrv import STATIC, DeviceServer
from .router import Router
from .rules import RuleSet, TriggerEngine
from .startup import install as startup_install, remove as startup_remove, status as startup_status
from .sync import StateDB, SyncService
from .util import IS_WINDOWS, foreground_window, idle_seconds, session_is_locked, set_dpi_aware, setup_logging

VERSION = "1.1.0"


class Agent:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.log = setup_logging(cfg.home_path, cfg.log_level, cfg.log_max_mb, cfg.log_keep)
        self.rules = RuleSet.load(cfg.rules_file)
        self.db = StateDB(cfg.state_db)
        self.capture = CaptureService(cfg.capture_dir, self.log)
        self.streamer = DesktopStreamer(self.log)
        self.injector = InputInjector(self.log)
        self.sync = SyncService(self.rules, self.db, self.log)
        self.engine = TriggerEngine(self.rules)
        self.channel: ControlChannel | None = None
        self.devsrv: DeviceServer | None = None
        self.router: Router | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.console_only = False
        self._stop = threading.Event()
        self._recording = False
        self._rec_status: dict = {}

    # ------------------------------------------------------------ 生命周期
    async def apply_rules(self, incoming: dict, version: int = 0) -> None:
        changed = self.rules.merge(incoming, version)
        if changed:
            self.rules.save(self.cfg.rules_file)
            self.engine.reload(self.rules)
            self.sync.reload(self.rules)
            self.log.info("规则已更新到 version=%s", self.rules.version)
        if incoming.get("upload", {}).get("token"):
            self.rules.data.setdefault("_upload_token", incoming["upload"]["token"])

    async def on_message(self, msg: dict):
        mtype = msg.get("type")
        if mtype == "desktop.start" and self.router is not None:
            res = await self.streamer.start(self.channel.send, **(msg.get("args") or {}))
            return {"type": "result", "id": msg.get("id"), "op": "desktop.start", "ok": True, "data": res}
        if mtype == "desktop.stop":
            res = await self.streamer.stop()
            return {"type": "result", "id": msg.get("id"), "op": "desktop.stop", "ok": True, "data": res}
        if mtype == "desktop.input" and self.router is not None:
            await self.router.handle(msg)
            return None
        if self.router is not None:
            return await self.router.handle(msg)
        return None

    async def run(self) -> None:
        set_dpi_aware()
        self.loop = asyncio.get_running_loop()
        self.router = Router(
            self.cfg, self.rules, self.capture, self.streamer, self.injector,
            self.sync, self.db, self.log, self.apply_rules,
        )
        self.devsrv = DeviceServer(
            self.cfg, self.rules, self.capture, self.injector, self.sync, self.log,
            get_router=lambda: self.router, get_loop=lambda: self.loop,
        )
        info = self.devsrv.start()
        if info.get("enabled"):
            self.log.info("设备本地控制台已就绪 %s", self.cfg.devconsole_url)
        else:
            self.log.info("设备本地控制台未启用（devsrv_enabled=false）")
        self.log.info(
            "cloudctl agent %s 启动 device=%s home=%s rules_v=%s",
            VERSION, self.cfg.device_id, self.cfg.home, self.rules.version,
        )
        threading.Thread(target=self._trigger_loop, name="trigger", daemon=True).start()
        threading.Thread(target=self._scan_loop, name="scan", daemon=True).start()
        if self.console_only:
            self.log.info("仅本地控制台模式：不建立外联通道")
            while not self._stop.is_set():
                await asyncio.sleep(0.5)
            return
        self.channel = ControlChannel(self.cfg, self.on_message, self.apply_rules, self.log)
        await self._emit({"type": "event", "event": "agent.online", "data": {"ver": VERSION, "rules": self.rules.version}})
        try:
            await self.channel.run()
        finally:
            self._stop.set()

    async def _emit(self, obj: dict) -> None:
        if self.channel:
            await self.channel.send(obj)

    def _emit_threadsafe(self, obj: dict) -> None:
        if self.channel is None or self.loop is None:
            return
        asyncio.run_coroutine_threadsafe(self.channel.send(obj), self.loop)

    # ------------------------------------------------------------ 自动采集
    def _trigger_loop(self) -> None:
        self.log.info("采集调度已启动")
        while not self._stop.is_set():
            try:
                tick = 5.0
                title, _pid = foreground_window()
                idle = idle_seconds()
                locked = session_is_locked()
                decision = self.engine.decide(title, idle, locked)
                if decision["video_stop"] and self._recording:
                    self._recording = False
                    self._emit_threadsafe({"type": "event", "event": "capture.video_stop", "data": {"reason": decision["reason"]}})
                if decision["video_start"] and not self._recording:
                    self._start_recording(decision["reason"], title)
                if decision["photo"]:
                    self._take_photo()
                if locked:
                    tick = 30.0
                time.sleep(tick)
            except Exception as e:
                self.log.warning("采集判定异常：%s", e)
                time.sleep(10)

    def _take_photo(self) -> None:
        ph = self.rules.photo
        device = (ph.get("device") or "cam0").lower()
        try:
            if device.startswith("cam"):
                idx = int("".join(ch for ch in device if ch.isdigit()) or 0)
                info = self.capture.photo_camera(idx)
            else:
                info = self.capture.photo_screen(int(self.rules.desktop.get("monitors", 1) - 1))
            self.engine.note_photo()
            self._emit_threadsafe({"type": "event", "event": "capture.done", "data": info})
            self.log.info("已拍照 %s（%s）", info["path"], info.get("kind"))
        except Exception as e:
            self.log.warning("拍照失败：%s", e)

    def _start_recording(self, reason: str, title: str) -> None:
        vd = self.rules.video
        seconds = int(vd.get("segment_s") or 60)
        fps = int(vd.get("fps") or 5)
        quality = int(vd.get("quality") or 60)
        monitor = int(vd.get("monitor") or 0)
        self._recording = True
        self.engine.note_video_start()

        def _job() -> None:
            try:
                info = self.capture.record(seconds=seconds, fps=fps, quality=quality, monitor=monitor)
                self.engine.note_video_bytes(int(info.get("bytes") or 0))
                self._rec_status = info
                self._emit_threadsafe(
                    {"type": "event", "event": "capture.video_done",
                     "data": {**info, "reason": reason, "title": title}}
                )
                self.log.info("段录像完成 %s（%s）", info.get("path"), info.get("size_h"))
            except Exception as e:
                self.log.warning("录制失败：%s", e)
            finally:
                self._recording = False
                self.engine.note_video_stop()

        threading.Thread(target=_job, name="recorder", daemon=True).start()
        self._emit_threadsafe({"type": "event", "event": "capture.video_start", "data": {"seconds": seconds, "fps": fps, "reason": reason}})

    # ------------------------------------------------------------ 归档调度
    def _scan_loop(self) -> None:
        while not self._stop.is_set():
            interval = max(1, int(self.rules.scan.get("interval_min") or 30))
            time.sleep(interval * 60)
            if self._stop.is_set():
                break
            if not self.rules.upload.get("enabled"):
                continue
            try:
                stats = self.sync.sync_once()
                self._emit_threadsafe({"type": "event", "event": "sync.done", "data": stats})
            except Exception as e:
                self.log.warning("归档异常：%s", e)

    def stop(self) -> None:
        self._stop.set()
        if self.devsrv is not None:
            self.devsrv.stop()


# ------------------------------------------------------------------ CLI

def _selftest() -> int:
    ok = []
    for mod in ("websockets", "requests", "PIL", "mss", "cv2", "psutil", "pynput"):
        try:
            __import__(mod)
            ok.append(f"{mod}: ok")
        except Exception as e:
            ok.append(f"{mod}: 缺失（{e}）")
    static_ok = (STATIC / "index.html").exists() and (STATIC / "console.js").exists()
    ok.append(f"devstatic: {'ok' if static_ok else '缺失'}")
    print("\n".join(ok))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser("cloudctl-agent")
    ap.add_argument("--config", default="")
    ap.add_argument("--run", action="store_true", help="常驻运行")
    ap.add_argument("--console", action="store_true", help="仅启动本地控制台")
    ap.add_argument("--install", action="store_true", help="安装开机自启")
    ap.add_argument("--uninstall", action="store_true", help="移除开机自启")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--scan", action="store_true", help="立即执行一轮扫描归档")
    ap.add_argument("--photo", action="store_true", help="立即截屏一张")
    ap.add_argument("--rules", default="", help="应用规则文件")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    cfg = Config.load()
    log = setup_logging(cfg.home_path, cfg.log_level, cfg.log_max_mb, cfg.log_keep)

    if args.install:
        print(json.dumps(startup_install(cfg.home_path, log), ensure_ascii=False, indent=2))
        return 0
    if args.uninstall:
        print(json.dumps(startup_remove(log), ensure_ascii=False, indent=2))
        return 0
    if args.status:
        rules = RuleSet.load(cfg.rules_file)
        print(json.dumps({
            "device_id": cfg.device_id, "device_name": cfg.device_name,
            "home": cfg.home, "rules_version": rules.version,
            "startup": startup_status(), "captures": str(cfg.capture_dir),
            "devconsole": {"enabled": cfg.devsrv_enabled, "bind": cfg.devsrv_bind,
                           "port": cfg.devsrv_port, "url": cfg.devconsole_url,
                           "static": str(STATIC)},
            "channels": {"ws": bool(cfg.server_url), "gh": bool(cfg.gh_rules_repo)},
        }, ensure_ascii=False, indent=2))
        return 0
    if args.rules:
        incoming = json.loads(Path(args.rules).read_text(encoding="utf-8"))
        rules = RuleSet.load(cfg.rules_file)
        rules.merge(incoming, int(incoming.get("version") or 0))
        rules.save(cfg.rules_file)
        print(f"rules 已更新 version={rules.version}")
        return 0
    if args.scan:
        agent = Agent(cfg)
        print(json.dumps(agent.sync.sync_once(), ensure_ascii=False, indent=2))
        return 0
    if args.photo:
        agent = Agent(cfg)
        print(json.dumps(agent.capture.photo_screen(), ensure_ascii=False, indent=2))
        return 0

    agent = Agent(cfg)
    agent.console_only = bool(args.console)

    def _graceful(*_a):
        agent.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _graceful)
        except Exception:
            pass

    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
