"""配置加载：config.json + 内嵌构建配置 + 环境变量覆盖。

优先级（从高到低）：环境变量 > exe 旁的 config.json > 内嵌 build_config.json > 默认值。

配置文件位置：
  - 源码方式运行：agent/config.json
  - 单文件 exe：exe 旁边的 config.json（打包后 __file__ 指向临时解包目录，不能用它推路径）
  - 内嵌默认：cloudctl_agent/build_config.json，构建时用 --add-data 打进去，
    适合做“装上去就能连”的发行包；本机再放一个 config.json 就能盖掉内嵌值。
  - 三种都可以用环境变量 CLOUDCTL_CONFIG 指到任意路径。

工作目录：默认自动选盘（在 D/E/F … 里挑剩余空间最大的那块，盘上都小于 min_free_mb 时
才退回系统默认目录）；想把数据固定到某处就手动填 home。

三类入口：设备本地控制台（devsrv）、两条平行外联通道（WS / GitHub）、局域网互联（mesh）。
上报：存活上报与关机上报（report）；存储配额（storage）。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from .storage import pick_data_drive


def default_home() -> Path:
    env = os.environ.get("CLOUDCTL_HOME")
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("ProgramData") or r"C:\ProgramData"
        return Path(base) / "cloudctl"
    return Path.home() / ".cloudctl"


@dataclass
class Config:
    # 设备标识
    device_id: str = ""
    device_name: str = ""
    group: str = "default"

    # 设备本地服务
    devsrv_enabled: bool = True
    devsrv_bind: str = "127.0.0.1"
    devsrv_port: int = 8788
    devsrv_token: str = ""
    devsrv_public_url: str = ""

    # 局域网互联
    mesh_enabled: bool = True
    mesh_group: str = ""
    mesh_bind: str = "0.0.0.0"
    mesh_port: int = 8792
    mesh_discovery_port: int = 8791
    mesh_mcast: str = "239.255.42.99"
    mesh_token: str = ""
    mesh_announce_s: int = 20
    mesh_ttl_s: int = 90
    mesh_accept_cmd: bool = True
    mesh_relay: bool = True
    mesh_max_hops: int = 2

    # 设备间直连（P2P，去中心化，不过服务器也能互相传）
    p2p_enabled: bool = True
    p2p_bind: str = "0.0.0.0"
    p2p_port: int = 8793
    p2p_token: str = ""
    p2p_public_host: str = ""
    p2p_public_port: int = 0
    p2p_keepalive_s: int = 20
    p2p_inbox_max_mb: int = 4096

    # 通道一：WebSocket
    server_url: str = ""
    server_token: str = ""
    reconnect_min_s: int = 3
    reconnect_max_s: int = 60
    heartbeat_s: int = 25

    # 通道二：GitHub 文件轮询
    gh_rules_repo: str = ""
    gh_rules_path: str = "ctl/rules.json"
    gh_cmd_dir: str = "ctl/cmd"
    gh_outbox_dir: str = "ctl/outbox"
    gh_token: str = ""
    gh_poll_s: int = 30
    gh_api_base: str = "https://api.github.com"
    gh_raw_base: str = "https://raw.githubusercontent.com"
    gh_proxy: str = ""
    gh_backoff_max_s: int = 900

    # 镜像池
    gh_mirror_pool: str = ""
    gh_mirror_top: int = 4
    gh_mirror_probe_s: int = 1800
    gh_mirror_probe: bool = True

    # 上报：存活心跳与关机上报
    report_enabled: bool = True
    report_interval_s: int = 300          # 存活上报间隔
    report_on_start: bool = True
    report_on_shutdown: bool = True
    report_shutdown_wait_s: int = 8        # 关机上报最多等多久（同步发送）
    report_include_metrics: bool = True    # 带 CPU / 内存占用

    # 存储与配额
    home_auto: bool = True                 # 自动选盘（有 D / E 盘时按剩余空间挑）
    work_drives: str = "CDEFGH"            # 候选盘符
    buffer_max_mb: int = 10240             # 待上传暂存上限，默认 10G
    min_free_mb: int = 2048                # 盘上至少留这么多余量，低于它停止写盘（选盘不卡这个值）
    drop_oldest_when_full: bool = True     # 超限且传不出去时，允许丢最旧的
    picked_drive: str = ""                 # 上次自动选中的盘（只读展示用）

    # 素材归档
    upload_repo: str = ""
    upload_branch: str = "main"
    upload_prefix: str = "kb"
    upload_token: str = ""
    upload_mode: str = "auto"
    upload_max_single_mb: int = 90
    upload_concurrency: int = 2

    # 本地状态
    home: str = ""
    log_level: str = "INFO"
    log_max_mb: int = 5
    log_keep: int = 3

    # 运行开关（服务端规则只能收紧）
    allow_shell: bool = True
    allow_file_write: bool = True
    allow_delete: bool = False

    def __post_init__(self) -> None:
        if not self.home:
            self.home = self._auto_home()
        if not self.device_id:
            self.device_id = self._derive_device_id()
        if not self.device_name:
            self.device_name = socket.gethostname()

    def _auto_home(self) -> str:
        env = os.environ.get("CLOUDCTL_HOME")
        if env:
            return env
        if self.home_auto:
            picked = pick_data_drive(self.work_drives, self.min_free_mb)
            if picked:
                self.picked_drive = picked["home"]
                return picked["home"]
        return str(default_home())

    @staticmethod
    def _derive_device_id() -> str:
        host = socket.gethostname().lower()
        node = uuid.getnode()
        seed = f"{host}-{node:012x}"
        return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex[:16]

    # --- 路径 ---
    @property
    def home_path(self) -> Path:
        p = Path(self.home)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def state_db(self) -> Path:
        return self.home_path / "state.db"

    @property
    def rules_file(self) -> Path:
        return self.home_path / "rules.json"

    @property
    def rules_cache(self) -> Path:
        return self.home_path / "rules.remote.json"

    @property
    def mirrors_file(self) -> Path:
        return self.home_path / "mirrors.json"

    @property
    def capture_dir(self) -> Path:
        p = self.home_path / "captures"
        p.mkdir(parents=True, exist_ok=True)
        return p

    # --- 令牌与安全 ---
    @property
    def devsrv_secret(self) -> str:
        return self.devsrv_token or self.server_token or self.device_id

    @property
    def devsrv_has_explicit_token(self) -> bool:
        """是否配了真正的令牌（没配时只能绑回环地址）。"""
        return bool(self.devsrv_token or self.server_token)

    @property
    def mesh_secret(self) -> str:
        return self.mesh_token or self.devsrv_secret

    @property
    def mesh_has_explicit_token(self) -> bool:
        return bool(self.mesh_token or self.devrsv_token if False else (self.mesh_token or self.devsrv_token or self.server_token))

    @property
    def p2p_secret(self) -> str:
        """直连层默认复用网格令牌；两者都不配就只能回退到 device_id。"""
        return self.p2p_token or self.mesh_secret

    @property
    def p2p_has_explicit_token(self) -> bool:
        return bool(self.p2p_token or self.mesh_has_explicit_token)

    @property
    def mesh_scope(self) -> str:
        return self.mesh_group or self.group or "default"

    @property
    def is_loopback(self) -> bool:
        return (self.devsrv_bind or "127.0.0.1") in ("127.0.0.1", "localhost", "::1")

    @property
    def devconsole_url(self) -> str:
        base = (self.devsrv_public_url or "").strip().rstrip("/")
        if base:
            return base
        host = self.devsrv_bind or "127.0.0.1"
        if host in ("0.0.0.0", "::", "*"):
            host = "127.0.0.1"
        return f"http://{host}:{int(self.devsrv_port)}"

    # --- 读写 ---
    @classmethod
    def path(cls) -> Path:
        """外部配置（可读写）路径。"""
        env = os.environ.get("CLOUDCTL_CONFIG")
        if env:
            return Path(env)
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent / "config.json"
        return Path(__file__).resolve().parent.parent / "config.json"

    @classmethod
    def bundled_path(cls) -> Path:
        """构建时内嵌进包的配置（PyInstaller --add-data cloudctl_agent/build_config.json）。"""
        return Path(__file__).resolve().parent / "build_config.json"

    @staticmethod
    def _read_dict(p: Path) -> dict[str, Any]:
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @classmethod
    def load(cls) -> "Config":
        raw = cls._read_dict(cls.bundled_path())          # 内嵌默认
        raw.update(cls._read_dict(cls.path()))            # 本机 config.json 优先
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in raw.items() if k in known}
        for f in fields(cls):
            env_key = "CLOUDCTL_" + f.name.upper()
            if env_key in os.environ:
                val: Any = os.environ[env_key]
                if isinstance(f.default, bool):
                    val = str(val).lower() in ("1", "true", "yes")
                elif isinstance(f.default, int):
                    try:
                        val = int(val)
                    except ValueError:
                        continue
                data[f.name] = val
        cfg = cls(**data)
        cfg.save()
        return cfg

    def save(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("server_token", "gh_token", "upload_token", "devsrv_token", "mesh_token"):
            if d.get(k):
                d[k] = "***"
        d["devconsole"] = self.devconsole_url
        d["mesh_scope"] = self.mesh_scope
        return d
