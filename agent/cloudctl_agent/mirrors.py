"""GitHub 镜像池：候选排序、健康打分、自探测、用户清单导入。

镜像前缀语义与 GitLink 一致：最终链接 = prefix + 原始 URL。
内置清单取自用户自己的 GitLink 项目（happy-everyday-everyweek/gitlink，
app/src/main/java/com/ghlink/app/core/Mirror.kt），共 38 条（含直连）。

所有状态持久化到工作目录的 mirrors.json：清单本身与健康计数分开放，
方便你从 GitLink 导出 JSON 后直接覆盖清单。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------- 内置清单
BUILTIN_RAW: list[tuple[str, str, str, str]] = [
    ("direct", "直连", "", "raw.githubusercontent.com 直连"),
    ("cxkpro", "Cxkpro", "https://ghproxy.cxkpro.top", "镜像 455ms"),
    ("gh-proxy", "Gh-Proxy", "https://gh-proxy.com", "镜像 635ms"),
    ("geekertao", "GeekerTao", "https://ghfile.geekertao.top", "镜像 1787ms"),
    ("nxnow", "Nxnow", "https://gh.nxnow.top", "镜像 1957ms"),
    ("ghfast", "Ghfast", "https://ghfast.top", "镜像 2215ms"),
    ("npee", "Npee", "https://down.npee.cn/?", "镜像 2551ms"),
    ("jasonzeng", "JasonZeng", "https://gh.jasonzeng.dev", "镜像 2876ms"),
    ("chjina", "Chjina", "https://gh.chjina.com", "镜像 2999ms"),
    ("monlor", "Monlor", "https://gh.monlor.com", "镜像 3720ms"),
    ("ghproxynethyph", "GhProxyNetHyph", "https://gh-proxy.net", "镜像 3583ms"),
    ("ednovas", "Ednovas", "https://github.ednovas.xyz", "镜像 4060ms"),
    ("ghproxynet", "GhProxyNet", "https://ghproxy.net", "镜像 4418ms"),
    ("github", "GitHub原始链接", "https://github.com", "官方直连 4683ms"),
    ("zwy", "Zwy", "https://gh.zwy.one", "镜像 4911ms"),
    ("mrhjx", "Mrhjx", "https://gitproxy.mrhjx.cn", "镜像 5005ms"),
    ("bokimoe", "BokiMoe", "https://github.boki.moe", "镜像 5301ms"),
    ("fastgitcc", "FastGitCc", "https://fastgit.cc", "镜像 5374ms"),
    ("crashmc", "CrashMc", "https://cdn.crashmc.com", "镜像 6065ms"),
    ("monkeyray", "Monkeyray", "https://ghproxy.monkeyray.net", "镜像 6085ms"),
    ("firewall", "Firewall", "https://firewall.lxstd.org", "UnknownHost"),
    ("gh188", "Gh188", "https://ghproxy.1888866.xyz", "SocketTimeout"),
    ("ghproxycfd", "GhPro", "https://ghproxy.cfd", "SocketTimeout"),
    ("gitmirror", "GitMirr", "https://hub.gitmirror.com", "UnknownHost"),
    ("limoru", "Limoru", "https://github.limoruirui.com", "SocketTimeout"),
    ("likk", "LIkk", "https://gh.llkk.cc", "SocketTimeout"),
    ("moeyy", "Moeyy", "https://github.moeyy.xyz", "SocketTimeout"),
    ("ghproxymirror", "GhProxyMirror", "https://mirror.ghproxy.com", "镜像"),
    ("workers", "Workers", "https://github.abskoop.workers.dev", "镜像"),
    ("tbedu", "Tbedu", "https://github.tbedu.top", "镜像"),
    ("yylx", "Yylx", "https://git.yylx.win", "镜像"),
    ("xxooo", "Xxooo", "https://gh.xxooo.cf", "镜像"),
    ("xx9527", "Xx9527", "https://gh.xx9527.cn", "镜像"),
]


def _norm_prefix(p: str) -> str:
    p = (p or "").strip()
    if not p:
        return ""
    return p if p.endswith(("/", "?")) else p + "/"


def builtin_list() -> list[dict]:
    return [{"id": i, "name": n, "prefix": _norm_prefix(p), "note": note, "builtin": True}
            for i, n, p, note in BUILTIN_RAW]


class MirrorPool:
    """一个带健康记忆的镜像候选池。"""

    def __init__(self, home: Path, log, pool_json: str = "", top: int = 4) -> None:
        self.home = Path(home)
        self.log = log
        self.top = max(1, int(top))
        self.file = self.home / "mirrors.json"
        self.items: list[dict] = builtin_list()
        self.health: dict[str, dict] = {}
        self.load(pool_json)

    # --- 持久化 ---
    def load(self, pool_json: str = "") -> None:
        raw: dict[str, Any] = {}
        if self.file.exists():
            try:
                raw = json.loads(self.file.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
        if isinstance(raw.get("list"), list) and raw["list"]:
            self.items = [self._coerce(x) for x in raw["list"] if isinstance(x, dict)]
        if isinstance(raw.get("health"), dict):
            self.health = raw["health"]
        if pool_json:
            self.import_text(pool_json, save=False)

    def _coerce(self, x: dict) -> dict:
        mid = str(x.get("id") or "").strip() or f"m{abs(hash(str(x.get('prefix')))) % 100000}"
        return {"id": mid, "name": str(x.get("name") or mid),
                "prefix": _norm_prefix(str(x.get("prefix") or "")),
                "note": str(x.get("note") or ""), "builtin": bool(x.get("builtin", False))}

    def import_text(self, text: str, save: bool = True) -> int:
        """导入 GitLink 导出的镜像 JSON（{\"list\":[...]} 或直接数组）。"""
        try:
            data = json.loads(text)
        except Exception as e:
            self.log.warning("镜像清单解析失败：%s", e)
            return 0
        arr = data.get("list") if isinstance(data, dict) else data
        if not isinstance(arr, list) or not arr:
            return 0
        extra = [self._coerce(x) for x in arr if isinstance(x, dict)]
        have = {m["id"] for m in self.items}
        added = [m for m in extra if m["id"] not in have]
        self.items = self.items + added
        if save:
            self.save()
        self.log.info("镜像清单已导入，新增 %s 条", len(added))
        return len(added)

    def import_file(self, path: str) -> int:
        p = Path(path)
        if not p.exists():
            self.log.warning("镜像清单文件不存在：%s", path)
            return 0
        return self.import_text(p.read_text(encoding="utf-8"))

    def save(self) -> None:
        try:
            self.file.write_text(json.dumps({"list": self.items, "health": self.health},
                                            ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # --- 健康度 ---
    def score(self, mid: str) -> float:
        h = self.health.get(mid) or {}
        ok = float(h.get("ok") or 0)
        fail = float(h.get("fail") or 0)
        lat = float(h.get("lat") or 0)
        streak = float(h.get("streak") or 0)
        s = ok * 2.0 - fail * 1.0 - streak * 3.0
        if lat > 0:
            s -= min(20.0, lat / 250.0)
        return s

    def report(self, mid: str, ok: bool, latency_ms: float = 0.0) -> None:
        h = self.health.setdefault(mid, {"ok": 0, "fail": 0, "lat": 0.0, "streak": 0, "ts": 0})
        h["ts"] = int(time.time())
        if ok:
            h["ok"] = int(h.get("ok") or 0) + 1
            h["streak"] = 0
            if latency_ms > 0:
                prev = float(h.get("lat") or 0)
                h["lat"] = latency_ms if prev <= 0 else (prev * 0.6 + latency_ms * 0.4)
        else:
            h["fail"] = int(h.get("fail") or 0) + 1
            h["streak"] = int(h.get("streak") or 0) + 1
        self.save()

    def ranking(self) -> list[str]:
        ids = [m["id"] for m in self.items]
        return sorted(ids, key=lambda i: self.score(i), reverse=True)

    # --- 候选 URL ---
    def candidates(self, url: str, prefer: str = "") -> list[tuple[str, str]]:
        """返回 (mirror_id, 实际 URL) 列表，按健康度从高到低。"""
        table = {m["id"]: m for m in self.items}
        out: list[tuple[str, str]] = []
        prefer = (prefer or "").strip()
        if prefer:
            pid = "prefer"
            table[pid] = {"id": pid, "name": "指定镜像", "prefix": _norm_prefix(prefer), "note": "", "builtin": False}
            out.append((pid, _norm_prefix(prefer) + url))
        for mid in self.ranking():
            m = table.get(mid)
            if not m:
                continue
            out.append((mid, (m["prefix"] or "") + url))
        seen = set()
        uniq: list[tuple[str, str]] = []
        for mid, u in out:
            if u in seen:
                continue
            seen.add(u)
            uniq.append((mid, u))
        return uniq

    def healthy(self, limit: int = 5) -> list[dict]:
        table = {m["id"]: m for m in self.items}
        out = []
        for mid in self.ranking()[:limit]:
            m = table.get(mid)
            if m:
                out.append({"id": mid, "name": m["name"], "score": round(self.score(mid), 2),
                            "health": self.health.get(mid) or {}})
        return out

    # --- 主动探测 ---
    def probe(self, getter: Callable[[str], tuple[bool, float, str]], probe_url: str,
              max_mirrors: int = 12) -> dict:
        """getter(url) 返回 (是否成功, 延迟毫秒, 说明)。探测结果写入健康计数。"""
        table = {m["id"]: m for m in self.items}
        results: dict[str, Any] = {}
        for mid in self.ranking()[:max_mirrors]:
            m = table.get(mid)
            if not m:
                continue
            ok, lat, note = getter((m["prefix"] or "") + probe_url)
            self.report(mid, ok, lat)
            results[mid] = {"ok": ok, "lat_ms": round(lat, 1), "note": note}
        self.save()
        best = [k for k, v in results.items() if v["ok"]]
        self.log.info("镜像探测完成，可用 %s/%s，最快 %s", len(best), len(results),
                      best[0] if best else "无")
        self.save()
        return results

    def status(self) -> dict:
        return {"count": len(self.items), "pool_file": str(self.file),
                "top": self.top, "best": self.healthy(6)}
