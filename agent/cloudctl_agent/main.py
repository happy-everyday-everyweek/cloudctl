"""agent 入口：装配各子系统，启动本地控制台、局域网互联、声控采集、OTA 与外联通道。

命令行：
    agent --run                 常驻运行（全功能）
    agent --console             仅本地：控制台 + 互联 + 声控采集，不建外联通道
    agent --install / --uninstall / --status
    agent --scan                立即跑一轮扫描归档
    agent --photo               立即截屏一张
    agent --rules PATH          应用规则文件
    agent --mesh-peers / --mesh-send ID OP
    agent --cameras / --mic-devices / --audio-level N
    agent --chunk FILE / --merge M.json OUT
    agent --index / --index-search K / --index-build
    agent --update-check        只看有没有新版本
    agent --update-apply        下载并就地替换（会重启自己）
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
from .router import Router
from .rules import RuleSet, TriggerEngine
from .startup import install as startup_install, remove as startup_remove, status as startup_status
from .sync import StateDB, SyncService
from .updater import Updater, UpdateService
from .util import IS_WINDOWS, foreground_window, idle_seconds, session_is_locked, set_dpi_aware, setup_logging

VERSION = "1.5.0"


class Agent:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.log = setup_logging(cfg.home_path, cfg.log_level, cfg.log_max_mb, cfg.log_keep)
        self.rules = RuleSet.load(cfg.rules_file)
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
        self.router = Router(
            self.cfg, self.rules, self.capture, self.streamer, self.injector,
            self.sync, self.db, self.log, self.apply_rules, self.updater,
        )
        self.devsrv = DeviceServer(
            self.cfg, self.rules, self.capture, self.injector, self.sync, self.log,
            get_router=lambda: self.router, get_loop=lambda: self.loop,
        )
        info = self.devsrv.start()
        self.log.info("设备本地控制台%s %s", "已就绪" if info.get("enabled") else "未启用",
                      self.cfg.devconsole_url)
        self.mesh = MeshService(self.cfg, self.log, run_cmd=self.devsrv.run_cmd, agent_version=VERSION)
        if self.mesh.start().get("enabled"):
            self.log.info("局域网互联 mesh=%s http=%s 发现端口=%s",
                          self.cfg.mesh_scope, self.cfg.mesh_port, self.cfg.mesh_discovery_port)
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
        threading.Thread(target=self._trigger_loop, name="trigger", daemon=True).start()
        threading.Thread(target=self._scan_loop, name="scan", daemon=True).start()
        if self.console_only:
            self.log.info("仅本地模式：不建立外联通道")
            while not self._stop.is_set():
                await asyncio.sleep(0.5)
            return
        self.channel = ControlChannel(self.cfg, self.on_message, self.apply_rules, self.log)
        await self._emit({"type": "event", "event": "agent.online",
                          "data": {"ver": VERSION, "rules": self.rules.version}})
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
            if not self.rules.upload.get("enabled"):
                continue
            try:
                stats = self.sync.sync_once()
                self._emit_threadsafe({"type": "event", "event": "sync.done", "data": stats})
            except Exception as e:
                self.log.warning("归档异常：%s", e)

    def stop(self) -> None:
        self._stop.set()
        for svc in (self.devsrv, self.mesh, self.camcap, self.updates):
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
            "channels": {"ws": bool(cfg.server_url), "gh": bool(cfg.gh_rules_repo),
                         "mirror_top": cfg.gh_mirror_top},
            "capture": {"camera": rules.camera.get("enabled"),
                        "audio_trigger": rules.audio.get("enabled"),
                        "threshold_db": rules.audio.get("threshold_db"),
                        "chunk_mb": rules.chunk.get("size_mb")},
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
