"""素材归档：扫描本地文件 + 去重 + 上传到指定 GitHub 仓库。

去重靠本地 SQLite 记的 sha256；远端路径按 layout 模板生成，
按类型与年月分桶，避免单目录堆积过多文件。
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .rules import RuleSet
from .util import glob_match_any, human_size, sha256_file, sha256_head

SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "$RECYCLE.BIN"}


# ------------------------------------------------------------------ 状态库
class StateDB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS files (
                sha256 TEXT PRIMARY KEY,
                local_path TEXT NOT NULL,
                remote_path TEXT,
                size INTEGER,
                kind TEXT,
                status TEXT,
                err TEXT,
                ts INTEGER
            )"""
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_local ON files(local_path)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON files(status)")
        self.conn.commit()

    def seen(self, sha: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT sha256, remote_path, status FROM files WHERE sha256=?", (sha,)
            ).fetchone()
        if not row:
            return None
        return {"sha256": row[0], "remote_path": row[1], "status": row[2]}

    def mark(self, sha: str, local_path: str, remote_path: str, size: int, kind: str, status: str, err: str = "") -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO files (sha256, local_path, remote_path, size, kind, status, err, ts)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (sha, local_path, remote_path, size, kind, status, err, int(time.time())),
            )
            self.conn.commit()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            rows = self.conn.execute("SELECT status, COUNT(*), SUM(size) FROM files GROUP BY status").fetchall()
        return {r[0]: {"count": r[1], "bytes": r[2] or 0} for r in rows}

    def pending(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT local_path, sha256, size, kind FROM files WHERE status='pending' LIMIT ?", (int(limit),)
            ).fetchall()
        return [{"local_path": r[0], "sha256": r[1], "size": r[2], "kind": r[3]} for r in rows]


# ------------------------------------------------------------------ 扫描
@dataclass
class FoundFile:
    path: Path
    size: int
    mtime: int
    kind: str


class Scanner:
    def __init__(self, rules: RuleSet, log) -> None:
        self.rules = rules
        self.log = log

    def reload(self, rules: RuleSet) -> None:
        self.rules = rules

    def walk(self, limit: int = 200000) -> list[FoundFile]:
        r = self.rules.scan
        roots = r.get("roots") or []
        ext_map = self.rules.ext_map()
        excludes = r.get("exclude_globs") or []
        follow = bool(r.get("follow_links"))
        max_mb = float(r.get("max_file_mb") or 4096)
        out: list[FoundFile] = []

        for root in roots:
            base = Path(os.path.expandvars(os.path.expanduser(str(root))))
            if not base.exists():
                self.log.debug("扫描根目录不存在：%s", base)
                continue
            for dirpath, dirnames, filenames in os.walk(base, followlinks=follow):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                if glob_match_any(dirpath, excludes):
                    dirnames[:] = []
                    continue
                for name in filenames:
                    if len(out) >= limit:
                        return out
                    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                    kind = ext_map.get(ext)
                    if not kind:
                        continue
                    full = Path(dirpath) / name
                    if glob_match_any(str(full), excludes):
                        continue
                    try:
                        st = full.stat()
                    except OSError:
                        continue
                    if st.st_size <= 0 or st.st_size > max_mb * 1024 * 1024:
                        continue
                    out.append(FoundFile(path=full, size=st.st_size, mtime=int(st.st_mtime), kind=kind))
        out.sort(key=lambda f: f.mtime)
        return out


# ------------------------------------------------------------------ 上传
class GitHubUploader:
    """把文件提交到指定仓库。

    走 Contents API。超过单文件上限的直接标记 too_large，
    避免白传一轮再被 GitHub 拒。
    """

    def __init__(self, log) -> None:
        self.log = log
        self.last_status = 0

    @staticmethod
    def remote_path(layout: str, prefix: str, kind: str, path: Path, sha: str, mtime: int) -> str:
        dt = datetime.fromtimestamp(mtime or time.time())
        stem, ext = path.stem, path.suffix
        name = f"{stem}-{sha[:8]}{ext}"
        out = (layout or "{prefix}/{type}/{yyyy}/{mm}/{name}").format(
            prefix=prefix.strip("/"), type=kind, yyyy=f"{dt:%Y}", mm=f"{dt:%m}", dd=f"{dt:%d}", name=name
        )
        return out.replace("//", "/").lstrip("/")

    def put(self, repo: str, branch: str, remote_path: str, data: bytes, token: str, message: str) -> tuple[bool, str]:
        import requests  # type: ignore

        url = f"https://api.github.com/repos/{repo}/contents/{remote_path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "cloudctl-agent",
        }
        payload = {
            "message": message,
            "content": base64.b64encode(data).decode("ascii"),
            "branch": branch or "main",
        }
        sha = self._exists(url, headers, branch)
        if sha:
            return True, f"exists:{sha[:8]}"
        resp = requests.put(url, headers=headers, json=payload, timeout=120)
        self.last_status = resp.status_code
        if resp.status_code in (200, 201):
            return True, ""
        if resp.status_code == 422:
            return True, "already_present"
        try:
            err = resp.json().get("message", "")
        except Exception:
            err = resp.text[:200]
        return False, f"{resp.status_code} {err}"

    @staticmethod
    def _exists(url: str, headers: dict, branch: str) -> str | None:
        import requests  # type: ignore

        try:
            r = requests.get(url, headers=headers, params={"ref": branch or "main"}, timeout=30)
            if r.status_code == 200:
                return r.json().get("sha")
        except Exception:
            pass
        return None


