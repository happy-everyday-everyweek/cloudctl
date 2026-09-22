"""采集层：屏幕 / 摄像头拍照、屏幕录制、幻灯片翻页感知。

录屏优先用 ffmpeg gdigrab，拿不到时回退逐帧抓屏 + OpenCV 写盘。
本次修复：record 支持 should_stop 回调，声控/翻页这类触发可以中途停录；
ffmpeg 分支改用 frag_keyframe 输出，中途终止也留下可播放的文件。
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .util import IS_WINDOWS, human_size


def _pil():
    from PIL import Image  # type: ignore
    return Image


def _mss():
    import mss  # type: ignore
    return mss


# ------------------------------------------------------------------ 感知哈希

def dhash(img, hash_size: int = 8) -> int:
    """差值哈希：把图缩到 hash_size+1 宽，比较相邻像素亮度。"""
    Image = _pil()
    if not isinstance(img, Image.Image):
        img = img if hasattr(img, "size") else Image.open(io.BytesIO(img))
    small = img.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    px = list(small.getdata())
    bits = 0
    idx = 0
    for row in range(hash_size):
        base = row * (hash_size + 1)
        for col in range(hash_size):
            bits |= (1 << idx) if px[base + col] > px[base + col + 1] else 0
            idx += 1
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# ------------------------------------------------------------------ 抓屏
class ScreenSource:
    def __init__(self, monitor: int = 0) -> None:
        self.monitor = monitor

    def grab(self):
        mss = _mss()
        with mss.mss() as sct:
            idx = min(max(0, self.monitor), len(sct.monitors) - 1)
            shot = sct.grab(sct.monitors[idx])
        Image = _pil()
        return Image.frombytes("RGB", shot.size, shot.rgb)

    def monitors(self) -> list[dict]:
        mss = _mss()
        with mss.mss() as sct:
            return [dict(m) for m in sct.monitors]


# ------------------------------------------------------------------ 拍照
class CaptureService:
    def __init__(self, out_dir: Path, log) -> None:
        self.out_dir = out_dir
        self.log = log
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def _target(self, kind: str, ext: str = "jpg", tag: str = "") -> Path:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = f"-{tag}" if tag else ""
        day_dir = self.out_dir / kind / datetime.now().strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        return day_dir / f"{ts}{suffix}.{ext}"

    def photo_screen(self, monitor: int = 0, quality: int = 80, path: str | None = None) -> dict[str, Any]:
        img = ScreenSource(monitor).grab()
        p = Path(path) if path else self._target("photo", "jpg", tag=f"m{monitor}")
        p.parent.mkdir(parents=True, exist_ok=True)
        img.save(p, quality=int(quality))
        size = p.stat().st_size
        return {"kind": "screen", "path": str(p), "bytes": size, "size_h": human_size(size),
                "w": img.width, "h": img.height, "sig": dhash(img), "ts": int(time.time())}

    def photo_camera(self, index: int = 0, path: str | None = None, warmup: int = 5) -> dict[str, Any]:
        import cv2  # type: ignore

        cap = cv2.VideoCapture(int(index), cv2.CAP_DSHOW if IS_WINDOWS else cv2.CAP_ANY)
        if not cap.isOpened():
            raise RuntimeError(f"摄像头 {index} 打开失败")
        try:
            frame = None
            for _ in range(max(1, warmup)):
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.05)
        finally:
            cap.release()
        if frame is None:
            raise RuntimeError("摄像头取帧失败")
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = _pil().fromarray(rgb)
        p = Path(path) if path else self._target("photo", "jpg", tag=f"cam{index}")
        p.parent.mkdir(parents=True, exist_ok=True)
        img.save(p, quality=85)
        size = p.stat().st_size
        return {"kind": "camera", "index": int(index), "path": str(p), "bytes": size,
                "size_h": human_size(size), "w": img.width, "h": img.height,
                "sig": dhash(img), "ts": int(time.time())}

    # --- 录制 ---
    def record(self, seconds: int = 60, fps: int = 5, quality: int = 60, monitor: int = 0,
               path: str | None = None, should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
        seconds = max(1, int(seconds))
        fps = max(1, min(30, int(fps)))
        p = Path(path) if path else self._target("video", "mp4", tag=f"m{monitor}")
        p.parent.mkdir(parents=True, exist_ok=True)

        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg and IS_WINDOWS:
            return self._record_ffmpeg(ffmpeg, p, seconds, fps, quality, should_stop)
        return self._record_frames(p, seconds, fps, monitor, should_stop)

    def _record_ffmpeg(self, ffmpeg: str, p: Path, seconds: int, fps: int, quality: int,
                       should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
        crf = max(18, min(34, int(40 - quality * 0.25)))
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "gdigrab", "-framerate", str(fps), "-i", "desktop",
            "-t", str(seconds),
            "-vcodec", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-movflags", "frag_keyframe+empty_moov",
            str(p),
        ]
        started = time.time()
        early = False
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except Exception as e:
            self.log.warning("ffmpeg 启动失败，回退逐帧：%s", e)
            return self._record_frames(p, seconds, fps, 0, should_stop)
        while proc.poll() is None:
            if should_stop is not None and should_stop():
                early = True
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                break
            if time.time() - started > seconds + 60:
                proc.kill()
                break
            time.sleep(0.2)
        if not p.exists() or p.stat().st_size == 0:
            err = b""
            try:
                err = proc.stderr.read() if proc.stderr else b""
            except Exception:
                pass
            self.log.warning("ffmpeg 录制未产出文件，回退逐帧：%s", (err or b"")[-200:])
            return self._record_frames(p, seconds, fps, 0, should_stop)
        size = p.stat().st_size
        return {"kind": "video", "encoder": "ffmpeg", "path": str(p), "bytes": size,
                "size_h": human_size(size), "seconds": round(time.time() - started, 1),
                "fps": fps, "stopped_early": early, "ts": int(time.time())}

    def _record_frames(self, p: Path, seconds: int, fps: int, monitor: int,
                       should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
        import cv2  # type: ignore
        import numpy as np  # type: ignore

        src = ScreenSource(monitor)
        first = src.grab()
        w, h = first.size
        w -= w % 2
        h -= h % 2
        writer = cv2.VideoWriter(str(p), cv2.VideoWriter_fourcc(*"mp4v"), float(fps), (w, h))
        interval = 1.0 / fps
        end = time.time() + seconds
        frames = 0
        early = False
        try:
            while time.time() < end:
                if should_stop is not None and should_stop():
                    early = True
                    break
                t0 = time.time()
                img = first if frames == 0 else src.grab()
                if img.size != (w, h):
                    img = img.resize((w, h))
                writer.write(cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR))
                frames += 1
                time.sleep(max(0.0, interval - (time.time() - t0)))
        finally:
            writer.release()
        size = p.stat().st_size if p.exists() else 0
        return {"kind": "video", "encoder": "opencv", "path": str(p), "bytes": size,
                "size_h": human_size(size), "seconds": seconds, "fps": fps,
                "frames": frames, "stopped_early": early, "ts": int(time.time())}

    # --- 幻灯片翻页感知 ---
    def is_slide_changed(self, prev_sig: int | None, monitor: int = 0,
                         threshold: int = 12) -> tuple[bool, int, dict]:
        img = ScreenSource(monitor).grab()
        sig = dhash(img)
        changed = prev_sig is None or hamming(prev_sig, sig) >= max(1, int(threshold))
        return changed, sig, {"w": img.width, "h": img.height}
