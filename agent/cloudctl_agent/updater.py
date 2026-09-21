"""OTA 自升级：从 GitHub Release 拉新版本，校验后就地替换自身。

流程：读规则里的 update 段 -> 用镜像池访问 Releases API -> 比版本号 ->
下载匹配的资产（可按 sha256 校验）-> 生成一个等待进程退出再替换的小脚本 ->
脚本替换 exe 并重新拉起 -> 退出当前进程。

只依赖标准库与 requests，配合 mirrors.py 的镜像池，国内网络也能取到新版本。
冻结成 exe 时支持自我替换；以源码方式运行时只下载到本地并提示手工重启。
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .mirrors import MirrorPool

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "repo": "happy-everyday-everyweek/cloudctl",
    "channel": "latest",
    "tag": "",
    "prerelease": False,
    "asset_pattern": "cloudctl-agent",
    "check_min": 60,
    "auto_apply": False,
    "verify_sha": True,
    "keep_backup": True,
    "restart_delay_s": 5,
    "token": "",
}


def parse_version(text: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", str(text or ""))
    return tuple(int(x) for x in nums[:4]) or (0,)


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return a > b


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Updater:
    def __init__(self, cfg, rules, log, pool: MirrorPool | None = None,
                 current_version: str = "0.0.0", exe_path: Path | None = None) -> None:
        self.cfg = cfg
        self.rules = rules
        self.log = log
        self.pool = pool or MirrorPool(cfg.home_path, log, pool_json=cfg.gh_mirror_pool,
                                      top=cfg.gh_mirror_top)
        self.current = current_version
        self.exe_path = Path(exe_path) if exe_path else Path(sys.executable)
        self.last_check: dict[str, Any] = {}
        self.last_error = ""

    # ------------------------------------------------------------ 配置
    def conf(self) -> dict[str, Any]:
        raw = (self.rules.data or {}).get("update") or {}
        out = dict(DEFAULTS)
        out.update({k: v for k, v in raw.items() if k in DEFAULTS})
        return out

    @property
    def frozen(self) -> bool:
        return bool(getattr(sys, "frozen", False))

    def status(self) -> dict[str, Any]:
        c = self.conf()
        return {"current": self.current, "enabled": bool(c.get("enabled")),
                "repo": c.get("repo"), "channel": c.get("channel"),
                "auto_apply": bool(c.get("auto_apply")), "frozen": self.frozen,
                "exe": str(self.exe_path), "last": self.last_check, "error": self.last_error}

    # ------------------------------------------------------------ 请求
    def _api_candidates(self, url: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for mid, u in self.pool.candidates(url):
            if "github.com/https://" in u or "github.com/http://" in u:
                continue
            out.append((mid, u))
        return out[: max(1, int(self.cfg.gh_mirror_top))] or [("direct", url)]

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        h = {"Accept": accept, "User-Agent": "cloudctl-agent"}
        token = self.conf().get("token") or getattr(self.cfg, "gh_token", "")
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def _get_json(self, url: str) -> dict | None:
        import requests  # type: ignore

        for mid, u in self._api_candidates(url):
            try:
                r = requests.get(u, headers=self._headers(), timeout=20)
                if r.status_code == 200:
                    self.pool.report(mid, True)
                    return r.json()
                self.pool.report(mid, False)
                self.last_error = f"HTTP {r.status_code}"
            except Exception as e:
                self.pool.report(mid, False)
                self.last_error = str(e)
        return None

    # ------------------------------------------------------------ 检查
    def check(self) -> dict[str, Any]:
        c = self.conf()
        repo = str(c.get("repo") or "").strip("/")
        if not repo:
            return {"ok": False, "err": "未配置 update.repo"}
        tag = str(c.get("tag") or "")
        if c.get("channel") == "tag" and tag:
            url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
        else:
            url = f"https://api.github.com/repos/{repo}/releases/latest"
        data = self._get_json(url)
        if not data:
            self.last_check = {"ok": False, "err": self.last_error or "拿不到 Release 信息"}
            return self.last_check
        latest = str(data.get("tag_name") or data.get("name") or "")
        assets = data.get("assets") or []
        pattern = str(c.get("asset_pattern") or "cloudctl-agent")
        asset = None
        for a in assets:
            name = str(a.get("name") or "")
            if pattern.lower() in name.lower():
                if platform.system() == "Windows" and not name.lower().endswith(".exe"):
                    continue
                asset = a
                break
        res = {"ok": True, "current": self.current, "latest": latest,
               "newer": is_newer(latest, self.current),
               "prerelease": bool(data.get("prerelease")),
               "published_at": data.get("published_at"),
               "notes": (data.get("body") or "")[:2000],
               "asset": ({"name": asset.get("name"), "size": asset.get("size"),
                          "sha256": (asset.get("digest") or "").replace("sha256:", ""),
                          "url": asset.get("browser_download_url")} if asset else None),
               "checked_at": int(time.time())}
        if c.get("verify_sha") and asset and not res["asset"]["sha256"]:
            res["asset"]["sha256_source"] = "release 未提供摘要，改用 release notes 中的 sha256 行（若有）"
            found = re.search(r"sha256[:= ]+([0-9a-fA-F]{64})", data.get("body") or "")
            res["asset"]["sha256"] = found.group(1).lower() if found else ""
        self.last_check = res
        self.last_error = "" if res.get("ok") else res.get("err", "")
        self.log.info("OTA 检查：当前 %s，最新 %s，%s", self.current, latest,
                      "有新版本" if res["newer"] else "已是最新")
        return res

    # ------------------------------------------------------------ 下载
    def _download_candidates(self, url: str) -> list[str]:
        out: list[str] = []
        proxy = (getattr(self.cfg, "gh_proxy", "") or "").strip().rstrip("/")
        if proxy:
            out.append(f"{proxy}/{url}")
        out.append(url)
        for _mid, u in self.pool.candidates(url):
            if u not in out and "github.com/https://" not in u:
                out.append(u)
        return out

    def download(self, asset: dict[str, Any], out_dir: Path | None = None) -> dict[str, Any]:
        import requests  # type: ignore

        url = asset.get("url") or ""
        if not url:
            return {"ok": False, "err": "没有可下载的资产"}
        dest_dir = Path(out_dir) if out_dir else (self.cfg.home_path / "updates")
        dest_dir.mkdir(parents=True, exist_ok=True)
        name = Path(str(asset.get("name") or "cloudctl-agent.exe")).name
        dest = dest_dir / name
        expect = str(asset.get("sha256") or "")
        last = ""
        for u in self._download_candidates(url):
            try:
                with requests.get(u, headers=self._headers("application/octet-stream"),
                                   stream=True, timeout=120) as r:
                    if r.status_code != 200:
                        last = f"HTTP {r.status_code}"
                        continue
                    h = hashlib.sha256()
                    with open(dest, "wb") as f:
                        for chunk in r.iter_content(1024 * 512):
                            if chunk:
                                f.write(chunk)
                                h.update(chunk)
                    digest = h.hexdigest()
            except Exception as e:
                last = str(e)
                continue
            if self.conf().get("verify_sha") and expect and digest != expect.lower():
                last = f"sha256 不匹配（期望 {expect[:12]}，实际 {digest[:12]}）"
                try:
                    dest.unlink(missing_ok=True)
                except Exception:
                    pass
                continue
            return {"ok": True, "path": str(dest), "bytes": dest.stat().st_size,
                    "sha256": digest, "url": u}
        return {"ok": False, "err": last or "下载失败"}

    # ------------------------------------------------------------ 应用
    def apply(self, new_exe: Path, restart: bool = True, delay_s: int | None = None) -> dict[str, Any]:
        """生成替换脚本并启动它；调用方随后应主动退出进程。"""
        new_exe = Path(new_exe)
        if not new_exe.exists():
            return {"ok": False, "err": "新版本文件不存在"}
        if not self.frozen:
            return {"ok": False, "err": "当前以源码方式运行，无法自我替换；新版本已下载到 " + str(new_exe),
                    "path": str(new_exe)}
        conf = self.conf()
        delay = int(delay_s if delay_s is not None else conf.get("restart_delay_s") or 5)
        target = self.exe_path
        backup = target.with_suffix(target.suffix + ".bak")
        script_dir = self.cfg.home_path / "updates"
        script_dir.mkdir(parents=True, exist_ok=True)
        log_file = script_dir / "ota.log"
        if platform.system() == "Windows":
            script = script_dir / "apply_update.cmd"
            body = [
                "@echo off",
                f"echo [%date% %time%] OTA start >> \"{log_file}\"",
                f"timeout /t {delay} /nobreak >nul",
                ":wait",
                f'tasklist /fi "IMAGENAME eq {target.name}" | find /i "{target.name}" >nul',
                "if not errorlevel 1 (ping -n 2 127.0.0.1 >nul & goto wait)",
                f"if exist \"{backup}\" del /f /q \"{backup}\"",
            ]
            if conf.get("keep_backup"):
                body.append(f'move /y "{target}" "{backup}" >> \"{log_file}\" 2>&1')
            body.append(f'move /y "{new_exe}" "{target}" >> \"{log_file}\" 2>&1')
            if restart:
                body.append(f'start "" "{target}" --run')
            body.append(f"echo [%date% %time%] OTA done >> \"{log_file}\"")
            body.append("del /f /q \"%~f0\"")
            script.write_text("\r\n".join(body) + "\r\n", encoding="utf-8")
            flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(["cmd", "/c", str(script)], creationflags=flags, close_fds=True)
        else:
            script = script_dir / "apply_update.sh"
            pid = os.getpid()
            body = ["#!/bin/sh", f"echo 'OTA start' >> '{log_file}'", f"sleep {delay}",
                    f"while kill -0 {pid} 2>/dev/null; do sleep 1; done"]
            if conf.get("keep_backup"):
                body.append(f"cp -f '{target}' '{backup}'")
            body.append(f"cp -f '{new_exe}' '{target}'")
            body.append(f"chmod +x '{target}'")
            if restart:
                body.append(f"nohup '{target}' --run >/dev/null 2>&1 &")
            body.append(f"echo 'OTA done' >> '{log_file}'")
            body.append("rm -f -- \"$0\"")
            script.write_text("\n".join(body) + "\n", encoding="utf-8")
            script.chmod(0o755)
            subprocess.Popen(["/bin/sh", str(script)], close_fds=True, start_new_session=True)
        self.log.info("OTA 替换脚本已启动：%s，%s 秒后替换并重启", script, delay)
        return {"ok": True, "script": str(script), "target": str(target),
                "backup": str(backup) if conf.get("keep_backup") else "",
                "restart": bool(restart), "delay_s": delay}

    def run_once(self, apply_if_newer: bool | None = None) -> dict[str, Any]:
        conf = self.conf()
        if not conf.get("enabled") and apply_if_newer is None:
            return {"ok": False, "err": "OTA 未启用（rules.update.enabled=false）"}
        res = self.check()
        if not res.get("ok") or not res.get("newer"):
            return res
        asset = res.get("asset") or {}
        if not asset:
            res["err"] = "找到新版本但没有匹配的资产"
            return res
        dl = self.download(asset)
        res["download"] = dl
        if not dl.get("ok"):
            return res
        do_apply = bool(conf.get("auto_apply")) if apply_if_newer is None else bool(apply_if_newer)
        if do_apply:
            res["apply"] = self.apply(Path(dl["path"]))
        return res


class UpdateService:
    """后台线程：按 update.check_min 周期检查，可选自动升级。"""

    def __init__(self, updater: Updater, log) -> None:
        self.updater = updater
        self.log = log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.checks = 0
        self.last: dict[str, Any] = {}

    def start(self) -> dict:
        conf = self.updater.conf()
        if not conf.get("enabled"):
            return {"enabled": False}
        if self._thread is not None:
            return {"enabled": True, "already": True}
        self._thread = threading.Thread(target=self._loop, name="ota", daemon=True)
        self._thread.start()
        self.log.info("OTA 已启用，每 %s 分钟检查一次，自动升级=%s", conf.get("check_min"), conf.get("auto_apply"))
        return {"enabled": True}

    def stop(self) -> dict:
        self._stop.set()
        return {"stopped": True}

    def _loop(self) -> None:
        interval = max(5, int(self.updater.conf().get("check_min") or 60)) * 60
        time.sleep(30)
        while not self._stop.is_set():
            try:
                self.last = self.updater.run_once()
                self.checks += 1
                applied = (self.last or {}).get("apply") or {}
                if applied.get("ok"):
                    self.log.warning("新版本已就位，进程稍后退出以完成替换")
                    os._exit(0)
            except Exception as e:
                self.log.debug("OTA 检查异常：%s", e)
            self._stop.wait(interval)

    def status(self) -> dict:
        return {"checks": self.checks, "last": self.last, "updater": self.updater.status()}