# ------------------------------------------------------------------ 调度
class SyncService:
    def __init__(self, rules: RuleSet, db: StateDB, log) -> None:
        self.rules = rules
        self.db = db
        self.log = log
        self.scanner = Scanner(rules, log)
        self.uploader = GitHubUploader(log)
        self.last_run: dict[str, Any] = {}
        self._busy = threading.Lock()

    def reload(self, rules: RuleSet) -> None:
        self.rules = rules
        self.scanner.reload(rules)

    def sync_once(self, limit: int = 500) -> dict[str, Any]:
        if not self._busy.acquire(blocking=False):
            return {"busy": True}
        started = time.time()
        stats: dict[str, Any] = {
            "scanned": 0, "uploaded": 0, "skipped": 0, "failed": 0,
            "too_large": 0, "bytes": 0, "errors": [],
        }
        try:
            up = self.rules.upload
            files = self.scanner.walk()
            stats["scanned"] = len(files)
            enabled = bool(up.get("enabled")) and bool(up.get("repo"))
            max_single = float(up.get("max_single_mb") or 90) * 1024 * 1024

            for f in files[: int(limit)]:
                try:
                    sha = sha256_head(f.path) if f.size > 8 * 1024 * 1024 else sha256_file(f.path)
                except OSError as e:
                    stats["failed"] += 1
                    stats["errors"].append(f"{f.path}: {e}")
                    continue
                known = self.db.seen(sha)
                if known and known["status"] in ("done", "exists"):
                    stats["skipped"] += 1
                    continue
                if not enabled:
                    self.db.mark(sha, str(f.path), "", f.size, f.kind, "local_only")
                    stats["skipped"] += 1
                    continue
                if f.size > max_single:
                    self.db.mark(sha, str(f.path), "", f.size, f.kind, "too_large")
                    stats["too_large"] += 1
                    continue
                remote = GitHubUploader.remote_path(
                    up.get("layout", ""), up.get("prefix", ""), f.kind, f.path, sha, f.mtime
                )
                try:
                    data = f.path.read_bytes()
                except OSError as e:
                    stats["failed"] += 1
                    stats["errors"].append(f"{f.path}: {e}")
                    continue
                ok, err = self.uploader.put(
                    up.get("repo", ""), up.get("branch", "main"), remote, data,
                    up.get("token") or self.rules.data.get("_upload_token", ""),
                    f"kb: add {f.kind} {f.path.name}",
                )
                if ok:
                    self.db.mark(sha, str(f.path), remote, f.size, f.kind, "done" if not err else "exists")
                    stats["uploaded"] += 1
                    stats["bytes"] += f.size
                else:
                    self.db.mark(sha, str(f.path), remote, f.size, f.kind, "pending", err)
                    stats["failed"] += 1
                    stats["errors"].append(f"{f.path.name}: {err}")
                    if "rate limit" in err.lower():
                        stats["errors"].append("触发 GitHub 速率限制，已中止本轮")
                        break
            stats["elapsed"] = round(time.time() - started, 2)
            stats["bytes_h"] = human_size(stats["bytes"])
            stats["errors"] = stats["errors"][:20]
            stats["db"] = self.db.summary()
            self.last_run = stats
            self.log.info(
                "归档完成：扫描 %s，上传 %s（%s），跳过 %s，失败 %s",
                stats["scanned"], stats["uploaded"], stats["bytes_h"], stats["skipped"], stats["failed"],
            )
            return stats
        finally:
            self._busy.release()