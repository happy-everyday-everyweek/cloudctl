"""配置加载：config.json + 环境变量覆盖。

配置优先级：环境变量 > config.json > 默认值。
默认工作目录：Windows 为 %ProgramData%\\cloudctl，其他平台为 ~/.cloudctl。
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


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

    # 主通道：内网穿透暴露的 WebSocket 地址
    server_url: str = ""
    server_token: str = ""
    reconnect_min_s: int = 3
    reconnect_max_s: int = 60
    heartbeat_s: int = 25

    # 备用通道：GitHub 仓库文件轮询
    gh_rules_repo: str = ""            # owner/repo
    gh_rules_path: str = "ctl/rules.json"
    gh_cmd_dir: str = "ctl/cmd"        # 每个设备一个文件 <device_id>.json
    gh_outbox_dir: str = "ctl/outbox"
    gh_token: str = ""
    gh_poll_s: int = 30

    # 素材归档
    upload_repo: str = ""
    upload_branch: str = "main"
    upload_prefix: str = "kb"
    upload_token: str = ""
    upload_mode: str = "auto"          # auto | contents | git
    upload_max_single_mb: int = 90
    upload_concurrency: int = 2

    # 本地状态
    home: str = ""
    log_level: str = "INFO"
    log_max_mb: int = 5
    log_keep: int = 3

    # 运行开关（服务端规则只能收紧，不能放开）
    allow_shell: bool = True
    allow_file_write: bool = True
    allow_delete: bool = False

    def __post_init__(self) -> None:
        if not self.home:
            self.home = str(default_home())
        if not self.device_id:
            self.device_id = self._derive_device_id()
        if not self.device_name:
            self.device_name = socket.gethostname()

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
    def capture_dir(self) -> Path:
        p = self.home_path / "captures"
        p.mkdir(parents=True, exist_ok=True)
        return p

    # --- 读写 ---
    @classmethod
    def path(cls) -> Path:
        env = os.environ.get("CLOUDCTL_CONFIG")
        if env:
            return Path(env)
        return Path(__file__).resolve().parent.parent / "config.json"

    @classmethod
    def load(cls) -> "Config":
        raw: dict[str, Any] = {}
        p = cls.path()
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
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
        """去掉凭据的副本，用于上报。"""
        d = asdict(self)
        for k in ("server_token", "gh_token", "upload_token"):
            if d.get(k):
                d[k] = "***"
        return d
