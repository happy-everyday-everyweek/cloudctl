"""存储与配额：选盘、待上传暂存上限、日志轮转上限、磁盘余量检查、超限清理。

用户要求：
1. 录完但还没上传的那部分体积要有上限，默认 10G（buffer_max_mb）。
2. 有多块盘（D / E …）时，按剩余空间挑最大的那块放数据。
3. 盘快满时先停止采集，再考虑丢最旧的。
4. 日志也要有上限：按 log_max_mb × log_keep 轮转，超出就把最旧的日志删掉。

只依赖标准库。规则侧可以下发 storage 段收紧配额（只能调小上限、调大余量阀值）。
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable

DEFAULT_CANDIDATES = "CDEFGH"
DEFAULT_BUFFER_MB = 10240          # 10G
DEFAULT_MIN_FREE_MB = 20480        # 盘上留 20G 余量，低于它就不再写盘
KEEP_RATIO = 0.9                   # 清理时降到上限的 90%
LOG_INDEX = re.compile(r"^agent\.log(?:\.(\d+))?$")


def drive_candidates(letters: str = DEFAULT_CANDIDATES) -> list[Path]:
    out: list[Path] = []
    for ch in str(letters or DEFAULT_CANDIDATES).upper():
        if not ch.isalpha():
            continue
        p = Path(f"{ch}:\\")
        try:
            if p.exists():
                out.append(p)
        except Exception:
            continue
    return out


def free_mb(p: Path) -> int:
    try:
        return int(shutil.disk_usage(str(p)).free // (1024 * 1024))
    except Exception:
        return -1


def pick_data_drive(letters: str = DEFAULT_CANDIDATES, min_free_mb: int = DEFAULT_MIN_FREE_MB,
                    prefer_non_system: bool = True) -> dict | None:
    """在候选盘里挑剩余空间最大的那块；优先非系统盘。没有合适的返回 None。"""
    best: tuple[tuple[int, int], Path, int] | None = None
    for p in drive_candidates(letters):
        fm = free_mb(p)
        if fm < 0 or fm < int(min_free_mb):
            continue
        non_system = not (p.drive or "").upper().startswith("C")
        rank = (0 if (prefer_non_system and non_system) else 1, -fm)
        if best is None or rank < best[0]:
            best = (rank, p, fm)
    if best is None:
        return None
    _, drv, fm = best
    return {"drive": str(drv), "free_mb": fm, "home": str(drv / "cloudctl")}


def human_mb(mb: int | float) -> str:
    mb = float(mb)
    if mb >= 1024 * 1024:
        return f"{mb / 1024 / 1024:.1f}T"
    if mb >= 1024:
        return f"{mb / 1024:.1f}G"
    return f"{mb:.0f}M"


class StoreManager:
    """配额裁判。pending 指“已录下但还没上传”的部分（captures + 分片暂存 + spool）。"""

    def __init__(self, cfg, log, overrides: Callable[[], dict] | None = None) -> None:
        self.cfg = cfg
        self.log = log
        self.overrides = overrides or (lambda: {})
        self.blocked_reason = ""
        self.dropped_files = 0
        self.dropped_bytes = 0
        self.pruned_logs = 0
        self.last_check_ts = 0.0

    # ------------------------------------------------------------ 配置
    def conf(self) -> dict:
        o = self.overrides() or {}
        buf = int(self.cfg.buffer_max_mb)
        try:
            if o.get("buffer_max_mb"):
                buf = min(buf, int(o["buffer_max_mb"]))
        except Exception:
            pass
        min_free = int(self.cfg.min_free_mb)
        try:
            if o.get("min_free_mb"):
                min_free = max(min_free, int(o["min_free_mb"]))
        except Exception:
            pass
        drop = bool(self.cfg.drop_oldest_when_full)
        if "drop_oldest" in o:
            drop = bool(o["drop_oldest"])
        return {"buffer_max_mb": buf, "min_free_mb": min_free, "drop_oldest": drop}

    # ------------------------------------------------------------ 统计
    def pending_dirs(self) -> list[Path]:
        return [self.cfg.capture_dir, self.cfg.home_path / "chunk_staging"]

    def pending_files(self) -> list[tuple[Path, int, float]]:
        out: list[tuple[Path, int, float]] = []
        for d in self.pending_dirs():
            if not d.exists():
                continue
            for f in d.rglob("*"):
                try:
                    if f.is_file():
                        st = f.stat()
                        out.append((f, st.st_size, st.st_mtime))
                except Exception:
                    continue
        for name in ("spool.ws.jsonl", "spool.gh.jsonl"):
            p = self.cfg.home_path / name
            if p.exists():
                try:
                    st = p.stat()
                    out.append((p, st.st_size, st.st_mtime))
                except Exception:
                    pass
        return out

    def pending_bytes(self) -> int:
        return sum(size for _f, size, _t in self.pending_files())

    def log_files(self) -> list[tuple[Path, int, int]]:
        """返回 (路径, 索引, 大小)；索引 0 是当前日志，数字越大越旧。"""
        out: list[tuple[Path, int, int]] = []
        for p in self.cfg.home_path.glob("agent.log*"):
            m = LOG_INDEX.match(p.name)
            if not m or not p.is_file():
                continue
            idx = int(m.group(1) or 0)
            try:
                out.append((p, idx, p.stat().st_size))
            except Exception:
                continue
        return out

    def logs_bytes(self) -> int:
        return sum(size for _p, _i, size in self.log_files())

    def logs_conf(self) -> dict:
        keep = max(1, int(self.cfg.log_keep))
        per_mb = max(1, int(self.cfg.log_max_mb))
        o = self.overrides() or {}
        try:
            if o.get("log_max_mb"):
                per_mb = min(per_mb, max(1, int(o["log_max_mb"])))
        except Exception:
            pass
        try:
            if o.get("log_keep"):
                keep = min(keep, max(1, int(o["log_keep"])))
        except Exception:
            pass
        return {"log_max_mb": per_mb, "log_keep": keep, "total_max_mb": per_mb * keep}

    def info(self) -> dict:
        conf = self.conf()
        lc = self.logs_conf()
        free = free_mb(self.cfg.home_path)
        pending = self.pending_bytes()
        logs = self.logs_bytes()
        return {
            "home": str(self.cfg.home_path),
            "drive": (self.cfg.picked_drive or ""),
            "drive_free_mb": free,
            "drive_free_h": human_mb(free) if free >= 0 else "未知",
            "pending_mb": int(pending / (1024 * 1024)),
            "pending_h": human_mb(pending / (1024 * 1024)),
            "buffer_max_mb": conf["buffer_max_mb"],
            "buffer_max_h": human_mb(conf["buffer_max_mb"]),
            "buffer_used_pct": round(pending * 100.0 / max(1, conf["buffer_max_mb"] * 1024 * 1024), 1),
            "min_free_mb": conf["min_free_mb"],
            "logs_bytes": logs,
            "logs_h": human_mb(logs / (1024 * 1024)),
            "log_total_max_mb": lc["total_max_mb"],
            "pruned_logs": self.pruned_logs,
            "dropped_files": self.dropped_files,
            "dropped_h": human_mb(self.dropped_bytes / (1024 * 1024)),
            "blocked_reason": self.blocked_reason,
        }

    # ------------------------------------------------------------ 写盘闸门
    def can_write(self, need_mb: int = 0) -> tuple[bool, str]:
        """采集前问一句：盘还有余量吗？暂存区还有额度吗？"""
        conf = self.conf()
        free = free_mb(self.cfg.home_path)
        if 0 <= free < conf["min_free_mb"] + int(need_mb):
            self.blocked_reason = f"磁盘剩余 {human_mb(free)} 低于阀值 {human_mb(conf['min_free_mb'])}"
            return False, self.blocked_reason
        used_mb = self.pending_bytes() / (1024 * 1024)
        if used_mb + need_mb > conf["buffer_max_mb"]:
            self.blocked_reason = (f"待上传暂存 {human_mb(used_mb)} 已达上限 "
                                   f"{human_mb(conf['buffer_max_mb'])}")
            return False, self.blocked_reason
        self.blocked_reason = ""
        return True, "ok"

    # ------------------------------------------------------------ 日志
    def prune_logs(self) -> dict:
        """日志也耍有上限：先删超出 keep 的轮转文件，再删最旧的直到总量达标。"""
        lc = self.logs_conf()
        files = self.log_files()
        removed = 0
        freed = 0
        for p, idx, _size in sorted(files, key=lambda x: -x[1]):
            if idx <= lc["log_keep"] and idx != 0:
                continue
            if idx == 0:
                continue
            try:
                size = p.stat().st_size
                p.unlink()
                removed += 1
                freed += size
            except Exception:
                continue
        total = self.logs_bytes()
        cap = lc["total_max_mb"] * 1024 * 1024
        if total > cap:
            for p, idx, size in sorted(self.log_files(), key=lambda x: (x[1] == 0, -x[1])):
                if total <= cap or idx == 0:
                    continue
                try:
                    p.unlink()
                    removed += 1
                    freed += size
                    total -= size
                except Exception:
                    continue
        if removed:
            self.pruned_logs += removed
            self.log.info("日志清理：删除 %s 个旧日志，释放 %s", removed, human_mb(freed / (1024 * 1024)))
        return {"removed": removed, "freed_mb": int(freed / (1024 * 1024)),
                "logs_bytes": self.logs_bytes(), "total_max_mb": lc["total_max_mb"]}

    # ------------------------------------------------------------ 清理
    def enforce(self, upload: Callable[[], Any] | None = None) -> dict:
        """超限时的处理顺序：先试上传，再按需丢最旧的未上传文件；日志每次都清理。"""
        log_result = self.prune_logs()
        conf = self.conf()
        self.last_check_ts = time.time()
        pending = self.pending_bytes()
        limit = conf["buffer_max_mb"] * 1024 * 1024
        result = {"pending_mb": int(pending / (1024 * 1024)), "limit_mb": conf["buffer_max_mb"],
                  "uploaded": False, "dropped": 0, "dropped_mb": 0, "ok": True,
                  "logs_removed": log_result["removed"]}
        if pending <= limit:
            return result
        self.log.warning("待上传暂存 %s 超过上限 %s，先尝试上传",
                         human_mb(pending / (1024 * 1024)), human_mb(conf["buffer_max_mb"]))
        if upload is not None:
            try:
                upload()
                result["uploaded"] = True
            except Exception as e:
                self.log.warning("超限时上传失败：%s", e)
            pending = self.pending_bytes()
            result["pending_mb"] = int(pending / (1024 * 1024))
            if pending <= limit:
                return result
        if not conf["drop_oldest"]:
            result["ok"] = False
            self.blocked_reason = "暂存超限且不允许自动丢弃"
            return result
        target = int(limit * KEEP_RATIO)
        for f, size, _t in sorted(self.pending_files(), key=lambda x: x[2]):
            if pending <= target:
                break
            if f.name.startswith("spool."):
                continue
            try:
                f.unlink()
            except Exception:
                continue
            pending -= size
            result["dropped"] += 1
            result["dropped_mb"] += int(size / (1024 * 1024))
            self.dropped_files += 1
            self.dropped_bytes += size
        self.log.warning("暂存超限：丢弃 %s 个最旧文件，释放约 %s",
                         result["dropped"], human_mb(result["dropped_mb"]))
        result["pending_mb"] = int(pending / (1024 * 1024))
        result["ok"] = pending <= limit
        return result
