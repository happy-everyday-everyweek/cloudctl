"""agent 入口：装配各子系统，启动本地控制台、局域网互联、声控采集、OTA 与外联通道。

命令行：
    agent --run                 常驻运行（全功能）
    agent --console             仅本地：控制台 + 互联 + 声控采集，不建外联通道
    agent --install / --uninstall / --status
    agent --scan                立即跑一轮扫描归档
    agent --photo               立即截屏一张
    agent --report              立即上报一次（存活上报的即时版）
    agent --storage             查看选盘结果、暂存占用与配额
    agent --rules PATH          应用规则文件
    agent --mesh-peers / --mesh-send ID OP
    agent --cameras / --mic-devices / --audio-level N
    agent --chunk FILE / --merge M.json OUT
    agent --index / --index-search K / --index-build
    agent --update-check        只看有没有新版本
    agent --update-apply        下载并就地替换（会重启自己）

上报有三个时机：启动、按 interval_s 定时（证明设备活着）、退出或关机（同步兜底）。
关机优先走 Windows 控制台事件钩子；拿不到钩子时由退出路径补发一次。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from . import chunker
from .audio import AudioMeter
from .camcap import CameraRecorder, VoiceCameraService
from .capture import CaptureService
from .channel import ControlChannel
from .config import Config
from .desktop import DesktopStreamer, InputInjector
from .devsrv import STATIC, DeviceServer
from .indexer import LocalIndex, RepoIndex
from .mesh import MeshService
from .mirrors import MirrorPool
from .p2p import P2PService
from .router import Router
from .rules import RuleSet, TriggerEngine
from .startup import install as startup_install, remove as startup_remove, status as startup_status
from .storage import StoreManager
from .sync import StateDB, SyncService
from .updater import Updater, UpdateService
from .util import (IS_WINDOWS, foreground_window, idle_seconds, session_is_locked,
                   set_dpi_aware, setup_logging, title_match_any)

VERSION = "1.7.0"
REPORT_MIN_INTERVAL = 30
CONSOLE_SHUTDOWN_EVENTS = (2, 5, 6)   # 关闭窗口 / 注销 / 关机


# ------------------------------------------------------------------ 上报载荷

def _metrics() -> dict:
    """CPU 与内存占用。psutil 缺失时返回空字典，不影响上报本身。"""
    out: dict = {}
    try:
        import psutil  # type: ignore
        out["cpu"] = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        out["mem"] = vm.percent
        out["mem_used_mb"] = int(vm.used / (1024 * 1024))
    except Exception:
        pass
    try:
        import shutil
        du = shutil.disk_usage(str(Path.home()))
        out["disk"] = int(du.used * 100 / du.total)
    except Exception:
        pass
    return out


def build_report(cfg: Config, rules: RuleSet, kind: str, *, version: str = VERSION,
                 uptime_s: int = 0, channel=None, store=None, extra: dict | None = None) -> dict:
    """一条上报就是一条普通事件，走同一条通道，不需要额外的协议端点。"""
    rep = rules.report
    include = bool(cfg.report_include_metrics and rep.get("include_metrics", True))
    data: dict = {
        "kind": kind, "ver": version, "device_id": cfg.device_id, "name": cfg.device_name,
        "group": cfg.group, "rules_v": rules.version, "ts": int(time.time()),
        "uptime_s": int(uptime_s), "pid": os.getpid(),
    }
    if include:
        data["metrics"] = _metrics()
    if channel is not None:
        try:
            data["links"] = {k: bool(v.get("connected")) for k, v in channel.links.items()}
            data["queued"] = channel.queued()
        except Exception:
            pass
    if store is not None:
        try:
            data["storage"] = store.info()
        except Exception:
            pass
    if extra:
        data.update(extra)
    return {"type": "event", "event": "agent.report",
            "id": f"rep-{cfg.device_id}-{int(time.time() * 1000)}", "data": data}


async def _noop_message(_msg: dict):
    return None


async def _noop_rules(_rules: dict, _version: int = 0) -> None:
    return None


class Agent:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.log = setup_logging(cfg.home_path, cfg.log_level, cfg.log_max_mb, cfg.log_keep)
        self.rules = RuleSet.load(cfg.rules_file)
        self.store = StoreManager(cfg, self.log, overrides=lambda: self.rules.data.get("storage") or {})
        self.db = StateDB(cfg.state_db)
        self.index = LocalIndex(cfg.home_path / "index.db")
        self.capture = CaptureService(cfg.capture_dir, self.log)
        self.streamer = DesktopStreamer(self.log)
        self.injector = InputInjector(self.log)
        self.pool = MirrorPool(cfg.home_path, self.log, pool_json=cfg.gh_mirror_pool, top=cfg.gh_mirror_top)
        self.sync = SyncService(self.rules, self.db, self.log, index=self.index,
                                pool=self.pool, device_id=cfg.device_id,
                                staging=cfg.home_path / "chunk_staging")
        self.updater = Updater(cfg, self.rules, self.log, pool=self.pool, current_version=VERSION,
                               exe_path=Path(sys.executable))
        self.updates = UpdateService(self.updater, self.log)
        self.engine = TriggerEngine(self.rules)
        self.camcap: VoiceCameraService | None = None
        self.channel: ControlChannel | None = None
        self.devsrv: DeviceServer | None = None
        self.mesh: MeshService | None = None
        self.p2p: P2PService | None = None
        self.router: Router | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.console_only = False
        self._stop = threading.Event()
        self._recording = False
        self._rec_status: dict = {}
        self._started_ts = time.time()
        self._main_task: asyncio.Task | None = None
        self._loop_thread: threading.Thread | None = None
        self._shutdown_reported = False
        self._ctrl_handler = None
        self._slide_sig: int | None = None
        self._slide_poll_ts = 0.0
        self._blocked_log_ts = 0.0

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
            sender = self.channel.send if self.channel else None
            res = await self.streamer.start(sender, **(msg.get("args") or {}))
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
        self._loop_thread = threading.current_thread()
        self._main_task = asyncio.current_task()
        self.router = Router(
            self.cfg, self.rules, self.capture, self.streamer, self.injector,
            self.sync, self.db, self.log, self.apply_rules, self.updater,
            on_report=self._report_now_sync, report_info=self._report_info,
        )
        self.devsrv = DeviceServer(
            self.cfg, self.rules, self.capture, self.injector, self.sync, self.log,
            get_router=lambda: self.router, get_loop=lambda: self.loop,
            on_report=self._report_now_sync, agent_version=VERSION,
        )
        info = self.devsrv.start()
        self.log.info("设备本地控制台%s %s", "已就绪" if info.get("enabled") else "未启用",
                      self.cfg.devconsole_url)
        self.mesh = MeshService(self.cfg, self.log, run_cmd=self.devsrv.run_cmd, agent_version=VERSION)
        if self.mesh.start().get("enabled"):
            self.log.info("局域网互联 mesh=%s http=%s 发现端口=%s",
                          self.cfg.mesh_scope, self.cfg.mesh_port, self.cfg.mesh_discovery_port)
        self.p2p = P2PService(self.cfg, self.log,
                              on_event=lambda ev: self._emit_threadsafe({"type": "event", **ev}),
                              relay_send=self._p2p_relay)
        pinfo = self.p2p.start()
        if pinfo.get("enabled"):
            self.log.info("P2P 直连层已启动，候选端点 %s", pinfo.get("candidates"))
        self.camcap = VoiceCameraService(self.cfg, self.rules, self.engine, self.log,
                                         on_event=self._emit_threadsafe,
                                         capture_dir=self.cfg.capture_dir / "camera")
        cinfo = self.camcap.start()
        if cinfo.get("enabled"):
            self.log.info("摄像头声控采集已启动，声卡引擎=%s", cinfo.get("audio") or "未启用")
        if self.updates.start().get("enabled"):
            self.log.info("OTA 已启用，当前版本 %s", VERSION)
        self.log.info("cloudctl agent %s 启动 device=%s home=%s rules_v=%s",
                      VERSION, self.cfg.device_id, self.cfg.home, self.rules.version)
        sinfo = self.store.info()
        self.log.info("工作目录 %s（%s），待上传 %s / 上限 %s，日志 %s",
                      sinfo["home"], ("自动选盘 " + self.cfg.picked_drive) if self.cfg.picked_drive else "按配置",
                      sinfo["pending_h"], sinfo["buffer_max_h"], sinfo["logs_h"])
        self.store.enforce()
        threading.Thread(target=self._trigger_loop, name="trigger", daemon=True).start()
        threading.Thread(target=self._scan_loop, name="scan", daemon=True).start()
        if self.console_only:
            self.log.info("仅本地模式：不建立外联通道")
            while not self._stop.is_set():
                await asyncio.sleep(0.5)
            return
        self.channel = ControlChannel(self.cfg, self.on_message, self.apply_rules, self.log,
                                      version=VERSION)
        self._install_console_handler()
        await self._emit({"type": "event", "event": "agent.online",
                          "data": {"ver": VERSION, "rules": self.rules.version}})
        if self.report_enabled("on_start"):
            await self._emit(self.report_payload("start"))
        threading.Thread(target=self._report_loop, name="report", daemon=True).start()
        try:
            await self.channel.run()
        finally:
            self._stop.set()
            self._shutdown_report("exit")

    def _p2p_relay(self, peer_id: str, blob: bytes) -> bool:
        """直连不通时的兜底：交给网格里对端可达的邻居转一手。没有能力就老实返回失败。"""
        mesh = self.mesh
        if mesh is None:
            return False
        fn = getattr(mesh, "send_blob", None) or getattr(mesh, "relay_blob", None)
        if fn is None:
            return False
        try:
            res = fn(peer_id, f"p2p-{int(time.time() * 1000)}.bin", blob)
            if isinstance(res, dict) and res.get("ok") is False:
                return False
            return bool(res)
        except Exception as e:
            self.log.debug("P2P 中继失败：%s", e)
            return False

    async def _emit(self, obj: dict) -> None:
        if self.channel:
            await self.channel.send(obj)

    def _emit_threadsafe(self, obj: dict) -> None:
        if self.channel is None or self.loop is None:
            return
        asyncio.run_coroutine_threadsafe(self.channel.send(obj), self.loop)

    # ------------------------------------------------------------ 上报
    def report_rule(self) -> dict:
        return self.rules.report

    def report_enabled(self, key: str = "enabled") -> bool:
        """配置与规则任一关闭即不上报；规则侧只能收紧，和权限开关同一口径。"""
        if not self.cfg.report_enabled:
            return False
        rule = self.report_rule()
        if key != "enabled" and not rule.get("enabled", True):
            return False
        return bool(rule.get(key, True))

    def report_interval(self) -> float:
        rule = self.report_rule()
        try:
            n = int(rule.get("interval_s") or self.cfg.report_interval_s)
        except Exception:
            n = int(self.cfg.report_interval_s)
        return float(max(REPORT_MIN_INTERVAL, n))

    def report_payload(self, kind: str, extra: dict | None = None) -> dict:
        merged = dict(extra or {})
        if self.p2p is not None:
            merged.setdefault("p2p", self.p2p.status())
        return build_report(self.cfg, self.rules, kind, version=VERSION,
                            uptime_s=int(time.time() - self._started_ts),
                            channel=self.channel, store=self.store, extra=merged or None)

    def _report_loop(self) -> None:
        self.log.info("存活上报已启动：间隔 %ss（规则可覆盖）", self.report_interval())
        while not self._stop.is_set():
            deadline = time.time() + self.report_interval()
            while not self._stop.is_set() and time.time() < deadline:
                time.sleep(0.5)
            if self._stop.is_set():
                break
            if not self.report_enabled():
                self.log.debug("上报已关闭，跳过本轮")
                continue
            self._emit_threadsafe(self.report_payload("alive"))

    def _report_info(self) -> dict:
        rule = self.report_rule()
        return {"enabled": self.report_enabled(), "interval_s": self.report_interval(),
                "on_start": self.report_enabled("on_start"),
                "on_shutdown": self.report_enabled("on_shutdown"),
                "shutdown_wait_s": int(self.cfg.report_shutdown_wait_s),
                "include_metrics": bool(self.cfg.report_include_metrics),
                "uptime_s": int(time.time() - self._started_ts),
                "online": bool(self.channel.online) if self.channel else False,
                "queued": self.channel.queued() if self.channel else {},
                "rules": rule}

    def _report_now_sync(self) -> dict:
        """同步上报一次：本地控制台按钮、云端命令、关机钩子都走这里。"""
        if not self.report_enabled():
            return {"ok": False, "err": "上报已关闭", "sent": False}
        return self._flush(self.report_payload("manual"))

    def _flush(self, payload: dict, timeout: float | None = None) -> dict:
        ch = self.channel
        if ch is None:
            return {"ok": False, "err": "未建立外联通道", "sent": False}
        wait = float(self.cfg.report_shutdown_wait_s if timeout is None else timeout)
        if threading.current_thread() is self._loop_thread:
            # 事件循环线程里不能再跑 asyncio.run，交给独立线程并等它一会儿
            box: dict = {}
            t = threading.Thread(target=lambda: box.update(ch.flush_sync(payload, wait)), daemon=True)
            t.start()
            t.join(timeout=wait + 2)
            res = box or {"ok": False, "err": "上报超时", "sent": False}
        else:
            res = ch.flush_sync(payload, wait)
        ok = bool(res.get("ws") or res.get("gh"))
        res["ok"] = ok
        res["sent"] = ok
        if not ok:
            res["err"] = "两条通道都不可达，已落盘待补发"
        return res

    def _shutdown_report(self, kind: str = "shutdown") -> None:
        if self._shutdown_reported:
            return
        self._shutdown_reported = True
        if not self.report_enabled("on_shutdown"):
            return
        try:
            res = self._flush(self.report_payload(kind))
            self.log.info("%s 上报结果：%s", kind, res)
        except Exception as e:
            self.log.warning("关机上报异常：%s", e)

    def _install_console_handler(self) -> None:
        """Windows 关机/注销时会送控制台事件，这是关机上报的主要触发点。"""
        if not IS_WINDOWS:
            return
        try:
            import ctypes
            from ctypes import wintypes

            handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

            def _handler(evt: int) -> bool:
                if evt in CONSOLE_SHUTDOWN_EVENTS:
                    try:
                        self._shutdown_report("shutdown_signal")
                    except Exception:
                        pass
                else:
                    self._stop.set()
                return True

            self._ctrl_handler = handler_type(_handler)
            if not ctypes.windll.kernel32.SetConsoleCtrlHandler(self._ctrl_handler, True):
                raise OSError("SetConsoleCtrlHandler 返回失败")
            self.log.info("已挂接关机/注销钩子，关机时会先发一次上报")
        except Exception as e:
            self._ctrl_handler = None
            self.log.warning("无法挂接关机钩子（无控制台或被系统限制）：%s；改由退出路径补发", e)

    # ------------------------------------------------------------ 自动采集
    def _trigger_loop(self) -> None:
        self.log.info("采集调度已启动")
        while not self._stop.is_set():
            try:
                tick = 5.0
                allow, why = self.store.can_write()
                if not allow:
                    now = time.time()
                    if now - self._blocked_log_ts > 300:
                        self._blocked_log_ts = now
                        self.log.warning("暂停采集：%s", why)
                        self._emit_threadsafe({"type": "event", "event": "storage.blocked",
                                               "data": {"reason": why, **self.store.info()}})
                    time.sleep(30)
                    continue
                title, _pid = foreground_window()
                idle = idle_seconds()
                locked = session_is_locked()
                slide = self._slide_changed(title)
                decision = self.engine.decide(title, idle, locked, slide_changed=slide)
                if decision["video_stop"] and self._recording:
                    self._recording = False
                    self._emit_threadsafe({"type": "event", "event": "capture.video_stop",
                                           "data": {"reason": decision["reason"]}})
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

    def _slide_changed(self, title: str) -> bool:
        """只有当前台是幻灯片放映窗口时才取样，避免白花抓屏开销。"""
        sl = self.rules.slideshow
        if not sl.get("enabled"):
            return False
        if not title or not title_match_any(title, sl.get("titles") or []):
            self._slide_sig = None
            return False
        now = time.time()
        if now - self._slide_poll_ts < float(sl.get("poll_s") or 4):
            return False
        self._slide_poll_ts = now
        monitor = max(0, int(self.rules.desktop.get("monitors", 1) - 1))
        try:
            changed, sig, _info = self.capture.is_slide_changed(
                self._slide_sig, monitor, int(sl.get("threshold") or 12))
        except Exception as e:
            self.log.debug("翻页检测取样失败：%s", e)
            return False
        first_sample = self._slide_sig is None
        self._slide_sig = sig
        if first_sample:
            return False
        if changed:
            self.engine.note_slide()
        return bool(changed)

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

        def _should_stop() -> bool:
            return self._stop.is_set() or not self._recording

        def _job() -> None:
            try:
                info = self.capture.record(seconds=seconds, fps=fps, quality=quality,
                                           monitor=monitor, should_stop=_should_stop)
                self.engine.note_video_bytes(int(info.get("bytes") or 0))
                self._rec_status = info
                self._emit_threadsafe({"type": "event", "event": "capture.video_done",
                                       "data": {**info, "reason": reason, "title": title}})
                self.log.info("段录像完成 %s（%s）", info.get("path"), info.get("size_h"))
            except Exception as e:
                self.log.warning("录制失败：%s", e)
            finally:
                self._recording = False
                self.engine.note_video_stop()

        threading.Thread(target=_job, name="recorder", daemon=True).start()
        self._emit_threadsafe({"type": "event", "event": "capture.video_start",
                               "data": {"seconds": seconds, "fps": fps, "reason": reason}})

    # ------------------------------------------------------------ 归档调度
    def _scan_loop(self) -> None:
        while not self._stop.is_set():
            interval = max(1, int(self.rules.scan.get("interval_min") or 30))
            time.sleep(interval * 60)
            if self._stop.is_set():
                break
            if not (self.rules.upload.get("enabled") or self.rules.index.get("enabled")):
                continue
            try:
                stats = self.sync.sync_once()
                self._emit_threadsafe({"type": "event", "event": "sync.done", "data": stats})
            except Exception as e:
                self.log.warning("归档异常：%s", e)
            pruned = self.store.enforce()
            if pruned.get("dropped") or pruned.get("logs_removed"):
                self.log.warning("存储清理：丢弃 %s 个文件、删除 %s 个旧日志",
                                 pruned.get("dropped"), pruned.get("logs_removed"))
                self._emit_threadsafe({"type": "event", "event": "storage.pruned", "data": pruned})

    def stop(self) -> None:
        self._stop.set()
        self._shutdown_report("shutdown")
        loop, task = self.loop, self._main_task
        if loop is not None and task is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except Exception:
                pass
        for svc in (self.devsrv, self.mesh, self.p2p, self.camcap, self.updates):
            if svc is not None:
                try:
                    svc.stop()
                except Exception:
                    pass


# ------------------------------------------------------------------ CLI

def _selftest() -> int:
    ok = []
    for mod in ("websockets", "requests", "PIL", "mss", "cv2", "psutil", "pynput", "numpy", "sounddevice"):
        try:
            __import__(mod)
            ok.append(f"{mod}: ok")
        except Exception as e:
            ok.append(f"{mod}: 缺失（{e}）")
    static_ok = (STATIC / "index.html").exists() and (STATIC / "console.js").exists()
    ok.append(f"devstatic: {'ok' if static_ok else '缺失'}")
    ok.append(f"cameras: {CameraRecorder.list_cameras()}")
    ok.append(f"frozen: {bool(getattr(sys, 'frozen', False))}")
    print("\n".join(ok))
    return 0


def _mesh_probe(cfg: Config, seconds: int, send: list[str] | None = None) -> int:
    log = setup_logging(cfg.home_path, cfg.log_level, cfg.log_max_mb, cfg.log_keep)
    mesh = MeshService(cfg, log, run_cmd=lambda op, args, timeout=60.0: {"ok": False, "err": "探测模式不执行命令"})
    mesh.start()
    deadline = time.time() + max(1, seconds)
    while time.time() < deadline:
        time.sleep(0.5)
    peers = mesh.peers_list()
    if send:
        target, op = send[0], send[1]
        args = json.loads(send[2]) if len(send) > 2 and send[2] else {}
        print(json.dumps({"peers": peers, "result": mesh.send_cmd(target, op, args)},
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"mesh": cfg.mesh_scope, "peers": peers}, ensure_ascii=False, indent=2))
    mesh.stop()
    return 0


def _report_once(cfg: Config, log) -> int:
    """--report：不建常驻循环，同步发一次就走，方便验证通道是否通。"""
    rules = RuleSet.load(cfg.rules_file)
    if not cfg.report_enabled or not rules.report.get("enabled", True):
        print(json.dumps({"ok": False, "err": "上报已关闭（config.report_enabled 或 rules.report.enabled）"},
                         ensure_ascii=False, indent=2))
        return 1
    ch = ControlChannel(cfg, _noop_message, _noop_rules, log, version=VERSION)
    payload = build_report(cfg, rules, "manual", version=VERSION, channel=ch,
                           store=StoreManager(cfg, log, overrides=lambda: rules.data.get("storage") or {}))
    res = ch.flush_sync(payload, float(cfg.report_shutdown_wait_s))
    res["ok"] = bool(res.get("ws") or res.get("gh"))
    if not res["ok"]:
        res["err"] = "两条通道都不可达，已落盘待补发"
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["ok"] else 1


def _p2p_probe(cfg: Config, log, args) -> int:
    """--p2p-ping ID=IP:PORT / --p2p-push ID=IP:PORT FILE：手动验证打洞与传输。"""
    svc = P2PService(cfg, log)
    info = svc.start()
    if not info.get("enabled"):
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 1
    target = args.p2p_ping or (args.p2p_push[0] if args.p2p_push else "")
    out: dict = {"self": svc.status()}
    if target:
        peer_id, _, addr = target.partition("=")
        host, _, port = addr.partition(":")
        out["punch"] = svc.add_peer(peer_id, [(host, int(port or cfg.p2p_port))])
        for _ in range(20):
            time.sleep(0.3)
            if svc.path_state(peer_id) == "direct":
                break
        out["state"] = svc.path_state(peer_id)
    if args.p2p_push:
        out["push"] = svc.push_file(target.split("=")[0], Path(args.p2p_push[1]))
    out["after"] = svc.status()
    svc.stop()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _audio_probe(cfg: Config, seconds: int) -> int:
    rules = RuleSet.load(cfg.rules_file)
    au = rules.audio
    meter = AudioMeter(int(au.get("device") or -1), int(au.get("sample_rate") or 16000),
                       int(au.get("block_ms") or 100))
    if not meter.open():
        print(json.dumps({"ok": False, "err": meter.error}, ensure_ascii=False, indent=2))
        return 1
    peak = -100.0
    samples: list[float] = []
    deadline = time.time() + max(1, seconds)
    while time.time() < deadline:
        db = meter.read_db()
        if db is not None:
            peak = max(peak, db)
            samples.append(db)
    meter.close()
    avg = sum(samples) / len(samples) if samples else -100.0
    print(json.dumps({"ok": True, "engine": meter.engine, "device": au.get("device"),
                      "blocks": len(samples), "avg_db": round(avg, 1), "peak_db": round(peak, 1),
                      "threshold_db": au.get("threshold_db")}, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser("cloudctl-agent")
    ap.add_argument("--config", default="")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--console", action="store_true", help="仅本地：控制台 + 互联 + 声控采集")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--photo", action="store_true")
    ap.add_argument("--report", action="store_true", help="立即上报一次（存活上报的即时版）")
    ap.add_argument("--storage", action="store_true", help="查看选盘结果、暂存占用与配额")
    ap.add_argument("--p2p-status", action="store_true", help="看直连层候选端点与已建立路径")
    ap.add_argument("--p2p-ping", default="", help="对指定设备打洞，格式 ID=IP:PORT")
    ap.add_argument("--p2p-push", nargs=2, default=None, help="P2P 推一个文件：ID=IP:PORT FILE")
    ap.add_argument("--rules", default="")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--mesh-peers", action="store_true")
    ap.add_argument("--mesh-wait", type=int, default=8)
    ap.add_argument("--mesh-send", nargs="+", default=None)
    ap.add_argument("--cameras", action="store_true")
    ap.add_argument("--mic-devices", action="store_true")
    ap.add_argument("--audio-level", type=int, default=0)
    ap.add_argument("--chunk", default="")
    ap.add_argument("--chunk-mb", type=float, default=40.0)
    ap.add_argument("--merge", nargs=2, default=None)
    ap.add_argument("--index", action="store_true")
    ap.add_argument("--index-build", action="store_true")
    ap.add_argument("--index-search", default="")
    ap.add_argument("--update-check", action="store_true", help="检查是否有新版本")
    ap.add_argument("--update-apply", action="store_true", help="下载并替换自身")
    ap.add_argument("--update-tag", default="", help="指定要升到的 tag")
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
    if args.update_check or args.update_apply:
        rules = RuleSet.load(cfg.rules_file)
        pool = MirrorPool(cfg.home_path, log, pool_json=cfg.gh_mirror_pool, top=cfg.gh_mirror_top)
        if args.update_tag:
            rules.data.setdefault("update", {})
            rules.data["update"]["channel"] = "tag"
            rules.data["update"]["tag"] = args.update_tag
        up = Updater(cfg, rules, log, pool=pool, current_version=VERSION, exe_path=Path(sys.executable))
        if args.update_check:
            print(json.dumps(up.check(), ensure_ascii=False, indent=2))
            return 0
        res = up.run_once(apply_if_newer=True)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    if args.mesh_peers:
        return _mesh_probe(cfg, args.mesh_wait)
    if args.mesh_send:
        return _mesh_probe(cfg, args.mesh_wait, args.mesh_send)
    if args.cameras:
        print(json.dumps({"cameras": CameraRecorder.list_cameras()}, ensure_ascii=False, indent=2))
        return 0
    if args.mic_devices:
        print(json.dumps({"devices": AudioMeter().devices()}, ensure_ascii=False, indent=2))
        return 0
    if args.audio_level:
        return _audio_probe(cfg, args.audio_level)
    if args.chunk:
        manifest = chunker.split(Path(args.chunk), cfg.home_path / "chunk_staging", size_mb=args.chunk_mb)
        print(json.dumps({k: manifest[k] for k in ("original", "bytes", "sha256", "manifest_path")},
                         ensure_ascii=False, indent=2))
        print(f"分片数：{len(manifest['parts'])}")
        return 0
    if args.merge:
        print(json.dumps(chunker.merge(Path(args.merge[0]), Path(args.merge[1])),
                         ensure_ascii=False, indent=2))
        return 0
    if args.index or args.index_build or args.index_search:
        idx = LocalIndex(cfg.home_path / "index.db")
        if args.index_search:
            print(json.dumps({"hits": idx.search(args.index_search)}, ensure_ascii=False, indent=2))
        elif args.index_build:
            out = cfg.home_path / "index.export.json"
            writer = RepoIndex(idx, device_id=cfg.device_id, branch=cfg.upload_branch)
            out.write_text(writer.render(writer.build()), encoding="utf-8")
            print(json.dumps({"ok": True, "path": str(out), "summary": idx.summary()},
                             ensure_ascii=False, indent=2))
        else:
            print(json.dumps(idx.summary(), ensure_ascii=False, indent=2))
        return 0
    if args.status:
        rules = RuleSet.load(cfg.rules_file)
        idx = LocalIndex(cfg.home_path / "index.db")
        up = Updater(cfg, rules, log, current_version=VERSION, exe_path=Path(sys.executable))
        print(json.dumps({
            "version": VERSION,
            "device_id": cfg.device_id, "device_name": cfg.device_name,
            "home": cfg.home, "rules_version": rules.version,
            "startup": startup_status(), "captures": str(cfg.capture_dir),
            "devconsole": {"enabled": cfg.devsrv_enabled, "bind": cfg.devsrv_bind,
                           "port": cfg.devsrv_port, "url": cfg.devconsole_url,
                           "static": str(STATIC)},
            "mesh": {"enabled": cfg.mesh_enabled, "scope": cfg.mesh_scope,
                     "port": cfg.mesh_port, "discovery_port": cfg.mesh_discovery_port,
                     "accept_cmd": cfg.mesh_accept_cmd, "relay": cfg.mesh_relay,
                     "max_hops": cfg.mesh_max_hops},
            "p2p": {"enabled": cfg.p2p_enabled, "bind": cfg.p2p_bind, "port": cfg.p2p_port,
                    "token_set": cfg.p2p_has_explicit_token, "keepalive_s": cfg.p2p_keepalive_s,
                    "public_host": cfg.p2p_public_host},
            "channels": {"ws": bool(cfg.server_url), "gh": bool(cfg.gh_rules_repo),
                         "mirror_top": cfg.gh_mirror_top},
            "capture": {"camera": rules.camera.get("enabled"),
                        "audio_trigger": rules.audio.get("enabled"),
                        "threshold_db": rules.audio.get("threshold_db"),
                        "chunk_mb": rules.chunk.get("size_mb")},
            "report": {"enabled": cfg.report_enabled, "interval_s": cfg.report_interval_s,
                       "on_start": cfg.report_on_start, "on_shutdown": cfg.report_on_shutdown,
                       "shutdown_wait_s": cfg.report_shutdown_wait_s,
                       "include_metrics": cfg.report_include_metrics, "rules": rules.report},
            "storage": StoreManager(cfg, log, overrides=lambda: rules.data.get("storage") or {}).info(),
            "ota": up.status(),
            "index": idx.summary(),
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
    if args.storage:
        rules = RuleSet.load(cfg.rules_file)
        st = StoreManager(cfg, log, overrides=lambda: rules.data.get("storage") or {})
        allow, why = st.can_write()
        print(json.dumps({"info": st.info(), "logs": st.prune_logs(),
                          "can_write": allow, "reason": why}, ensure_ascii=False, indent=2))
        return 0
    if args.p2p_status or args.p2p_ping or args.p2p_push:
        return _p2p_probe(cfg, log, args)
    if args.report:
        return _report_once(cfg, log)

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
    except asyncio.CancelledError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
