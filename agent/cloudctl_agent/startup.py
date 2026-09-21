"""开机自启动：Windows 计划任务（SYSTEM 上下文），无管理员权限时回退到 HKCU Run。

计划任务是 RMM 客户端的常规做法：不依赖用户登录，重启后自动拉起。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .util import IS_WINDOWS

TASK_NAME = "cloudctl-agent"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "cloudctl-agent"


def current_exe() -> str:
    """打包后返回 exe 路径，源码运行时返回 python + 脚本的启动串。"""
    if getattr(sys, "frozen", False):
        return sys.executable
    entry = Path(__file__).resolve().parent.parent / "run_agent.py"
    return f'"{sys.executable}" "{entry}"'


def is_admin() -> bool:
    if not IS_WINDOWS:
        return os.geteuid() == 0  # type: ignore[attr-defined]
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def install(home: Path, log) -> dict:
    if not IS_WINDOWS:
        return _install_systemd(home, log)
    exe = current_exe()
    result: dict = {"exe": exe, "method": None}
    if is_admin():
        cmd = [
            "schtasks", "/Create", "/TN", TASK_NAME,
            "/TR", f'"{exe}" --run' if getattr(sys, "frozen", False) else exe,
            "/SC", "ONSTART", "/RU", "SYSTEM", "/RL", "HIGHEST", "/F",
        ]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode == 0:
            result["method"] = "schtasks-onstart-system"
            log.info("已创建系统级开机任务 %s", TASK_NAME)
            return result
        log.warning("schtasks 创建失败：%s", (proc.stderr or b"").decode("utf-8", "replace"))
    # 回退：当前用户登录时启动
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, f'"{exe}" --run')
        result["method"] = "hkcu-run"
        log.info("已写入 HKCU Run 自启项")
    except Exception as e:
        result["error"] = str(e)
        log.error("自启安装失败：%s", e)
    return result


def _install_systemd(home: Path, log) -> dict:
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "cloudctl-agent.service"
    unit.write_text(
        "[Unit]\n"
        "Description=cloudctl agent\nAfter=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={current_exe()} --run\n"
        "Restart=always\nRestartSec=10\n\n"
        "[Install]\nWantedBy=default.target\n",
        encoding="utf-8",
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "cloudctl-agent.service"], capture_output=True)
    log.info("已安装 systemd user service")
    return {"method": "systemd-user", "unit": str(unit)}


def remove(log) -> dict:
    if not IS_WINDOWS:
        subprocess.run(["systemctl", "--user", "disable", "--now", "cloudctl-agent.service"], capture_output=True)
        return {"removed": True, "method": "systemd-user"}
    subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True)
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_VALUE)
    except Exception:
        pass
    log.info("已移除自启项")
    return {"removed": True, "method": "schtasks+hkcu"}


def status() -> dict:
    if not IS_WINDOWS:
        return {"installed": False, "platform": "posix"}
    proc = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True)
    installed = proc.returncode == 0
    val = ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            val = winreg.QueryValueEx(k, RUN_VALUE)[0]
    except Exception:
        pass
    return {"installed": installed or bool(val), "task": installed, "hkcu_run": val, "admin": is_admin()}
