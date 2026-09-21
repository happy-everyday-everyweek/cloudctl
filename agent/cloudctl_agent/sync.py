"""素材归档：扫描 + 去重 + 分片 + 上传 + 索引。

四层机制：
1. 去重：本地 sqlite 记 sha256（已传过的直接跳），索引库再做第二道判定。
2. 分片：单文件超过 max_single_mb 时不放弃，切成 size_mb 小片连清单一起传，
   下载后按清单可复原，因此视频类也能进 GitHub。
3. 上传：走 Contents API，若有镜像池则逐个候选重试，失败标 pending 待下轮。
4. 索引：每轮结束把索引写成 kb/_index/index.json（可按类型分文件）传上去，
   别人 clone 后不用拉全文就能检索。
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
from typing import Any

from . import chunker
from .indexer import LocalIndex, RepoIndex
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

    def mark(self, sha: str, local_path: str, remote_path: str, size: int, kind: str,
             status: str, err: str = "") -> None:
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
    """把文件提交到指定仓库，可选走镜像池候选重试。"""

    def __init__(self, log, pool=None) -> None:
        self.log = log
        self.pool = pool
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

    def _candidates(self, api_url: str) -> list[tuple[str, str]]:
        if self.pool is None:
            return [("direct", api_url)]
        out: list[tuple[str, str]] = []
        for mid, url in self.pool.candidates(api_url):
            if "github.com/https://" in url or "github.com/http://" in url:
                continue
            out.append((mid, url))
        return out[:3] or [("direct", api_url)]

    def put(self, repo: str, branch: str, remote_path: str, data: bytes, token: str, message: str) -> tuple[bool, str]:
        import requests  # type: ignore

        api = f"https://api.github.com/repos/{repo}/contents/{remote_path}"
        headers = {"Accept": "application/vnd.github+json",
                   "Authorization": f"Bearer {token}",
                   "User-Agent": "cloudctl-agent"}
        last = "无可用候选"
        for mid, url in self._candidates(api):
            try:
                sha = self._exists(url, headers, branch)
                if sha:
                    if self.pool is not None:
                        self.pool.report(mid, True)
                    return True, f"exists:{sha[:8]}"
                payload = {"message": message,
                           "content": base64.b64encode(data).decode("ascii"),
                           "branch": branch or "main"}
                resp = requests.put(url, headers=headers, json=payload, timeout=180)
                self.last_status = resp.status_code
                if resp.status_code in (200, 201):
                    if self.pool is not None:
                        self.pool.report(mid, True)
                    return True, ""
                if resp.status_code == 422:
                    if self.pool is not None:
                        self.pool.report(mid, True)
                    return True, "already_present"
                if self.pool is not None:
                    self.pool.report(mid, False)
                try:
                    last = f"{resp.status_code} {resp.json().get('message', '')}"
                except Exception:
                    last = f"{resp.status_code} {resp.text[:160]}"
            except Exception as e:
                if self.pool is not None:
                    self.pool.report(mid, False)
                last = str(e)
        return False, last

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
    def __init__(self, rules: RuleSet, db: StateDB, log, index: LocalIndex | None = None,
                 pool=None, device_id: str = "", staging: Path | None = None) -> None:
        self.rules = rules
        self.db = db
        self.log = log
        self.index = index
        self.device_id = device_id
        self.staging = Path(staging) if staging else None
        self.scanner = Scanner(rules, log)
        self.uploader = GitHubUploader(log, pool=pool)
        self.last_run: dict[str, Any] = {}
        self._busy = threading.Lock()

    def reload(self, rules: RuleSet) -> None:
        self.rules = rules
        self.scanner.reload(rules)

    # --- 工具 ---
    def _staging_dir(self) -> Path:
        base = self.staging or Path(".") / "chunk_staging"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _token(self) -> str:
        up = self.rules.upload
        return up.get("token") or self.rules.data.get("_upload_token", "")

    def _remember(self, sha: str, remote: str, f: FoundFile, chunks: list[dict] | None = None) -> None:
        if self.index is None:
            return
        self.index.add(sha, remote, local_path=str(f.path), bytes=f.size, kind=f.kind,
                       mtime=f.mtime, device=self.device_id, chunks=chunks or [])

    # --- 一轮归档 ---
    def sync_once(self, limit: int = 500) -> dict[str, Any]:
        if not self._busy.acquire(blocking=False):
            return {"busy": True}
        started = time.time()
        stats: dict[str, Any] = {"scanned": 0, "uploaded": 0, "skipped": 0, "failed": 0,
                                 "too_large": 0, "chunked": 0, "parts": 0, "bytes": 0,
                                 "index": 0, "errors": []}
        try:
            up = self.rules.upload
            ck = self.rules.chunk
            files = self.scanner.walk()
            stats["scanned"] = len(files)
            enabled = bool(up.get("enabled")) and bool(up.get("repo"))
            max_single = float(up.get("max_single_mb") or 90) * 1024 * 1024
            chunk_mb = float(ck.get("size_mb") or 40)
            layout = up.get("layout", "")
            prefix = up.get("prefix", "")
            repo = up.get("repo", "")
            branch = up.get("branch", "main")
            token = self._token()

            for f in files[: int(limit)]:
                try:
                    sha = sha256_head(f.path) if f.size > 8 * 1024 * 1024 else sha256_file(f.path)
                except OSError as e:
                    stats["failed"] += 1
                    stats["errors"].append(f"{f.path}: {e}")
                    continue
                known = self.db.seen(sha)
                if known and known["status"] in ("done", "exists", "chunked"):
                    stats["skipped"] += 1
                    continue
                if self.index is not None:
                    hit = self.index.has(sha)
                    if hit:
                        self.db.mark(sha, str(f.path), hit["repo_path"], f.size, f.kind, "done")
                        stats["skipped"] += 1
                        continue
                if not enabled:
                    self.db.mark(sha, str(f.path), "", f.size, f.kind, "local_only")
                    stats["skipped"] += 1
                    continue

                remote = GitHubUploader.remote_path(layout, prefix, f.kind, f.path, sha, f.mtime)

                # 超大文件走分片
                if f.size > max_single:
                    if not ck.get("enabled"):
                        self.db.mark(sha, str(f.path), "", f.size, f.kind, "too_large")
                        stats["too_large"] += 1
                        continue
                    res = self._upload_chunked(f, sha, remote, repo, branch, token, chunk_mb, ck)
                    if res.get("ok"):
                        stats["chunked"] += 1
                        stats["parts"] += int(res.get("parts") or 0)
                        stats["bytes"] += f.size
                    else:
                        stats["failed"] += 1
                        stats["errors"].append(f"{f.path.name}: {res.get('err')}")
                    continue

                try:
                    data = f.path.read_bytes()
                except OSError as e:
                    stats["failed"] += 1
                    stats["errors"].append(f"{f.path}: {e}")
                    continue
                ok, err = self.uploader.put(repo, branch, remote, data, token,
                                            f"kb: add {f.kind} {f.path.name}")
                if ok:
                    self.db.mark(sha, str(f.path), remote, f.size, f.kind, "done" if not err else "exists")
                    self._remember(sha, remote, f)
                    stats["uploaded"] += 1
                    stats["bytes"] += f.size
                else:
                    self.db.mark(sha, str(f.path), remote, f.size, f.kind, "pending", err)
                    stats["failed"] += 1
                    stats["errors"].append(f"{f.path.name}: {err}")
                    if "rate limit" in err.lower():
                        stats["errors"].append("触发 GitHub 速率限制，已中止本轮")
                        break

            if enabled and self.rules.index.get("enabled"):
                stats["index"] = self.upload_index(repo, branch, token)

            stats["elapsed"] = round(time.time() - started, 2)
            stats["bytes_h"] = human_size(stats["bytes"])
            stats["errors"] = stats["errors"][:20]
            stats["db"] = self.db.summary()
            if self.index is not None:
                stats["index_db"] = self.index.summary()
            self.last_run = stats
            self.log.info(
                "归档完成：扫描 %s，上传 %s（%s），分片 %s（%s 片），跳过 %s，失败 %s",
                stats["scanned"], stats["uploaded"], stats["bytes_h"], stats["chunked"],
                stats["parts"], stats["skipped"], stats["failed"],
            )
            return stats
        finally:
            self._busy.release()

    def _upload_chunked(self, f: FoundFile, sha: str, remote: str, repo: str, branch: str,
                        token: str, chunk_mb: float, ck: dict) -> dict[str, Any]:
        """切片、上传分片与清单，并把父文件登记到索引。"""
        parts_dir = f"{remote}.parts"
        try:
            manifest = chunker.split(f.path, self._staging_dir() / sha[:12], size_mb=chunk_mb)
        except Exception as e:
            self.db.mark(sha, str(f.path), remote, f.size, f.kind, "pending", f"分片失败：{e}")
            return {"ok": False, "err": f"分片失败：{e}"}
        part_records: list[dict] = []
        for name, ppath in chunker.iter_parts(manifest):
            try:
                payload = ppath.read_bytes()
            except OSError as e:
                return {"ok": False, "err": f"读取分片失败：{e}"}
            ok, err = self.uploader.put(repo, branch, f"{parts_dir}/{name}", payload, token,
                                        f"kb: part {name}")
            if not ok:
                self.db.mark(sha, str(f.path), parts_dir, f.size, f.kind, "pending", err)
                return {"ok": False, "err": f"分片上传失败 {name}: {err}"}
            rec = {"name": name, "bytes": len(payload), "sha256": next(
                (p.get("sha256") for p in manifest["parts"] if p["name"] == name), "")}
            part_records.append(rec)
            if self.index is not None:
                self.index.add_part(rec["sha256"], sha, f"{parts_dir}/{name}", rec["name"][-8:] and 0, len(payload))
        manifest["parts_dir"] = parts_dir
        manifest["original_remote"] = remote
        ok, err = self.uploader.put(repo, branch, f"{parts_dir}/{f.path.name}.parts.json",
                                    json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                                    token, f"kb: manifest {f.path.name}")
        if not ok:
            self.db.mark(sha, str(f.path), parts_dir, f.size, f.kind, "pending", err)
            return {"ok": False, "err": f"清单上传失败：{err}"}
        self.db.mark(sha, str(f.path), parts_dir, f.size, f.kind, "chunked")
        self._remember(sha, parts_dir, f, chunks=part_records)
        if not ck.get("keep_parts", True):
            chunker.cleanup(manifest)
        self.log.info("分片上传完成 %s（%s 片）", f.path.name, len(part_records))
        return {"ok": True, "parts": len(part_records), "parts_dir": parts_dir}

    # --- 索引 ---
    def upload_index(self, repo: str = "", branch: str = "", token: str = "") -> int:
        idx_cfg = self.rules.index
        up = self.rules.upload
        repo = repo or up.get("repo", "")
        branch = branch or up.get("branch", "main")
        token = token or self._token()
        if not repo or self.index is None:
            return 0
        writer = RepoIndex(self.index, device_id=self.device_id, branch=branch)
        count = 0
        for path, content in writer.files(path=idx_cfg.get("path") or "kb/_index/index.json",
                                          per_kind=bool(idx_cfg.get("per_kind", True))):
            ok, err = self.uploader.put(repo, branch, path, content.encode("utf-8"), token,
                                        f"index: update {path}")
            if ok:
                count += 1
            else:
                self.log.debug("索引上传失败 %s：%s", path, err)
        if count:
            self.log.info("索引已更新，共 %s 个文件", count)
        return count
