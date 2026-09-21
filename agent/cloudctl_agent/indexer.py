"""归档索引：去重判定与可检索索引文件。

两层：本地 sqlite（home/index.db）记每个 sha256 对应的仓库路径、字节、类型与分片，
用于防止重复上传；上传完再把条目写成仓库里的 kb/_index/index.json（可按类型分文件），
其他人 clone 后不用拉全文就能检索。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    sha256 TEXT PRIMARY KEY,
    repo_path TEXT NOT NULL,
    local_path TEXT,
    bytes INTEGER DEFAULT 0,
    kind TEXT DEFAULT '',
    mtime REAL DEFAULT 0,
    device TEXT DEFAULT '',
    chunks TEXT DEFAULT '',
    extra TEXT DEFAULT '',
    ts REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_entries_path ON entries(repo_path);
CREATE INDEX IF NOT EXISTS idx_entries_kind ON entries(kind);
CREATE TABLE IF NOT EXISTS parts (
    part_sha256 TEXT PRIMARY KEY,
    parent_sha256 TEXT,
    repo_path TEXT,
    bytes INTEGER DEFAULT 0,
    index_no INTEGER DEFAULT 0,
    ts REAL DEFAULT 0
);
"""


class LocalIndex:
    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

    # --- 去重 ---
    def has(self, sha256: str) -> dict[str, Any] | None:
        with self._lock:
            cur = self._db.execute("SELECT repo_path, bytes, kind, ts, chunks FROM entries WHERE sha256=?",
                                   (sha256,))
            row = cur.fetchone()
        if not row:
            return None
        return {"repo_path": row[0], "bytes": row[1], "kind": row[2], "ts": row[3],
                "chunks": json.loads(row[4]) if row[4] else []}

    def has_path(self, repo_path: str) -> bool:
        with self._lock:
            cur = self._db.execute("SELECT 1 FROM entries WHERE repo_path=? LIMIT 1", (repo_path,))
            return cur.fetchone() is not None

    # --- 写入 ---
    def add(self, sha256: str, repo_path: str, **kw: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO entries (sha256, repo_path, local_path, bytes, kind, mtime,"
                " device, chunks, extra, ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sha256, repo_path, kw.get("local_path", ""), int(kw.get("bytes") or 0),
                 kw.get("kind", ""), float(kw.get("mtime") or 0), kw.get("device", ""),
                 json.dumps(kw.get("chunks") or [], ensure_ascii=False),
                 json.dumps(kw.get("extra") or {}, ensure_ascii=False), time.time()))
            self._db.commit()

    def add_part(self, part_sha256: str, parent_sha256: str, repo_path: str,
                 index_no: int, nbytes: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO parts (part_sha256, parent_sha256, repo_path, bytes, index_no, ts)"
                " VALUES (?,?,?,?,?,?)",
                (part_sha256, parent_sha256, repo_path, int(nbytes), int(index_no), time.time()))
            self._db.commit()

    # --- 查询 ---
    def search(self, keyword: str, limit: int = 50) -> list[dict]:
        like = f"%{keyword}%"
        with self._lock:
            cur = self._db.execute(
                "SELECT repo_path, bytes, kind, device, ts FROM entries"
                " WHERE repo_path LIKE ? OR kind LIKE ? ORDER BY ts DESC LIMIT ?",
                (like, like, int(limit)))
            rows = cur.fetchall()
        return [{"repo_path": r[0], "bytes": r[1], "kind": r[2], "device": r[3], "ts": r[4]} for r in rows]

    def entries(self, limit: int = 20000) -> list[dict]:
        with self._lock:
            cur = self._db.execute(
                "SELECT sha256, repo_path, bytes, kind, mtime, device, chunks FROM entries"
                " ORDER BY ts DESC LIMIT ?", (int(limit),))
            rows = cur.fetchall()
        out = []
        for r in rows:
            out.append({"sha256": r[0], "repo_path": r[1], "bytes": r[2], "kind": r[3],
                        "mtime": r[4], "device": r[5],
                        "chunks": json.loads(r[6]) if r[6] else []})
        return out

    def summary(self) -> dict[str, Any]:
        with self._lock:
            total = self._db.execute("SELECT COUNT(*), COALESCE(SUM(bytes),0) FROM entries").fetchone()
            parts = self._db.execute("SELECT COUNT(*), COALESCE(SUM(bytes),0) FROM parts").fetchone()
            by_kind = dict(self._db.execute(
                "SELECT kind, COUNT(*) FROM entries GROUP BY kind").fetchall())
        return {"entries": int(total[0] or 0), "bytes": int(total[1] or 0),
                "parts": int(parts[0] or 0), "part_bytes": int(parts[1] or 0),
                "by_kind": by_kind}


class RepoIndex:
    """把本地索引渲染成可提交到仓库的 JSON。"""

    def __init__(self, index: LocalIndex, device_id: str = "", branch: str = "main") -> None:
        self.index = index
        self.device_id = device_id
        self.branch = branch

    def build(self, max_entries: int = 20000) -> dict[str, Any]:
        items = self.index.entries(max_entries)
        return {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "device": self.device_id, "branch": self.branch,
                "count": len(items), "schema": 1, "dedup": "sha256", "entries": items}

    def build_per_kind(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for it in self.index.entries(20000):
            kind = it.get("kind") or "other"
            bucket = out.setdefault(kind, {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                          "device": self.device_id, "kind": kind, "entries": []})
            bucket["entries"].append(it)
        for k, v in out.items():
            v["count"] = len(v["entries"])
        return out

    @staticmethod
    def render(data: dict[str, Any]) -> str:
        return json.dumps(data, ensure_ascii=False, indent=2)

    def files(self, path: str = "kb/_index/index.json", per_kind: bool = True) -> Iterator[tuple[str, str]]:
        yield path, self.render(self.build())
        if per_kind:
            base = path.rsplit("/", 1)[0]
            for kind, data in self.build_per_kind().items():
                yield f"{base}/kind_{kind}.json", self.render(data)
