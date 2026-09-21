"""远程操作实现层：系统信息、终端会话、文件管理、进程管理。

所有函数返回 dict，由 channel 层包成 result 消息回传。失败时抛异常，
由调用方统一捕获成 ok=false。
"""
from __future__ import annotations

import base64
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .util import human_size, sha256_file

IS_WINDOWS = os.name == "nt"
CHUNK = 512 * 1024


# ------------------------------------------------------------------ 系统信息

def sys_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "user": os.environ.get("USERNAME") or os.environ.get("USER") or "",
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "boot_ts": int(time.time() - time.monotonic()),
        "drives": [],
        "cpu": {},
        "memory": {},
        "nics": [],
    }
    try:
        import psutil  # type: ignore

        info["cpu"] = {
            "logical": psutil.cpu_count(logical=True),
            "physical": psutil.cpu_count(logical=False),
            "percent": psutil.cpu_percent(interval=0.2),
        }
        vm = psutil.virtual_memory()
        info["memory"] = {"total": vm.total, "used": vm.used, "percent": vm.percent}
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except Exception:
                continue
            info["drives"].append(
                {
                    "mount": part.mountpoint,
                    "fs": part.fstype,
                    "total": usage.total,
                    "used": usage.used,
                    "percent": usage.percent,
                    "total_h": human_size(usage.total),
                    "free_h": human_size(usage.free),
                }
            )
        info["boot_ts"] = int(psutil.boot_time())
    except Exception:
        pass
    return info


# ------------------------------------------------------------------ 一次性命令

def shell_exec(cmd: str, timeout_s: int = 60, cwd: str | None = None) -> dict[str, Any]:
    started = time.time()
    shell_exe = None
    if IS_WINDOWS:
        shell_exe = os.environ.get("COMSPEC", "cmd.exe")
    proc = subprocess.run(
        cmd,
        shell=True,
        executable=shell_exe,
        cwd=cwd or None,
        capture_output=True,
        timeout=max(1, int(timeout_s)),
    )
    def dec(b: bytes) -> str:
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                return b.decode(enc)
            except Exception:
                continue
        return b.decode("utf-8", "replace")

    return {
        "code": proc.returncode,
        "stdout": dec(proc.stdout or b""),
        "stderr": dec(proc.stderr or b""),
        "elapsed": round(time.time() - started, 3),
    }


