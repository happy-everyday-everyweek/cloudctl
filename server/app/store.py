"""服务端存储：设备表、规则表、审计表。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                name TEXT, grp TEXT, os TEXT, ver TEXT,
                first_seen INTEGER, last_seen INTEGER, ip TEXT, info TEXT
            );
            CREATE TABLE IF NOT EXISTS rules (
                scope TEXT, scope_value TEXT, version INTEGER, data TEXT, updated INTEGER,
                PRIMARY KEY (scope, scope_value)
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT, op TEXT, args TEXT, ok INTEGER, result TEXT, ts INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_audit_dev ON audit(device_id, ts);
            """
        )
        self.conn.commit()

    # ---------------------------------------------------------- 设备
    def upsert_device(self, device_id: str, name: str, grp: str, os_: str, ver: str, ip: str, info: dict) -> None:
        now = int(time.time())
        with self._lock:
            self.conn.execute(
                "INSERT INTO devices (device_id, name, grp, os, ver, first_seen, last_seen, ip, info)"
                " VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(device_id) DO UPDATE SET name=excluded.name, grp=excluded.grp, os=excluded.os,"
                " ver=excluded.ver, last_seen=excluded.last_seen, ip=excluded.ip, info=excluded.info",
                (device_id, name, grp, os_, ver, now, now, ip, json.dumps(info, ensure_ascii=False)),
            )
            self.conn.commit()

    def touch(self, device_id: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE devices SET last_seen=? WHERE device_id=?", (int(time.time()), device_id))
            self.conn.commit()

    def list_devices(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["info"] = json.loads(d.get("info") or "{}")
            except Exception:
                d["info"] = {}
            out.append(d)
        return out

    def get_device(self, device_id: str) -> dict | None:
        with self._lock:
            r = self.conn.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
        return dict(r) if r else None

    # ---------------------------------------------------------- 规则
    def set_rules(self, scope: str, scope_value: str, data: dict, version: int | None = None) -> int:
        with self._lock:
            r = self.conn.execute(
                "SELECT version FROM rules WHERE scope=? AND scope_value=?", (scope, scope_value)
            ).fetchone()
            ver = int(version or ((r["version"] + 1) if r else 1))
            payload = dict(data)
            payload["version"] = ver
            self.conn.execute(
                "INSERT INTO rules (scope, scope_value, version, data, updated) VALUES (?,?,?,?,?)"
                " ON CONFLICT(scope, scope_value) DO UPDATE SET version=excluded.version, data=excluded.data, updated=excluded.updated",
                (scope, scope_value, ver, json.dumps(payload, ensure_ascii=False), int(time.time())),
            )
            self.conn.commit()
        return ver

    def get_rules(self, scope: str, scope_value: str) -> dict | None:
        with self._lock:
            r = self.conn.execute(
                "SELECT data, version FROM rules WHERE scope=? AND scope_value=?", (scope, scope_value)
            ).fetchone()
        if not r:
            return None
        try:
            return json.loads(r["data"])
        except Exception:
            return None

    def rules_for_device(self, device_id: str, grp: str = "default") -> dict | None:
        """设备级规则优先，没有则回退分组规则，再回退全局。"""
        for scope, value in (("device", device_id), ("group", grp or "default"), ("global", "all")):
            r = self.get_rules(scope, value)
            if r:
                return r
        return None

    def list_rules(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM rules ORDER BY updated DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["data"] = json.loads(d["data"])
            except Exception:
                pass
            out.append(d)
        return out

    # ---------------------------------------------------------- 审计
    def audit(self, device_id: str, op: str, args: Any, ok: bool, result: Any) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO audit (device_id, op, args, ok, result, ts) VALUES (?,?,?,?,?,?)",
                (device_id, op, json.dumps(args, ensure_ascii=False)[:4000], 1 if ok else 0,
                 json.dumps(result, ensure_ascii=False)[:8000], int(time.time())),
            )
            self.conn.commit()

    def list_audit(self, device_id: str = "", limit: int = 100) -> list[dict]:
        with self._lock:
            if device_id:
                rows = self.conn.execute(
                    "SELECT * FROM audit WHERE device_id=? ORDER BY id DESC LIMIT ?", (device_id, int(limit))
                ).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
