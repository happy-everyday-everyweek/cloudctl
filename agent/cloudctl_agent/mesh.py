"""局域网互联：邻居发现 + 点对点命令转发 + 文件互传。

本次修复（安全）：以前令牌会回退到 device_id，而 device_id 由主机名与 MAC 派生、
可枚举，导致未配令牌时同网段任何人都能执行命令。现在规定：
  未显式配置互联令牌（mesh_token / devsrv_token / server_token 全空）时，
  只开放 /mesh/hello 与 /mesh/peers 两个只读端点，命令、中继、文件读写全部 403。

另修复：删掉 _exec 里的死代码；新增 on_event 回调，邻居上线下线可以上报出去。
只用标准库，不引入任何 UI 依赖。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import socket
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import ops

PROTOCOL = "cloudctl-mesh"
VERSION = 1
BROADCAST = "255.255.255.255"
BLOB_MAX_MB = 32
MAX_SEEN = 2000
READ_ONLY_PATHS = {"/mesh/hello", "/mesh/peers"}


class MeshService:
    """局域网内的一个对等节点。"""

    def __init__(self, cfg, log, run_cmd: Callable[..., dict] | None = None,
                 agent_version: str = "0.0.0",
                 on_event: Callable[[dict], None] | None = None) -> None:
        self.cfg = cfg
        self.log = log
        self.run_cmd = run_cmd or (lambda op, args, timeout=60.0: {"ok": False, "err": "命令执行器未就绪"})
        self.version = agent_version
        self.on_event = on_event or (lambda obj: None)
        self.mesh = cfg.mesh_scope
        self.token = cfg.mesh_secret
        self.explicit_token = bool(cfg.mesh_has_explicit_token)
        self.accept_cmd = bool(cfg.mesh_accept_cmd) and self.explicit_token
        self.peers: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}
        self._udp: socket.socket | None = None
        self._httpd: ThreadingHTTPServer | None = None
        self._stop = threading.Event()
        self.started_ts = 0.0
        self.recv_announce = 0
        self.relayed = 0
        self.rejected = 0

    # ------------------------------------------------------------ 身份
    def identity(self) -> dict:
        return {
            "v": VERSION, "kind": "announce", "proto": PROTOCOL, "mesh": self.mesh,
            "from": self.cfg.device_id, "from_name": self.cfg.device_name,
            "group": self.cfg.group, "port": int(self.cfg.mesh_port),
            "devconsole": self.cfg.devconsole_url, "mesh_port": int(self.cfg.mesh_port),
            "ver": self.version, "ts": time.time(),
            "caps": (["cmd", "blob", "pull", "relay"] if self.explicit_token else ["readonly"]),
            "auth": "token" if self.explicit_token else "open-readonly",
        }

    # ------------------------------------------------------------ 生命周期
    def start(self) -> dict:
        if not self.cfg.mesh_enabled:
            return {"enabled": False}
        if self._udp is not None:
            return {"enabled": True, "already": True}
        if not self.explicit_token:
            self.log.warning("互联未设置令牌，已降级为只读模式（仅发现与列邻居），命令与文件互传关闭")
        self.started_ts = time.time()
        try:
            self._udp = self._open_udp()
        except Exception as e:
            self.log.warning("mesh 发现端口打开失败：%s", e)
            self._udp = None
        handler = _make_handler(self)
        host = self.cfg.mesh_bind or "0.0.0.0"
        self._httpd = ThreadingHTTPServer((host, int(self.cfg.mesh_port)), handler)
        self._httpd.daemon_threads = True
        threading.Thread(target=self._httpd.serve_forever, name="mesh-http", daemon=True).start()
        threading.Thread(target=self._udp_loop, name="mesh-udp", daemon=True).start()
        threading.Thread(target=self._announce_loop, name="mesh-announce", daemon=True).start()
        self.log.info("局域网互联已启动 mesh=%s http=%s:%s 发现端口=%s 权限=%s",
                      self.mesh, host, self.cfg.mesh_port, self.cfg.mesh_discovery_port,
                      "可写" if self.explicit_token else "只读")
        return {"enabled": True, "mesh": self.mesh, "port": int(self.cfg.mesh_port),
                "mode": "rw" if self.explicit_token else "readonly"}

    def stop(self) -> dict:
        self._stop.set()
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        if self._udp is not None:
            try:
                self._udp.close()
            except Exception:
                pass
            self._udp = None
        return {"stopped": True}

    def status(self) -> dict:
        with self._lock:
            peers = len(self.peers)
        return {
            "enabled": bool(self.cfg.mesh_enabled), "mesh": self.mesh,
            "listening": self._httpd is not None, "port": int(self.cfg.mesh_port),
            "discovery_port": int(self.cfg.mesh_discovery_port), "peers": peers,
            "recv_announce": self.recv_announce, "relayed": self.relayed,
            "rejected": self.rejected, "accept_cmd": bool(self.accept_cmd),
            "auth": "token" if self.explicit_token else "open-readonly",
            "relay_enabled": bool(self.cfg.mesh_relay),
            "uptime_s": int(time.time() - self.started_ts) if self.started_ts else 0,
        }

    # ------------------------------------------------------------ 发现
    def _open_udp(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception:
            pass
        s.bind(("", int(self.cfg.mesh_discovery_port)))
        try:
            mreq = socket.inet_aton(self.cfg.mesh_mcast) + socket.inet_aton("0.0.0.0")
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except Exception as e:
            self.log.debug("加入组播失败，退化为广播：%s", e)
        s.settimeout(1.0)
        return s

    def _announce_loop(self) -> None:
        while not self._stop.is_set():
            self._send_announce()
            self._prune()
            self._stop.wait(max(5, int(self.cfg.mesh_announce_s)))

    def _send_announce(self) -> None:
        if self._udp is None:
            return
        payload = json.dumps(self.identity(), ensure_ascii=False).encode("utf-8")
        for host in (self.cfg.mesh_mcast, BROADCAST):
            try:
                self._udp.sendto(payload, (host, int(self.cfg.mesh_discovery_port)))
            except Exception:
                continue

    def _udp_loop(self) -> None:
        while not self._stop.is_set():
            s = self._udp
            if s is None:
                time.sleep(2)
                continue
            try:
                raw, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            except Exception:
                time.sleep(1)
                continue
            try:
                msg = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            if not isinstance(msg, dict) or msg.get("proto") != PROTOCOL:
                continue
            if msg.get("kind") != "announce" or msg.get("mesh") != self.mesh:
                continue
            self._register(msg, addr[0])

    def _register(self, msg: dict, addr: str) -> None:
        did = str(msg.get("from") or "")
        if not did or did == self.cfg.device_id:
            return
        with self._lock:
            known = did in self.peers
            self.peers[did] = {
                "device_id": did, "name": msg.get("from_name") or did,
                "group": msg.get("group") or "", "addr": addr,
                "port": int(msg.get("port") or self.cfg.mesh_port),
                "devconsole": msg.get("devconsole") or "",
                "ver": msg.get("ver") or "", "last_seen": time.time(),
            }
        self.recv_announce += 1
        if not known:
            self.log.info("发现邻居 %s（%s）%s", did, msg.get("from_name") or "", addr)
            self.on_event({"type": "event", "event": "mesh.peer_up",
                           "data": {"peer": did, "name": msg.get("from_name") or "", "addr": addr}})

    def _prune(self) -> None:
        limit = time.time() - max(30, int(self.cfg.mesh_ttl_s))
        with self._lock:
            gone = [k for k, v in self.peers.items() if v.get("last_seen", 0) < limit]
            for did in gone:
                self.peers.pop(did, None)
        for did in gone:
            self.on_event({"type": "event", "event": "mesh.peer_down", "data": {"peer": did}})

    def peers_list(self) -> list[dict]:
        with self._lock:
            return sorted(self.peers.values(), key=lambda p: str(p.get("device_id")))

    # ------------------------------------------------------------ 与对端通信
    def _post(self, addr: str, port: int, path: str, payload: dict, timeout: float = 20.0) -> dict | None:
        url = f"http://{addr}:{int(port)}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json", "X-Mesh-Token": self.token,
            "User-Agent": "cloudctl-mesh"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            self.log.debug("mesh 请求失败 %s: %s", url, e)
            return None

    def _get_peer(self, device_id: str) -> dict | None:
        with self._lock:
            return self.peers.get(device_id)

    def send_cmd(self, target: str, op: str, args: dict, timeout: float = 60.0) -> dict:
        if not self.explicit_token:
            return {"ok": False, "err": "未配置互联令牌，不能向对端下发命令"}
        env = self._envelope(kind="cmd", to=target)
        env.update({"op": op, "args": args or {}, "timeout": timeout})
        if target == self.cfg.device_id:
            return self._exec(env)
        peer = self._get_peer(target)
        if peer is not None:
            res = self._post(peer["addr"], peer["port"], "/mesh/cmd", env, timeout=timeout + 15)
            if res is not None:
                return res
        if not self.cfg.mesh_relay:
            return {"ok": False, "err": f"邻居 {target} 不可达，且未开启中继"}
        for relay in self.peers_list():
            if relay["device_id"] in (target, self.cfg.device_id):
                continue
            env2 = dict(env)
            env2["hops"] = 1
            res = self._post(relay["addr"], relay["port"], "/mesh/relay", env2, timeout=timeout + 20)
            if res is not None and res.get("ok"):
                self.relayed += 1
                return res
        return {"ok": False, "err": f"邻居 {target} 不可达"}

    def _envelope(self, kind: str, to: str = "*") -> dict:
        return {"v": VERSION, "proto": PROTOCOL, "kind": kind, "mesh": self.mesh,
                "from": self.cfg.device_id, "from_name": self.cfg.device_name,
                "to": to, "id": uuid.uuid4().hex, "ts": time.time(), "hops": 0}

    def _dedup(self, env: dict) -> bool:
        eid = str(env.get("id") or "")
        if not eid:
            return False
        now = time.time()
        if len(self._seen) > MAX_SEEN:
            for k, t in list(self._seen.items()):
                if now - t > 300:
                    self._seen.pop(k, None)
        if eid in self._seen:
            return True
        self._seen[eid] = now
        return False

    def _exec(self, env: dict) -> dict:
        if not self.accept_cmd:
            self.rejected += 1
            return {"ok": False, "err": "本机不接受对端命令（未配令牌或 mesh_accept_cmd=false）"}
        if self._dedup(env):
            return {"ok": False, "err": "重复请求，已忽略"}
        op = str(env.get("op") or "")
        args = env.get("args") or {}
        timeout = float(env.get("timeout") or 60)
        res = self.run_cmd(op, args, timeout)
        return {"ok": bool(res.get("ok")), "op": op, "from": self.cfg.device_id,
                "via": env.get("from"), "hops": int(env.get("hops") or 0),
                "data": res.get("data"), "err": res.get("err")}

    def _forward(self, env: dict) -> dict:
        if not self.explicit_token:
            return {"ok": False, "err": "未配置互联令牌，不提供中继"}
        if not self.cfg.mesh_relay:
            return {"ok": False, "err": "本机未开启中继"}
        hops = int(env.get("hops") or 0)
        if hops >= max(1, int(self.cfg.mesh_max_hops)):
            self.rejected += 1
            return {"ok": False, "err": "超过最大跳数"}
        target = str(env.get("to") or "")
        if target == self.cfg.device_id:
            return self._exec(env)
        peer = self._get_peer(target)
        if peer is None:
            return {"ok": False, "err": f"本机也不知道 {target} 在哪"}
        env2 = dict(env)
        env2["hops"] = hops + 1
        res = self._post(peer["addr"], peer["port"], "/mesh/relay", env2)
        if res is not None:
            self.relayed += 1
            return res
        return {"ok": False, "err": f"转发到 {target} 失败"}

    # ------------------------------------------------------------ 文件
    def _blob_dir(self, peer: str) -> Path:
        p = self.cfg.home_path / "peers" / "".join(ch for ch in peer if ch.isalnum() or ch in "-_")
        p.mkdir(parents=True, exist_ok=True)
        return p

    def save_blob(self, env: dict) -> dict:
        if not self.explicit_token:
            self.rejected += 1
            return {"ok": False, "err": "未配置互联令牌，不接受文件"}
        raw = base64.b64decode(env.get("data_b64") or "")
        if len(raw) > BLOB_MAX_MB * 1024 * 1024:
            return {"ok": False, "err": f"超过 {BLOB_MAX_MB}MB 上限"}
        sha = hashlib.sha256(raw).hexdigest()
        if env.get("sha256") and env["sha256"] != sha:
            return {"ok": False, "err": "sha256 校验失败"}
        name = Path(str(env.get("name") or "blob.bin")).name or "blob.bin"
        dest = self._blob_dir(str(env.get("from") or "unknown")) / name
        dest.write_bytes(raw)
        self.log.info("收到对端文件 %s（%s 字节）from=%s", dest, len(raw), env.get("from"))
        self.on_event({"type": "event", "event": "mesh.blob_in",
                       "data": {"path": str(dest), "bytes": len(raw), "from": env.get("from")}})
        return {"ok": True, "path": str(dest), "bytes": len(raw), "sha256": sha}

    def send_blob(self, target: str, path: str, name: str = "") -> dict:
        if not self.explicit_token:
            return {"ok": False, "err": "未配置互联令牌，不能发送文件"}
        peer = self._get_peer(target)
        if peer is None:
            return {"ok": False, "err": f"邻居 {target} 不在线"}
        try:
            raw = Path(path).read_bytes()
        except Exception as e:
            return {"ok": False, "err": f"读取失败：{e}"}
        if len(raw) > BLOB_MAX_MB * 1024 * 1024:
            return {"ok": False, "err": f"超过 {BLOB_MAX_MB}MB 上限，建议改用 pull 分段"}
        env = self._envelope("blob", to=target)
        env.update({"name": name or Path(path).name,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "data_b64": base64.b64encode(raw).decode("ascii")})
        res = self._post(peer["addr"], peer["port"], "/mesh/blob", env, timeout=180)
        return res or {"ok": False, "err": "发送失败"}


# ------------------------------------------------------------------ HTTP 路由
class _Handler(BaseHTTPRequestHandler):
    server_version = "cloudctl-mesh"
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        self.ms: MeshService = kw.pop("_mesh")
        super().__init__(*a, **kw)

    def log_message(self, fmt: str, *args) -> None:
        return

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _authed(self) -> bool:
        got = self.headers.get("X-Mesh-Token") or ""
        return hmac.compare_digest(str(got), str(self.ms.token))

    def _guard(self, path: str) -> bool:
        """返回 True 表示可以继续处理。"""
        if self.ms.explicit_token:
            if self._authed():
                return True
            self.ms.rejected += 1
            self._json({"ok": False, "err": "缺少或错误的 X-Mesh-Token"}, 401)
            return False
        if path in READ_ONLY_PATHS:
            return True
        self.ms.rejected += 1
        self._json({"ok": False, "err": "本机未配置互联令牌，仅开放只读端点"}, 403)
        return False

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        if not self._guard(parsed.path):
            return
        if parsed.path == "/mesh/hello":
            return self._json({"ok": True, "identity": self.ms.identity(), "peers": self.ms.peers_list()})
        if parsed.path == "/mesh/peers":
            return self._json({"ok": True, "data": self.ms.peers_list(), "status": self.ms.status()})
        if parsed.path == "/mesh/pull":
            try:
                data = ops.file_pull(q.get("path", [""])[0], int(q.get("offset", ["0"])[0]),
                                     int(q.get("length", [str(256 * 1024)])[0]))
                return self._json({"ok": True, "data": data})
            except Exception as e:
                return self._json({"ok": False, "err": str(e)}, 500)
        return self._json({"ok": False, "err": "未知路径"}, 404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if self.ms.explicit_token and not self._authed():
            self.ms.rejected += 1
            return self._json({"ok": False, "err": "缺少或错误的 X-Mesh-Token"}, 401)
        if not self._guard(parsed.path):
            return
        body = self._body()
        if str(body.get("mesh") or "") and body.get("mesh") != self.ms.mesh:
            self.ms.rejected += 1
            return self._json({"ok": False, "err": "mesh 组不匹配"}, 403)
        if body.get("from"):
            self.ms._register({"from": body["from"], "from_name": body.get("from_name"),
                               "group": body.get("group"),
                               "port": body.get("port") or self.ms.cfg.mesh_port,
                               "ver": body.get("ver")}, self.client_address[0] if self.client_address else "")
        if parsed.path == "/mesh/cmd":
            return self._json(self.ms._exec(body))
        if parsed.path == "/mesh/relay":
            return self._json(self.ms._forward(body))
        if parsed.path == "/mesh/blob":
            try:
                return self._json(self.ms.save_blob(body))
            except Exception as e:
                return self._json({"ok": False, "err": str(e)}, 500)
        return self._json({"ok": False, "err": "未知路径"}, 404)


def _make_handler(ms: MeshService):
    def _factory(*a, **kw):
        return _Handler(*a, _mesh=ms, **kw)

    return _factory
