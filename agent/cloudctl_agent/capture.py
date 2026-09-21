"""采集层：屏幕 / 摄像头拍照、屏幕录制、幻灯片翻页感知。

录像优先用 ffmpeg 的 gdigrab（Windows）录，拿不到 ffmpeg 时回退到
逐帧抓屏 + OpenCV 写盘，保证不依赖外部工具也能跑。
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

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

    # --- 屏幕截图 ---
    def photo_screen(self, monitor: int = 0, quality: int = 80, path: str | None = None) -> dict[str, Any]:
        img = ScreenSource(monitor).grab()
        p = Path(path) if path else self._target("photo", "jpg", tag=f"m{monitor}")
        p.parent.mkdir(parents=True, exist_ok=True)
        img.save(p, quality=int(quality))
        return {
            "kind": "screen",
            "path": str(p),
            "bytes": p.stat().st_size,
            "size_h": human_size(p.stat().st_size),
            "w": img.width,
            "h": img.height,
            "sig": dhash(img),
            "ts": int(time.time()),
        }

    # --- 摄像头拍照 ---
    def photo_camera(self, index: int = 0, path: str | None = None, warmup: int = 5) -> dict[str, Any]:
        import cv2  # type: ignore

        cap = cv2.VideoCapture(int(index), cv2.CAP_DSHOW if IS_WINDOWS else cv2.CAP_ANY)
        if not cap.isOpened():
            raise RuntimeError(f"摄像头 {index} 打开失败")
        try:
            frame = None
            for _ in range(max(1, warmup)):  # 前几帧是黑的，丢弃
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
        return {
            "kind": "camera",
            "index": int(index),
            "path": str(p),
            "bytes": p.stat().st_size,
            "size_h": human_size(p.stat().st_size),
            "w": img.width,
            "h": img.height,
            "sig": dhash(img),
            "ts": int(time.time()),
        }

    # --- 录制 ---
    def record(
        self,
        seconds: int = 60,
        fps: int = 5,
        quality: int = 60,
        monitor: int = 0,
        path: str | None = None,
    ) -> dict[str, Any]:
        seconds = max(1, int(seconds))
        fps = max(1, min(30, int(fps)))
        p = Path(path) if path else self._target("video", "mp4", tag=f"m{monitor}")
        p.parent.mkdir(parents=True, exist_ok=True)

        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg and IS_WINDOWS:
            return self._record_ffmpeg(ffmpeg, p, seconds, fps, quality)
        return self._record_frames(p, seconds, fps, monitor)

    def _record_ffmpeg(self, ffmpeg: str, p: Path, seconds: int, fps: int, quality: int) -> dict[str, Any]:
        crf = max(18, min(34, int(40 - quality * 0.25)))
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "gdigrab", "-framerate", str(fps), "-i", "desktop",
            "-t", str(seconds),
            "-vcodec", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-pix_fmt", "yuv420p", str(p),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=seconds + 60)
        if proc.returncode != 0 or not p.exists():
            self.log.warning("ffmpeg 录制失败，回退逐帧：%s", (proc.stderr or b"")[-200:])
            return self._record_frames(p, seconds, fps, 0)
        return {"kind": "video", "encoder": "ffmpeg", "path": str(p), "bytes": p.stat().st_size, "size_h": human_size(p.stat().st_size), "seconds": seconds, "fps": fps, "ts": int(time.time())}

    def _record_frames(self, p: Path, seconds: int, fps: int, monitor: int) -> dict[str, Any]:
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
        try:
            while time.time() < end:
                t0 = time.time()
                img = first if frames == 0 else src.grab()
                if img.size != (w, h):
                    img = img.resize((w, h))
                writer.write(cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR))
                frames += 1
                time.sleep(max(0.0, interval - (time.time() - t0)))
        finally:
            writer.release()
        return {"kind": "video", "encoder": "opencv", "path": str(p), "bytes": p.stat().st_size, "size_h": human_size(p.stat().st_size), "seconds": seconds, "fps": fps, "frames": frames, "ts": int(time.time())}

    # --- 幻灯片翻页感知 ---
    def is_slide_changed(self, prev_sig: int | None, monitor: int = 0, threshold: int = 12, quality: int = 70) -> tuple[bool, int, dict]:
        """返回（是否翻页, 当前签名, 截图信息）。签名会顺便复用给录像使用。"""
        img = ScreenSource(monitor).grab()
        sig = dhash(img)
        changed = prev_sig is None or hamming(prev_sig, sig) >= max(1, int(threshold))
        return changed, sig, {"w": img.width, "h": img.height}
