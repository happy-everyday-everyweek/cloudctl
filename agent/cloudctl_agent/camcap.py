"""摄像头采集：声音阀值触发的自动录像（修复：静音能真正停录）。

流程：麦克风每 block_ms 读一次电平，连续超过 threshold_db 达 attack_s 即开始录像，
静音保持 hold_s 则停录——停录通过 should_stop 回调传给录制器，会真的提前收尾，
而不是只发个事件等它录满整段。超过 segment_s 自动换段，配额走 TriggerEngine。

声卡不可用时退化为按 cooldown_s 的周期录制，并在日志里写明原因。
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .audio import AudioGate, AudioMeter
from .util import human_size


class CameraRecorder:
    """摄像头分段录制（单段 mp4）。"""

    def __init__(self, out_dir: Path, log) -> None:
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.log = log

    @staticmethod
    def list_cameras(max_index: int = 5) -> list[int]:
        try:
            import cv2  # type: ignore
        except Exception:
            return []
        found: list[int] = []
        for i in range(max(1, max_index)):
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW if hasattr(cv2, "CAP_DSHOW") else 0)
            if cap is not None and cap.isOpened():
                ok, _frame = cap.read()
                if ok:
                    found.append(i)
                cap.release()
        return found

    def record(self, seconds: float, fps: int = 15, size: tuple[int, int] = (1280, 720),
               index: int = 0, quality: int = 70, audio_track: bool = False,
               audio_device: int = -1, tag: str = "voice",
               should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
        import cv2  # type: ignore

        name = f"cam{index}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{tag}.mp4"
        dest = self.out_dir / name
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW if hasattr(cv2, "CAP_DSHOW") else 0)
        if not cap.isOpened():
            raise RuntimeError(f"摄像头 {index} 打不开")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(size[0]))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(size[1]))
        writer = cv2.VideoWriter(str(dest), cv2.VideoWriter_fourcc(*"mp4v"), float(max(1, fps)),
                                 (int(size[0]), int(size[1])))
        frames = 0
        started = time.time()
        interval = 1.0 / float(max(1, fps))
        early = False
        try:
            while time.time() - started < seconds:
                if should_stop is not None and should_stop():
                    early = True
                    break
                ok, frame = cap.read()
                if not ok:
                    break
                writer.write(frame)
                frames += 1
                drift = started + frames * interval - time.time()
                if drift > 0:
                    time.sleep(min(drift, 0.5))
        finally:
            writer.release()
            cap.release()
        duration = round(time.time() - started, 1)
        if audio_track:
            self._mux_audio(dest, duration, audio_device)
        size_bytes = dest.stat().st_size if dest.exists() else 0
        return {"path": str(dest), "bytes": size_bytes, "size_h": human_size(size_bytes),
                "seconds": duration, "frames": frames, "kind": "camera", "stopped_early": early}

    def _mux_audio(self, video: Path, seconds: float, device: int) -> None:
        exe = shutil.which("ffmpeg")
        if not exe:
            self.log.warning("未找到 ffmpeg，忽略 audio_track")
            return
        tmp = video.with_name(video.stem + "_audio.wav")
        try:
            subprocess.run([exe, "-y", "-f", "dshow",
                            "-i", f"audio={device}" if device >= 0 else "audio=default",
                            "-t", str(int(max(1, seconds))), str(tmp)],
                           capture_output=True, timeout=int(seconds) + 20)
            if tmp.exists():
                muxed = video.with_name(video.stem + "_mux.mp4")
                subprocess.run([exe, "-y", "-i", str(video), "-i", str(tmp), "-c:v", "copy",
                                "-c:a", "aac", "-shortest", str(muxed)],
                               capture_output=True, timeout=int(seconds) + 30)
                if muxed.exists():
                    muxed.replace(video)
        except Exception as e:
            self.log.warning("音视频合并失败：%s", e)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass


class VoiceCameraService:
    """声控录像服务：一个后台线程，一个麦克风，一个摄像头。"""

    def __init__(self, cfg, rules, engine, log,
                 on_event: Callable[[dict], None] | None = None,
                 capture_dir: Path | None = None) -> None:
        self.cfg = cfg
        self.rules = rules
        self.engine = engine
        self.log = log
        self.on_event = on_event or (lambda obj: None)
        base = Path(capture_dir) if capture_dir else (cfg.capture_dir / "camera")
        self.recorder = CameraRecorder(base, log)
        self.meter: AudioMeter | None = None
        self.gate: AudioGate | None = None
        self._stop = threading.Event()
        self._stop_seg = threading.Event()
        self._thread: threading.Thread | None = None
        self.recording = False
        self.stats: dict[str, Any] = {"segments": 0, "bytes": 0, "last_reason": "",
                                      "last_db": -100.0, "peak_db": -100.0,
                                      "audio_engine": "", "camera": -1, "errors": 0}

    # --- 生命周期 ---
    def start(self) -> dict:
        cm = self.rules.camera
        if not cm.get("enabled"):
            return {"enabled": False}
        if self._thread is not None:
            return {"enabled": True, "already": True}
        self.stats["camera"] = int(cm.get("index") or 0)
        self._prepare_audio()
        self._thread = threading.Thread(target=self._loop, name="camcap", daemon=True)
        self._thread.start()
        self.log.info("摄像头采集已启动 cam=%s 声控=%s", cm.get("index"),
                      "开" if (self.meter and self.meter.available) else "关")
        return {"enabled": True, "audio": (self.meter.engine if self.meter else "")}

    def stop(self) -> dict:
        self._stop.set()
        self._stop_seg.set()
        if self.meter is not None:
            self.meter.close()
        self.recording = False
        return {"stopped": True}

    def status(self) -> dict:
        au = self.rules.audio
        return {"enabled": bool(self.rules.camera.get("enabled")), "recording": self.recording,
                "camera_index": self.stats["camera"], "audio_engine": self.stats["audio_engine"],
                "audio_enabled": bool(au.get("enabled")), "threshold_db": au.get("threshold_db"),
                "db": round(self.stats["last_db"], 1), "peak_db": round(self.stats["peak_db"], 1),
                "segments": self.stats["segments"], "bytes": self.stats["bytes"],
                "errors": self.stats["errors"], "last_reason": self.stats["last_reason"],
                "quota_mb": round(self.engine.state.camera_mb, 1)}

    # --- 内部 ---
    def _prepare_audio(self) -> None:
        au = self.rules.audio
        if not au.get("enabled"):
            return
        self.meter = AudioMeter(int(au.get("device") or -1), int(au.get("sample_rate") or 16000),
                                int(au.get("block_ms") or 100))
        if not self.meter.open():
            self.log.warning("声卡不可用（%s），退化为周期录制", self.meter.error)
            self.meter = None
            return
        self.stats["audio_engine"] = self.meter.engine
        self.gate = AudioGate(float(au.get("threshold_db") or -35), float(au.get("attack_s") or 0.3),
                              float(au.get("hold_s") or 3), int(au.get("block_ms") or 100))

    def _loop(self) -> None:
        started = 0.0
        while not self._stop.is_set():
            try:
                au = self.rules.audio
                cm = self.rules.camera
                segment_s = float(cm.get("segment_s") or 120)
                min_s = float(cm.get("min_segment_s") or 5)
                block_s = max(0.05, float(au.get("block_ms") or 100) / 1000.0)
                now = time.time()

                if self.gate is not None and self.meter is not None:
                    db = self.meter.read_db()
                    if db is None:
                        self.stats["errors"] += 1
                        self._stop.wait(block_s)   # 读不到电平也不能空转
                        continue
                    self.stats["last_db"] = db
                    self.stats["peak_db"] = max(float(self.stats["peak_db"]), db)
                    action = self.gate.feed(db)
                    if action == "start" and not self.recording:
                        ok, why = self.engine.camera_allowed()
                        if ok:
                            self._start("voice", float(au.get("threshold_db") or -35))
                            started = time.time()
                        else:
                            self.stats["last_reason"] = why
                    if self.recording and action == "stop":
                        # 修复点：静音就真的停录
                        if (now - started) >= min_s:
                            self._stop_seg.set()
                            self._stop_rec("silence", round(now - started, 1))
                        else:
                            self.stats["last_reason"] = "too_short_ignored"
                    if self.recording and (now - started) >= segment_s:
                        self._stop_seg.set()
                        self._stop_rec("segment_end", round(now - started, 1))
                        if self.gate.speaking and self.engine.camera_allowed()[0]:
                            self._start("voice_continue", float(au.get("threshold_db") or -35))
                            started = time.time()
                    continue

                # 无声控：按段周期录制
                if not self.recording:
                    ok, why = self.engine.camera_allowed()
                    if ok:
                        self._start("interval", 0.0)
                        started = time.time()
                    else:
                        self.stats["last_reason"] = why
                        self._stop.wait(2.0)
                elif (now - started) >= segment_s or self._stop.is_set():
                    self._stop_seg.set()
                    self._stop_rec("segment_end", round(now - started, 1))
                    self._stop.wait(1.0)
            except Exception as e:
                self.stats["errors"] += 1
                self.log.warning("摄像头采集异常：%s", e)
                self._stop.wait(2.0)

    def _start(self, reason: str, threshold_db: float) -> None:
        cm = self.rules.camera
        self.recording = True
        self._stop_seg = threading.Event()
        self.engine.note_camera_start()
        if self.gate is not None:
            self.engine.note_audio_start()
        self.stats["last_reason"] = reason
        self.stats["peak_db"] = -100.0
        seg_event = self._stop_seg
        self.on_event({"type": "event", "event": "camera.start",
                       "data": {"reason": reason, "threshold_db": threshold_db,
                                "device": cm.get("index"), "segment_s": cm.get("segment_s")}})

        def _should_stop() -> bool:
            return self._stop.is_set() or seg_event.is_set()

        def _job() -> None:
            seg = float(cm.get("segment_s") or 120)
            try:
                info = self.recorder.record(
                    seconds=seg, fps=int(cm.get("fps") or 15),
                    size=(int(cm.get("width") or 1280), int(cm.get("height") or 720)),
                    index=int(cm.get("index") or 0), quality=int(cm.get("quality") or 70),
                    audio_track=bool(cm.get("audio_track")),
                    audio_device=int(self.rules.audio.get("device") or -1),
                    tag="voice" if self.gate is not None else "interval",
                    should_stop=_should_stop)
                self.stats["segments"] += 1
                self.stats["bytes"] += int(info.get("bytes") or 0)
                self.engine.note_camera_bytes(int(info.get("bytes") or 0))
                self.on_event({"type": "event", "event": "camera.done",
                               "data": {**info, "reason": reason}})
                self.log.info("摄像头段完成 %s（%s，%s 秒）", info.get("path"),
                              info.get("size_h"), info.get("seconds"))
            except Exception as e:
                self.stats["errors"] += 1
                self.log.warning("摄像头录制失败：%s", e)
                self.on_event({"type": "event", "event": "camera.error", "data": {"err": str(e)}})

        threading.Thread(target=_job, name="camcap-seg", daemon=True).start()

    def _stop_rec(self, reason: str, seconds: float) -> None:
        self.recording = False
        self.on_event({"type": "event", "event": "camera.stop",
                       "data": {"reason": reason, "seconds": seconds,
                                "db": round(self.stats["last_db"], 1),
                                "peak_db": round(self.stats["peak_db"], 1)}})
