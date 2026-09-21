"""大文件分片：绕开 GitHub 单文件 100MB 硬限制。

做法：把超大文件切成一堆小片写到暂存目录，并生成一份清单（manifest），
清单里记录原始文件名、总大小、整体 sha256、每片的偏移与单独 sha256。
上传端只需要把分片当成普通文件传上去，下载后按清单拼回原文件。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterator

CHUNK_READ = 4 * 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(CHUNK_READ)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def needs_chunk(size_bytes: int, max_single_mb: float) -> bool:
    return size_bytes > max(1.0, float(max_single_mb)) * 1024 * 1024


def part_name(name: str, index: int, total: int) -> str:
    width = max(3, len(str(total)))
    return f"{name}.part{index + 1:0{width}d}"


def split(path: Path, staging: Path, size_mb: float = 40.0) -> dict[str, Any]:
    """切片并返回清单（不删除原文件）。"""
    path = Path(path)
    staging = Path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    size = path.stat().st_size
    chunk = max(1, int(float(size_mb) * 1024 * 1024))
    total = max(1, (size + chunk - 1) // chunk)
    parts: list[dict[str, Any]] = []
    with open(path, "rb") as src:
        for i in range(total):
            name = part_name(path.name, i, total)
            dest = staging / name
            written = 0
            h = hashlib.sha256()
            with open(dest, "wb") as out:
                while written < chunk:
                    buf = src.read(min(CHUNK_READ, chunk - written))
                    if not buf:
                        break
                    out.write(buf)
                    h.update(buf)
                    written += len(buf)
            parts.append({"name": name, "index": i, "offset": i * chunk,
                          "length": written, "sha256": h.hexdigest()})
    manifest = {"original": path.name, "source": str(path), "bytes": size,
                "sha256": sha256_file(path), "chunk_mb": float(size_mb),
                "parts": parts, "created_at": int(time.time()), "staging": str(staging)}
    mpath = staging / f"{path.name}.parts.json"
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest["manifest_path"] = str(mpath)
    return manifest


def merge(manifest_path: Path, out_path: Path, verify: bool = True) -> dict[str, Any]:
    """按清单把分片拼回原文件，可选校验整体 sha256。"""
    manifest_path = Path(manifest_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    staging = Path(data.get("staging") or manifest_path.parent)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    written = 0
    with open(out_path, "wb") as out:
        for p in sorted(data.get("parts") or [], key=lambda x: int(x.get("index") or 0)):
            src = staging / p["name"]
            with open(src, "rb") as f:
                while True:
                    buf = f.read(CHUNK_READ)
                    if not buf:
                        break
                    out.write(buf)
                    h.update(buf)
                    written += len(buf)
            if verify and p.get("sha256") and sha256_file(src) != p["sha256"]:
                raise RuntimeError(f"分片校验失败：{p['name']}")
    digest = h.hexdigest()
    ok = (not verify) or (not data.get("sha256")) or digest == data["sha256"]
    return {"ok": ok, "path": str(out_path), "bytes": written, "sha256": digest}


def iter_parts(manifest: dict[str, Any]) -> Iterator[tuple[str, Path]]:
    staging = Path(manifest.get("staging") or ".")
    for p in manifest.get("parts") or []:
        yield p["name"], staging / p["name"]


def cleanup(manifest: dict[str, Any]) -> int:
    """删除分片与清单，返回删除的文件数。"""
    removed = 0
    for _name, p in iter_parts(manifest):
        try:
            if p.exists():
                p.unlink()
                removed += 1
        except Exception:
            continue
    mp = manifest.get("manifest_path")
    if mp:
        try:
            Path(mp).unlink(missing_ok=True)
            removed += 1
        except Exception:
            pass
    return removed
