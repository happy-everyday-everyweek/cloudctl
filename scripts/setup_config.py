#!/usr/bin/env python3
"""配置向导：填自己的凭证与目标仓库，生成 config.json / rules.json / mirrors.json。

用法：
    python scripts/setup_config.py                  # 交互式
    python scripts/setup_config.py --non-interactive \
        --device-name PC1 --gh-repo me/cloudctl --gh-token ghp_xxx \
        --upload-repo me/knowledge-base --upload-token ghp_xxx \
        --server-url https://ctl.example.com

做的事：写 agent/config.json（缺失字段用默认值）、生成带安全默认值的 rules.json，
可选把 GitLink 导出的镜像清单合并进 mirrors.json，最后打印构建与安装命令。
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "agent"
EXAMPLE = AGENT / "config.example.json"

EXCLUDE_KEYS = {"device_id", "home", "devsrv_secret", "mesh_secret"}


def load_example() -> dict:
    if EXAMPLE.exists():
        try:
            return json.loads(EXAMPLE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def gen_token(prefix: str = "cc") -> str:
    return f"{prefix}_{secrets.token_urlsafe(24)}"


def ask(prompt: str, default: str = "") -> str:
    tip = f"{prompt}" + (f" [{default}]" if default else "")
    try:
        val = input(tip + "：").strip()
    except EOFError:
        return default
    return val or default


def build(args: argparse.Namespace) -> dict:
    cfg = load_example()
    non_interactive = bool(args.non_interactive)

    def val(flag: str, prompt: str, default: str = "") -> str:
        got = getattr(args, flag, None)
        if got:
            return str(got)
        return default if non_interactive else ask(prompt, default)

    cfg["device_name"] = val("device_name", "设备名（留空用主机名）", cfg.get("device_name", ""))
    cfg["group"] = val("group", "分组", cfg.get("group", "default")) or "default"
    cfg["devsrv_token"] = val("devsrv_token", "本地控制台令牌（留空自动生成）", "") or gen_token("dev")
    cfg["server_url"] = val("server_url", "WebSocket 通道地址（无则留空）", cfg.get("server_url", ""))
    cfg["gh_rules_repo"] = val("gh_repo", "控制仓库 owner/repo", cfg.get("gh_rules_repo", ""))
    cfg["gh_token"] = val("gh_token", "控制仓库令牌（细粒度，Contents 读写）", "")
    cfg["upload_repo"] = val("upload_repo", "归档目标仓库 owner/repo", cfg.get("upload_repo", ""))
    cfg["upload_token"] = val("upload_token", "归档仓库令牌（留空则用控制仓库的）", "") or cfg.get("gh_token", "")
    cfg["upload_branch"] = val("upload_branch", "归档分支", cfg.get("upload_branch", "main")) or "main"
    cfg["upload_prefix"] = val("upload_prefix", "仓库内前缀目录", cfg.get("upload_prefix", "kb")) or "kb"
    cfg["mesh_token"] = val("mesh_token", "局域网互通令牌（留空自动生成）", "") or gen_token("mesh")
    cfg["gh_mirror_pool"] = val("mirror_pool", "自备镜像清单 JSON 路径（可留空）", cfg.get("gh_mirror_pool", ""))
    return cfg


def write_rules(home_hint: str, cfg: dict) -> Path:
    rules = {
        "version": 1,
        "identity": {"display_name": cfg.get("device_name") or "", "tags": []},
        "capture": {
            "photo": {"enabled": False},
            "video": {"enabled": False},
            "camera": {"enabled": False, "index": 0, "segment_s": 120, "max_mb_per_day": 2000},
            "audio": {"enabled": True, "threshold_db": -35.0, "attack_s": 0.3, "hold_s": 3.0},
        },
        "scan": {"roots": [], "interval_min": 30},
        "upload": {
            "enabled": False,
            "repo": cfg.get("upload_repo", ""),
            "branch": cfg.get("upload_branch", "main"),
            "path_prefix": cfg.get("upload_prefix", "kb"),
            "max_single_mb": 90,
            "chunk": {"enabled": True, "size_mb": 40, "keep_parts": True},
        },
        "index": {"enabled": True, "path": "kb/_index/index.json", "per_kind": True},
        "security": {"allow_shell": True, "allow_file_write": True, "allow_delete": False},
    }
    out = Path(home_hint) / "rules.json" if home_hint else AGENT / "rules.suggested.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser("cloudctl-setup")
    ap.add_argument("--non-interactive", action="store_true")
    ap.add_argument("--device-name", default="")
    ap.add_argument("--group", default="")
    ap.add_argument("--devsrv-token", default="")
    ap.add_argument("--server-url", default="")
    ap.add_argument("--gh-repo", default="")
    ap.add_argument("--gh-token", default="")
    ap.add_argument("--upload-repo", default="")
    ap.add_argument("--upload-token", default="")
    ap.add_argument("--upload-branch", default="")
    ap.add_argument("--upload-prefix", default="")
    ap.add_argument("--mesh-token", default="")
    ap.add_argument("--mirror-pool", default="")
    ap.add_argument("--home", default="", help="工作目录，默认取环境变量或配置默认值")
    args = ap.parse_args(argv)

    cfg = build(args)
    cfg_path = AGENT / "config.json"
    if cfg_path.exists():
        try:
            old = json.loads(cfg_path.read_text(encoding="utf-8"))
            old.update({k: v for k, v in cfg.items() if v not in ("", None)})
            cfg = old
        except Exception:
            pass
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    home = args.home or ""
    rules_path = write_rules(home, cfg)

    print("\n已写入：")
    print(f"  配置：{cfg_path}")
    print(f"  规则：{rules_path}")
    print("\n接下来：")
    print("  1) 验证依赖：python -m cloudctl_agent.main --selftest")
    print("  2) 测声音电平：python -m cloudctl_agent.main --audio-level 5（把 peak_db 往下调 10 就是合适的 threshold_db）")
    print("  3) 看摄像头：python -m cloudctl_agent.main --cameras")
    print("  4) 本地跑起来：python -m cloudctl_agent.main --console，浏览器开 http://127.0.0.1:8788")
    print("  5) 构建发布包：bash scripts/build_linux.sh  或  powershell -File scripts/build_windows.ps1")
    print("\n注意：请勿把 config.json 提交到公开仓库，它里面有你的令牌。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
