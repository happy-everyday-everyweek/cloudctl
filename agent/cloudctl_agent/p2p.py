"""设备间直连层（P2P 优先，转发兜底，尽量去中心化）。

设计参考 Piik 的思路：能直连就别过服务器；直连不成才交由任意一台
“对两边都通”的设备转发。中心只做两件事：告诉大家彼此在哪、以及实在
没法时当最后一个转发点。任何时候都不需要付费服务。

实现要点：
  - UDP 打洞：双方互相向对方的候选端点发 PUNCH，任一边收到 PONG 即置为直连。
  - 信封鉴权：所有包带 nonce 与 sig（token+nonce+from+to 的 sha256 前 16 位）。
  - 分片传输：大对象切 256KB 分片，FIN 带 size/sha256，接收端先写 .part 再改名。
  - 转发兜底：直连超时就交给 mesh 里对端可达的邻居（现有 /mesh/relay）。

只依赖标准库，不引入任何 UI 库。
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable

MAGIC = b"CCTL1"
KIND_PUNCH = "punch"
KIND_PONG = "pong"
KIND_PING = "ping"
KIND_DATA = "data"
KIND_ACK = "ack"
KIND_FIN = "fin"
KIND_MSG = "msg"
CHUNK = 256 * 1024          # 一次逻辑分片的大小
FRAG = 1000                 # UDP 单包有效负载，避开 64KB 上限与 MTU 碎片
MAX_DGRAM = 1150


def _sig(token: str, nonce: str, frm: str, to: str) -> str:
    h = hashlib.sha256(f"{token}|{nonce}|{frm}|{to}".encode("utf-8"))
    return h.hexdigest()[:16]


def _pack(kind: str, frm: str, to: str, token: str, extra: dict | None = None, blob: bytes = b"") -> bytes:
    nonce = os.urandom(6).hex()
    head: dict[str, Any] = {"k": kind, "f": frm, "t": to, "n": nonce,
                            "s": _sig(token, nonce, frm, to), "ts": int(time.time())}
    if extra:
        head.update(extra)
    raw = json.dumps(head, ensure_ascii=False).encode("utf-8")
    return MAGIC + struct.pack("!I", len(raw)) + raw + blob


def _unpack(pkt: bytes, token: str) -> tuple[dict, bytes] | None:
    if len(pkt) < 9 or not pkt.startswith(MAGIC):
        return None
    (n,) = struct.unpack("!I", pkt[5:9])
    if len(pkt) < 9 + n:
        return None
    try:
        head = json.loads(pkt[9:9 + n].decode("utf-8"))
    except Exception:
        return None
    if not isinstance(head, dict):
        return None
    if head.get("s") != _sig(token, str(head.get("n") or ""), str(head.get("f") or ""), str(head.get("t") or "")):
        return None
    return head, pkt[9 + n:]


class PeerPath:
    __slots__ = ("addr", "state", "last_rx", "rtt_ms", "via", "sent", "recv")

    def __init__(self, addr: tuple[str, int]) -> None:
        self.addr = addr
        self.state = "punching"
        self.last_rx = time.time()
        self.rtt_ms = -1
        self.via = "direct"
        self.sent = 0
        self.recv = 0

    def info(self) -> dict:
        return {"addr": f"{self.addr[0]}:{self.addr[1]}", "state": self.state, "via": self.via,
                "rtt_ms": self.rtt_ms, "age_s": int(time.time() - self.last_rx),
                "sent": self.sent, "recv": self.recv}

class P2PService:
    """直连优先、转发兼底的设备间通道。"""

    def __init__(self, cfg, log, on_message: Callable[[str, bytes], None] | None = None,
                 on_event: Callable[[dict], None] | None = None,
                 relay_send: Callable[[str, bytes], bool] | None = None) -> None:
        self.cfg = cfg
        self.log = log
        self.device_id = cfg.device_id
        self.token = cfg.p2p_secret
        self.on_message = on_message
        self.on_event = on_event
        self.relay_send = relay_send
        self.sock: socket.socket | None = None
        self.threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self.paths: dict[str, PeerPath] = {}
        self._lock = threading.Lock()
        self.rx_bytes = 0
        self.tx_bytes = 0
        self.relayed = 0
        self.dropped = 0
        self.inbox = cfg.home_path / "peers" / "p2p_inbox"
        self.rx_frags: dict[str, set[int]] = {}
        self.tx_cache: dict[str, tuple[bytes, dict]] = {}

    # ---------------------------------------------------------- 生命周期
    def start(self) -> dict:
        if not self.cfg.p2p_enabled:
            return {"enabled": False}
        if self.sock is not None:
            return {"enabled": True, "already": True}
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            except Exception:
                pass
            s.bind((self.cfg.p2p_bind or "0.0.0.0", int(self.cfg.p2p_port)))
            s.settimeout(1.0)
        except Exception as e:
            self.log.error("P2P 端口绑定失败：%s", e)
            return {"enabled": False, "err": str(e)}
        self.sock = s
        self.inbox.mkdir(parents=True, exist_ok=True)
        for name, fn in (("p2p-recv", self._recv_loop), ("p2p-keep", self._keep_loop)):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self.threads.append(t)
        self.log.info("P2P 直连层已就绪 udp=%s:%s 候选=%s", self.cfg.p2p_bind or "0.0.0.0",
                      self.cfg.p2p_port, [f"{h}:{p}" for h, p in self.candidates()])
        return {"enabled": True, "port": int(self.cfg.p2p_port),
                "candidates": [f"{h}:{p}" for h, p in self.candidates()]}

    def stop(self) -> dict:
        self._stop.set()
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
        return {"stopped": True}

    # ---------------------------------------------------------- 端点与路径
    def candidates(self) -> list[tuple[str, int]]:
        """本机可供对方尝试的端点：局网地址 + 手工声明的公网地址。"""
        out: list[tuple[str, int]] = []
        port = int(self.cfg.p2p_port)
        if self.cfg.p2p_public_host:
            out.append((self.cfg.p2p_public_host, int(self.cfg.p2p_public_port or port)))
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if ip and not ip.startswith("127.") and (ip, port) not in out:
                    out.append((ip, port))
        except Exception:
            pass
        out.append(("127.0.0.1", port))
        return out

    def add_peer(self, peer_id: str, endpoints: list[tuple[str, int]]) -> dict:
        if not endpoints or peer_id == self.device_id:
            return {"ok": False, "err": "无可用端点"}
        with self._lock:
            p = self.paths.get(peer_id)
            if p is None or p.addr != tuple(endpoints[0]):
                p = PeerPath(tuple(endpoints[0]))
                self.paths[peer_id] = p
            p.state = "punching"
        self._punch(peer_id, [tuple(e) for e in endpoints])
        return {"ok": True, "peer": peer_id, "trying": [f"{h}:{pp}" for h, pp in endpoints]}

    def _punch(self, peer_id: str, endpoints: list[tuple[str, int]]) -> None:
        assert self.sock is not None
        pkt = _pack(KIND_PUNCH, self.device_id, peer_id, self.token,
                    {"cands": [f"{h}:{pp}" for h, pp in self.candidates()]})
        for addr in endpoints:
            for _ in range(3):
                try:
                    self.sock.sendto(pkt, addr)
                except Exception:
                    pass
                time.sleep(0.05)

    def path_state(self, peer_id: str) -> str:
        p = self.paths.get(peer_id)
        return p.state if p else "unknown"

    # ---------------------------------------------------------- 收发
    def send(self, peer_id: str, payload: bytes, kind: str = KIND_MSG,
             extra: dict | None = None) -> bool:
        """直连通就直接发；不通就交给 relay_send（通常是 mesh 里对端可达的邻居）。"""
        if len(payload) > MAX_DGRAM:
            # 大块拆成多个 UDP 包，每包标上分片序号，接收端按偏移写盘
            n = (len(payload) + FRAG - 1) // FRAG
            okall = True
            for i in range(n):
                if not self._send_frag(peer_id, payload, extra or {}, kind, i, n):
                    okall = False
                if i and i % 64 == 0:
                    time.sleep(0.002)   # 稍作节流，别把对方接收缓冲击穿
            return okall
        p = self.paths.get(peer_id)
        pkt = _pack(kind, self.device_id, peer_id, self.token, extra, payload)
        if p is not None and p.state == "direct" and self.sock is not None:
            try:
                self.sock.sendto(pkt, p.addr)
                p.sent += 1
                self.tx_bytes += len(payload)
                return True
            except Exception as e:
                self.log.debug("直连发送失败，转中继：%s", e)
                p.state = "down"
        if self.relay_send is not None:
            try:
                if self.relay_send(peer_id, pkt):
                    self.relayed += 1
                    self.tx_bytes += len(payload)
                    if p is not None:
                        p.via = "relay"
                    return True
            except Exception as e:
                self.log.debug("中继失败：%s", e)
        self.dropped += 1
        return False

    def _send_frag(self, peer_id: str, data: bytes, extra: dict, kind: str,
                   idx: int, total: int) -> bool:
        piece = data[idx * FRAG:(idx + 1) * FRAG]
        ex = dict(extra)
        ex.update({"frag": idx, "frags": total})
        return self.send(peer_id, piece, kind, ex)

    def _resend(self, peer_id: str, name: str, missing: list[int]) -> int:
        cached = self.tx_cache.get(name)
        if cached is None:
            return 0
        data, extra = cached
        total = (len(data) + FRAG - 1) // FRAG
        done = 0
        for idx in missing:
            if 0 <= idx < total:
                self._send_frag(peer_id, data, extra, KIND_DATA, idx, total)
                done += 1
        self.send(peer_id, b"", KIND_FIN, extra)
        return done

    def push_file(self, peer_id: str, path: Path, name: str | None = None) -> dict:
        """分片推送一个大文件（升级包、录屏片段）。直连不通用中继逐片发。"""
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        fname = name or path.name
        total = (len(data) + CHUNK - 1) // CHUNK
        meta = {"name": fname, "total": total, "size": len(data)}
        self.tx_cache[fname] = (data, meta)
        while len(self.tx_cache) > 4:
            self.tx_cache.pop(next(iter(self.tx_cache)))
        ok = 0
        for seq in range(total):
            part = data[seq * CHUNK:(seq + 1) * CHUNK]
            ex = dict(meta)
            ex["seq"] = seq
            if not self.send(peer_id, part, KIND_DATA, ex):
                break
            ok += 1
        done = self.send(peer_id, b"", KIND_FIN,
                         {"name": fname, "sha256": digest, "size": len(data), "chunks": total})
        return {"ok": bool(done and ok == total), "name": fname, "chunks_sent": ok,
                "total": total, "bytes": len(data), "sha256": digest, "via": self.path_state(peer_id)}

    # ---------------------------------------------------------- 后台线程
    def _recv_loop(self) -> None:
        assert self.sock is not None
        while not self._stop.is_set():
            try:
                pkt, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            parsed = _unpack(pkt, self.token)
            if parsed is None:
                continue
            head, blob = parsed
            if head.get("t") not in (self.device_id, "*"):
                continue
            peer = str(head.get("f") or "")
            if not peer:
                continue
            self.rx_bytes += len(blob)
            kind = head.get("k")
            with self._lock:
                p = self.paths.get(peer)
                if p is None:
                    p = self.paths[peer] = PeerPath(addr)
                p.addr = addr
                p.last_rx = time.time()
                p.recv += 1
            if kind == KIND_PUNCH:
                if self.sock is not None:
                    try:
                        self.sock.sendto(_pack(KIND_PONG, self.device_id, peer, self.token), addr)
                    except Exception:
                        pass
                with self._lock:
                    self.paths[peer].state = "direct"
                continue
            if kind in (KIND_PONG, KIND_PING):
                with self._lock:
                    self.paths[peer].state = "direct"
                    self.paths[peer].via = "direct"
                    if kind == KIND_PING and self.sock is not None:
                        try:
                            self.sock.sendto(_pack(KIND_PONG, self.device_id, peer, self.token), addr)
                        except Exception:
                            pass
                continue
            if kind in (KIND_DATA, KIND_FIN):
                self._recv_file(head, blob)
                continue
            if kind == KIND_ACK:
                missing = head.get("missing")
                if missing:
                    sent = self._resend(peer, str(head.get("name") or ""), list(missing))
                    self.log.info("P2P 补发 %s 个分片（%s）", sent, head.get("name"))
                continue
            if kind == KIND_MSG and self.on_message is not None:
                try:
                    self.on_message(peer, blob)
                except Exception as e:
                    self.log.warning("P2P 消息处理异常：%s", e)

    def _recv_file(self, head: dict, blob: bytes) -> None:
        name = str(head.get("name") or "blob.bin").replace("\\", "/").split("/")[-1]
        if head.get("k") == KIND_DATA:
            # 按序号写指定位置，UDP 乱序到达也能拼对
            part = self.inbox / f"{name}.part"
            seq = int(head.get("seq") or 0)
            idx = int(head.get("frag") or 0)
            off = seq * CHUNK + idx * FRAG
            self.rx_frags.setdefault(name, set()).add(off // FRAG)
            try:
                if not part.exists():
                    part.touch()
                with open(part, "r+b") as f:
                    f.seek(off)
                    f.write(blob)
            except Exception as e:
                self.log.warning("P2P 落盘失败 %s：%s", name, e)
            return
        size = int(head.get("size") or 0)
        expect = (size + FRAG - 1) // FRAG
        got = self.rx_frags.get(name, set())
        missing = [i for i in range(expect) if i not in got]
        if missing and head.get("f"):
            if self.send(str(head.get("f")), b"", KIND_ACK, {"name": name, "missing": missing[:2000]}):
                self.log.info("P2P %s 缺 %s 片，已请求补发", name, len(missing))
                return
        final = self.inbox / name
        part = self.inbox / f"{name}.part"
        try:
            if part.exists():
                os.replace(part, final)
        except Exception as e:
            self.log.warning("P2P 收尾失败 %s：%s", name, e)
            return
        digest = ""
        try:
            digest = hashlib.sha256(final.read_bytes()).hexdigest()
        except Exception:
            pass
        good = bool(digest and digest == head.get("sha256"))
        if not good and final.exists():
            try:
                final.unlink()
            except Exception:
                pass
        self.rx_frags.pop(name, None)
        info = {"name": name, "path": str(final), "bytes": head.get("size"),
                "sha256_ok": good, "from": head.get("f"), "ts": int(time.time())}
        self.log.info("P2P 收到文件 %s（%s，校验%s）", name, head.get("size"), "通过" if good else "不通过")
        if self.on_event is not None:
            try:
                self.on_event({"event": "p2p.file_in", "data": info})
            except Exception:
                pass

    def _keep_loop(self) -> None:
        interval = max(5, int(self.cfg.p2p_keepalive_s))
        while not self._stop.is_set():
            time.sleep(interval)
            if self._stop.is_set() or self.sock is None:
                continue
            now = time.time()
            with self._lock:
                items = list(self.paths.items())
            for peer, p in items:
                if now - p.last_rx > max(30, interval * 3):
                    p.state = "down"
                    continue
                try:
                    self.sock.sendto(_pack(KIND_PING, self.device_id, peer, self.token), p.addr)
                except Exception:
                    p.state = "down"
            self.prune_files()
            self.prune_frags()

    def prune_frags(self) -> None:
        """半途而废的传输留的碎片表定期清掉，避免长期运行吃掉内存。"""
        keep = set(self.tx_cache.keys())
        for name in list(self.rx_frags.keys()):
            if name not in keep and not (self.inbox / f"{name}.part").exists():
                self.rx_frags.pop(name, None)

    def prune_files(self) -> None:
        """收取目录不无限长：超过上限就删最旧的已经收完的文件。"""
        limit_mb = int(self.cfg.p2p_inbox_max_mb)
        files = [f for f in self.inbox.glob("*") if f.is_file() and not f.name.endswith(".part")]
        total = sum(f.stat().st_size for f in files if f.exists())
        if total <= limit_mb * 1024 * 1024:
            return
        for f in sorted(files, key=lambda x: x.stat().st_mtime):
            if total <= limit_mb * 1024 * 1024:
                break
            try:
                size = f.stat().st_size
                f.unlink()
                total -= size
            except Exception:
                continue

    # ---------------------------------------------------------- 状态
    def status(self) -> dict:
        with self._lock:
            peers = {k: v.info() for k, v in self.paths.items()}
        direct = sum(1 for v in peers.values() if v["state"] == "direct")
        return {"enabled": bool(self.cfg.p2p_enabled), "port": int(self.cfg.p2p_port),
                "candidates": [f"{h}:{p}" for h, p in self.candidates()],
                "peers": peers, "direct": direct, "relayed": self.relayed,
                "rx_bytes": self.rx_bytes, "tx_bytes": self.tx_bytes, "dropped": self.dropped}
