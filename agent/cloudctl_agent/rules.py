"""规则引擎：规则合并、以及“何时拍照/录像”的判定。

规则来源：服务端下发（主通道）或仓库文件（备用通道），
本地缓存在 rules.json，断网时继续沿用缓存继续执行。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .util import in_time_window, read_json, title_match_any, write_json

DEFAULT_RULES: dict[str, Any] = {
    "version": 0,
    "identity": {"display_name": "", "tags": []},
    "capture": {
        "photo": {
            "enabled": False,
            "device": "cam0",
            "min_interval_s": 600,
            "max_per_day": 50,
            "active_hours": None,
            "settle_s": 3,
            "when": {
                "only_if_idle_s": 0,
                "on_foreground_change": True,
                "on_session_unlock": False,
            },
        },
        "video": {
            "enabled": False,
            "segment_s": 60,
            "fps": 5,
            "quality": 60,
            "monitor": 0,
            "max_mb_per_day": 500,
            "active_hours": None,
            "settle_s": 5,
            "cooldown_s": 10,
            "when": {
                "on_foreground_change": True,
                "on_slideshow": True,
                "idle_skip": True,
                "idle_skip_s": 300,
                "exclude_titles": [],
            },
        },
        "slideshow": {
            "enabled": True,
            "titles": ["PowerPoint 幻灯片放映", "Slide Show", "Presentation"],
            "threshold": 12,
            "cooldown_s": 3,
        },
    },
    "scan": {
        "roots": [],
        "interval_min": 30,
        "follow_links": False,
        "max_file_mb": 4096,
        "types": {
            "doc": ["ppt", "pptx", "doc", "docx", "pdf", "xls", "xlsx", "md", "txt"],
            "image": ["jpg", "jpeg", "png", "webp", "gif", "bmp", "heic"],
            "audio": ["mp3", "wav", "flac", "m4a", "aac", "ogg"],
            "video": ["mp4", "mov", "mkv", "avi", "webm"],
        },
        "exclude_globs": [
            "*/node_modules/*",
            "*/.git/*",
            "*/AppData/Local/Temp/*",
            "*/Windows/*",
            "*/$Recycle.Bin/*",
            "*/captures/*",
        ],
    },
    "upload": {
        "enabled": False,
        "repo": "",
        "branch": "main",
        "path_prefix": "kb",
        "layout": "{prefix}/{type}/{yyyy}/{mm}/{name}",
        "dedup": "sha256",
        "max_single_mb": 90,
        "chunk_video": True,
        "chunk_seconds": 600,
        "concurrency": 2,
    },
    "desktop": {"max_fps": 10, "allow_input": True, "monitors": 1, "quality": 60},
    "security": {"allow_shell": True, "allow_file_write": True, "allow_delete": False},
}


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class RuleSet:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = deep_merge(DEFAULT_RULES, data or {})

    @property
    def version(self) -> int:
        return int(self.data.get("version") or 0)

    @property
    def photo(self) -> dict:
        return self.data["capture"]["photo"]

    @property
    def video(self) -> dict:
        return self.data["capture"]["video"]

    @property
    def slideshow(self) -> dict:
        return self.data["capture"]["slideshow"]

    @property
    def scan(self) -> dict:
        return self.data["scan"]

    @property
    def upload(self) -> dict:
        return self.data["upload"]

    @property
    def desktop(self) -> dict:
        return self.data["desktop"]

    @property
    def security(self) -> dict:
        return self.data["security"]

    def ext_map(self) -> dict[str, str]:
        """扩展名 -> 类型，如 pptx -> doc。"""
        out: dict[str, str] = {}
        for kind, exts in (self.data["scan"].get("types") or {}).items():
            for e in exts:
                out[str(e).lower().lstrip(".")] = kind
        return out

    # --- 持久化 ---
    @classmethod
    def load(cls, path: Path) -> "RuleSet":
        return cls(read_json(path, {}) or {})

    def save(self, path: Path) -> None:
        write_json(path, self.data)

    def merge(self, incoming: dict[str, Any], version: int = 0) -> bool:
        """合并新规则，仅当版本号更新时生效。返回是否发生变化。"""
        new_ver = int(version or (incoming or {}).get("version") or 0)
        if new_ver and new_ver <= self.version:
            return False
        self.data = deep_merge(self.data, incoming or {})
        if new_ver:
            self.data["version"] = new_ver
        return True


@dataclass
class TriggerState:
    day: str = ""
    photo_count: int = 0
    video_mb: float = 0.0
    last_photo_ts: float = 0.0
    last_video_start_ts: float = 0.0
    last_title: str = ""
    title_seen_ts: float = 0.0
    last_reason: str = ""
    errors: list[str] = field(default_factory=list)


class TriggerEngine:
    """把“什么时候拍”这件事收敛到一个地方。

    输入当前前台窗口与空闲时长，输出本轮是否开始/停止录像、是否拍照。
    """

    def __init__(self, rules: RuleSet) -> None:
        self.rules = rules
        self.state = TriggerState(day=datetime.now().strftime("%Y-%m-%d"))
        self.video_running = False

    def reload(self, rules: RuleSet) -> None:
        self.rules = rules

    # --- 统计 ---
    def _roll_day(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self.state.day:
            self.state.day = today
            self.state.photo_count = 0
            self.state.video_mb = 0.0

    def note_photo(self) -> None:
        self._roll_day()
        self.state.photo_count += 1
        self.state.last_photo_ts = time.time()

    def note_video_bytes(self, nbytes: int) -> None:
        self._roll_day()
        self.state.video_mb += nbytes / (1024 * 1024)

    def note_video_start(self) -> None:
        self.state.last_video_start_ts = time.time()
        self.video_running = True

    def note_video_stop(self) -> None:
        self.video_running = False

    # --- 判定 ---
    def decide(self, fg_title: str, idle_s: float, locked: bool = False) -> dict[str, Any]:
        self._roll_day()
        now = time.time()
        ph, vd, sl = self.rules.photo, self.rules.video, self.rules.slideshow
        out: dict[str, Any] = {"photo": False, "video_start": False, "video_stop": False, "reason": ""}

        # 前台窗口变化跟踪
        if fg_title and fg_title != self.state.last_title:
            self.state.last_title = fg_title
            self.state.title_seen_ts = now
        title_stable_s = now - self.state.title_seen_ts

        # ---- 停止判定 ----
        if self.video_running:
            stop_reason = ""
            if locked:
                stop_reason = "session_locked"
            elif not vd.get("enabled"):
                stop_reason = "disabled"
            elif not in_time_window(vd.get("active_hours")):
                stop_reason = "outside_active_hours"
            elif self.state.video_mb >= float(vd.get("max_mb_per_day") or 1e9):
                stop_reason = "daily_quota"
            elif title_match_any(fg_title, vd["when"].get("exclude_titles") or []):
                stop_reason = "excluded_title"
            elif vd["when"].get("idle_skip") and idle_s >= float(vd["when"].get("idle_skip_s") or 300):
                stop_reason = "idle"
            if stop_reason:
                out["video_stop"] = True
                out["reason"] = stop_reason
                self.state.last_reason = stop_reason
                return out

        # ---- 录像启动判定 ----
        if vd.get("enabled") and not self.video_running and not locked:
            ok = in_time_window(vd.get("active_hours"))
            ok = ok and self.state.video_mb < float(vd.get("max_mb_per_day") or 1e9)
            ok = ok and not title_match_any(fg_title, vd["when"].get("exclude_titles") or [])
            ok = ok and (now - self.state.last_video_start_ts) >= float(vd.get("cooldown_s") or 0)
            triggered, why = self._interest_trigger(vd["when"], title_stable_s, float(vd.get("settle_s") or 0))
            if ok and triggered:
                out["video_start"] = True
                out["reason"] = why or "interval"
                self.state.last_reason = out["reason"]
                return out

        # ---- 拍照判定 ----
        if ph.get("enabled") and not locked:
            ok = self.state.photo_count < int(ph.get("max_per_day") or 0)
            ok = ok and in_time_window(ph.get("active_hours"))
            ok = ok and (now - self.state.last_photo_ts) >= float(ph.get("min_interval_s") or 0)
            ok = ok and idle_s >= float(ph["when"].get("only_if_idle_s") or 0)
            settle = float(ph.get("settle_s") or 0)
            ok = ok and title_stable_s >= settle
            out["photo"] = bool(ok)
            if ok:
                self.state.last_reason = "photo:interval"
        return out

    def _interest_trigger(self, when: dict, title_stable_s: float, settle_s: float) -> tuple[bool, str]:
        """“值得录”的判定。

        配置了条件就按条件走（换窗口 / 放映），什么都没配就当成周期录制。
        仅当当前窗口稳定时间超过 settle_s 才认，避免逐窗口切换时录一堆碎片。
        """
        want_change = bool(when.get("on_foreground_change"))
        want_slideshow = bool(when.get("on_slideshow"))
        if not want_change and not want_slideshow:
            return True, "interval"
        if title_stable_s < settle_s:
            return False, "settling"
        return True, "foreground_ready"