# ------------------------------------------------------------------ 交互式会话
class ShellSession:
    """长驻可交互 shell。Windows 用 powershell -Command -，其他平台用 /bin/sh。

    输出用后台线程持续读入环形缓冲区，read() 只取增量。
    """

    def __init__(self, name: str = "default") -> None:
        self.name = name
        self._buf = ""
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._alive = False
        self.started_ts = 0.0

    def start(self) -> dict[str, Any]:
        if self._alive:
            return {"session": self.name, "already": True, "pid": self._proc.pid if self._proc else 0}
        if IS_WINDOWS:
            argv = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "-"]
        else:
            argv = ["/bin/sh", "-i"]
        creation = 0
        if IS_WINDOWS:
            creation = subprocess.CREATE_NO_WINDOW  # 不弹控制台窗口
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            creationflags=creation,
        )
        self._alive = True
        self.started_ts = time.time()
        threading.Thread(target=self._reader, name=f"shell-{self.name}", daemon=True).start()
        return {"session": self.name, "pid": self._proc.pid}

    def _reader(self) -> None:
        assert self._proc and self._proc.stdout
        while True:
            try:
                b = self._proc.stdout.read(4096)
            except Exception:
                break
            if not b:
                break
            text = b.decode("utf-8", "replace")
            with self._lock:
                self._buf += text
                if len(self._buf) > 4 * 1024 * 1024:
                    self._buf = self._buf[-1024 * 1024 :]
        self._alive = False

    def write(self, data: str) -> dict[str, Any]:
        if not self._alive:
            self.start()
        assert self._proc and self._proc.stdin
        line = data if data.endswith("\n") else data + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        self._proc.stdin.flush()
        time.sleep(0.35)
        return {"session": self.name, "output": self.read()["output"]}

    def read(self) -> dict[str, Any]:
        with self._lock:
            out, self._buf = self._buf, ""
        return {"session": self.name, "output": out, "alive": self._alive}

    def close(self) -> dict[str, Any]:
        if self._proc and self._alive:
            try:
                if IS_WINDOWS:
                    subprocess.run(
                        ["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                        capture_output=True,
                    )
                else:
                    self._proc.terminate()
            except Exception:
                pass
        self._alive = False
        self._proc = None
        return {"session": self.name, "closed": True}

    @property
    def alive(self) -> bool:
        return self._alive


# ------------------------------------------------------------------ 文件管理

def _p(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def file_list(path: str) -> dict[str, Any]:
    p = _p(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    entries = []
    if p.is_file():
        st = p.stat()
        return {
            "path": str(p),
            "is_file": True,
            "size": st.st_size,
            "size_h": human_size(st.st_size),
            "mtime": int(st.st_mtime),
        }
    for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        try:
            st = child.stat()
            is_dir = child.is_dir()
            entries.append(
                {
                    "name": child.name,
                    "path": str(child),
                    "is_dir": is_dir,
                    "size": None if is_dir else st.st_size,
                    "size_h": "" if is_dir else human_size(st.st_size),
                    "mtime": int(st.st_mtime),
                }
            )
        except Exception as e:
            entries.append({"name": child.name, "path": str(child), "error": str(e)})
    return {"path": str(p), "is_file": False, "entries": entries, "count": len(entries)}


def file_pull(path: str, offset: int = 0, length: int = CHUNK) -> dict[str, Any]:
    p = _p(path)
    size = p.stat().st_size
    with open(p, "rb") as f:
        f.seek(max(0, int(offset)))
        data = f.read(int(length))
    return {
        "path": str(p),
        "size": size,
        "offset": max(0, int(offset)),
        "length": len(data),
        "eof": (max(0, int(offset)) + len(data)) >= size,
        "sha256": None,
        "data_b64": base64.b64encode(data).decode("ascii"),
    }


def file_push(path: str, data_b64: str, append: bool = False, offset: int = 0) -> dict[str, Any]:
    p = _p(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    blob = base64.b64decode(data_b64)
    mode = "ab" if append else "wb"
    if not append and offset:
        mode = "r+b" if p.exists() else "wb"
        with open(p, mode) as f:
            f.seek(int(offset))
            f.write(blob)
    else:
        with open(p, mode) as f:
            f.write(blob)
    return {"path": str(p), "written": len(blob), "size": p.stat().st_size}


def file_mkdir(path: str) -> dict[str, Any]:
    p = _p(path)
    p.mkdir(parents=True, exist_ok=True)
    return {"path": str(p), "created": True}


def file_rename(path: str, new_path: str) -> dict[str, Any]:
    src, dst = _p(path), _p(new_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst) if dst.exists() else shutil.move(str(src), str(dst))
    return {"path": str(src), "new_path": str(dst)}


def file_delete(path: str) -> dict[str, Any]:
    p = _p(path)
    if p.is_dir():
        shutil.rmtree(p)
    elif p.exists():
        p.unlink()
    return {"path": str(p), "deleted": True}


def file_hash(path: str) -> dict[str, Any]:
    p = _p(path)
    return {"path": str(p), "sha256": sha256_file(p), "size": p.stat().st_size}


# ------------------------------------------------------------------ 进程

def proc_list(limit: int = 200, keyword: str = "") -> dict[str, Any]:
    out = []
    try:
        import psutil  # type: ignore

        for pr in psutil.process_iter(["pid", "name", "username", "memory_info", "cpu_percent", "create_time"]):
            try:
                d = pr.info
                if keyword and keyword.lower() not in (d.get("name") or "").lower():
                    continue
                out.append(
                    {
                        "pid": d["pid"],
                        "name": d.get("name"),
                        "user": d.get("username"),
                        "rss": getattr(d.get("memory_info"), "rss", 0),
                        "cpu": d.get("cpu_percent"),
                        "started": int(d.get("create_time") or 0),
                    }
                )
            except Exception:
                continue
    except Exception as e:
        raise RuntimeError(f"psutil 不可用: {e}") from e
    out.sort(key=lambda x: x.get("rss") or 0, reverse=True)
    return {"count": len(out), "processes": out[: max(1, int(limit))]}


def proc_kill(pid: int, force: bool = True) -> dict[str, Any]:
    try:
        import psutil  # type: ignore

        pr = psutil.Process(int(pid))
        pr.kill() if force else pr.terminate()
        return {"pid": int(pid), "killed": True}
    except Exception as e:
        raise RuntimeError(str(e)) from e
