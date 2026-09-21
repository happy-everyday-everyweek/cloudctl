"""通用工具：日志、哈希、时间窗、Windows 前台窗口与空闲检测。"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Iterable

IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------- 日志

def setup_logging(home: Path, level: str = "INFO", max_mb: int = 5, keep: int = 3) -> logging.Logger:
    home.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("cloudctl")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")

    fh = logging.handlers.RotatingFileHandler(
        home / "agent.log", maxBytes=max_mb * 1024 * 1024, backupCount=keep, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger


# ---------------------------------------------------------------- 杂项

def now_ts() -> int:
    return int(time.time())


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_head(path: Path, size: int = 256 * 1024) -> str:
    """大文件只读头部算摘要，用于快速变化检测。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(size))
    h.update(str(path.stat().st_size).encode())
    return h.hexdigest()


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n}B"


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 时间窗

def parse_hhmm(s: str) -> dtime:
    hh, mm = str(s).split(":")[:2]
    return dtime(int(hh), int(mm))


def in_time_window(window: Any, when: datetime | None = None) -> bool:
    """window 形如 ["08:00", "22:00"]，跨零点（22:00-06:00）也可。

    放宽 15 分钟容差，防止刚好卡在边界上被挡掉。
    """
    if not window:
        return True
    if isinstance(window, (list, tuple)) and len(window) == 2 and all(window):
        start, end = parse_hhmm(window[0]), parse_hhmm(window[1])
    else:
        return True
    now = (when or datetime.now()).time()
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


def glob_match_any(path: str, patterns: Iterable[str]) -> bool:
    p = path.replace("\\", "/")
    for pat in patterns or []:
        if fnmatch.fnmatch(p, pat.replace("\\", "/")) or fnmatch.fnmatch(p.lower(), pat.lower().replace("\\", "/")):
            return True
        if pat.strip("*/ ") and pat.strip("*/ ").lower() in p.lower():
            return True
    return False


def title_match_any(title: str, patterns: Iterable[str]) -> bool:
    t = (title or "")
    for pat in patterns or []:
        if "*" in pat or "?" in pat:
            if fnmatch.fnmatch(t.lower(), pat.lower()):
                return True
        elif pat.lower() in t.lower():
            return True
    return False


# ---------------------------------------------------------------- Windows 状态
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    def idle_seconds() -> float:
        """用户无键盘鼠标输入的持续秒数。"""
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not _user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        millis = _kernel32.GetTickCount() - info.dwTime
        return max(0.0, millis / 1000.0)

    def foreground_window() -> tuple[str, int]:
        """返回（前台窗口标题, 进程 PID）。"""
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return "", 0
        length = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return buf.value, int(pid.value)

    def session_is_locked() -> bool:
        """通过切换桌面的小技巧判断工作站是否锁定。"""
        h = _user32.OpenInputDesktop(0, False, 0x0100)
        if h:
            _user32.CloseDesktop(h)
            return False
        return True

    def set_dpi_aware() -> None:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        except Exception:
            try:
                _user32.SetProcessDPIAware()
            except Exception:
                pass

else:

    def idle_seconds() -> float:
        return 0.0

    def foreground_window() -> tuple[str, int]:
        return "", 0

    def session_is_locked() -> bool:
        return False

    def set_dpi_aware() -> None:
        return None
