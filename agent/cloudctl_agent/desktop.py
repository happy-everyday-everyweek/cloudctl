"""远程桌面：JPEG 帧推流 + 鼠标键盘事件注入。

输入注入优先用 pynput（跨平台、API 干净），拿不到时回退到 Windows 的
SendInput。帧率与质量由规则里的 desktop 节点控制。
"""
from __future__ import annotations

import asyncio
import base64
import io
import time
from typing import Any, Callable, Awaitable

from .capture import ScreenSource
from .util import IS_WINDOWS


class DesktopStreamer:
    def __init__(self, log) -> None:
        self.log = log
        self._task: asyncio.Task | None = None
        self.running = False
        self.fps = 8
        self.quality = 55
        self.monitor = 0
        self.scale = 1.0
        self.frames = 0
        self.last_frame_ts = 0.0
        self.last_error = ""

    async def start(self, send: Callable[[dict], Awaitable[None]], **opts) -> dict[str, Any]:
        if self.running:
            await self.stop()
        self.fps = max(1, min(30, int(opts.get("fps") or 8)))
        self.quality = max(20, min(90, int(opts.get("quality") or 55)))
        self.monitor = int(opts.get("monitor") or 0)
        self.scale = float(opts.get("scale") or 1.0)
        self.frames = 0
        self.last_error = ""
        self.running = True
        self._task = asyncio.create_task(self._loop(send), name="desktop-stream")
        return {"started": True, "fps": self.fps, "quality": self.quality, "monitor": self.monitor}

    async def stop(self) -> dict[str, Any]:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
            self._task = None
        return {"stopped": True, "frames": self.frames}

    async def _loop(self, send: Callable[[dict], Awaitable[None]]) -> None:
        src = ScreenSource(self.monitor)
        interval = 1.0 / self.fps
        seq = 0
        try:
            while self.running:
                t0 = time.time()
                try:
                    img = await asyncio.to_thread(src.grab)
                except Exception as e:
                    self.last_error = str(e)
                    self.log.warning("桌面抓帧失败：%s", e)
                    await asyncio.sleep(1.0)
                    continue
                if self.scale != 1.0:
                    img = img.resize((max(1, int(img.width * self.scale)), max(1, int(img.height * self.scale))))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=self.quality, optimize=False)
                data = buf.getvalue()
                seq += 1
                self.frames = seq
                self.last_frame_ts = time.time()
                await send(
                    {
                        "type": "frame",
                        "stream": "desktop",
                        "seq": seq,
                        "w": img.width,
                        "h": img.height,
                        "quality": self.quality,
                        "t": int(self.last_frame_ts * 1000),
                        "jpeg": base64.b64encode(data).decode("ascii"),
                    }
                )
                await asyncio.sleep(max(0.0, interval - (time.time() - t0)))
        except asyncio.CancelledError:
            raise
        finally:
            self.running = False
            try:
                await send({"type": "stream.end", "stream": "desktop", "frames": seq})
            except Exception:
                pass


class InputInjector:
    """把前端传来的鼠标/键盘事件回注到本机。"""

    def __init__(self, log) -> None:
        self.log = log
        self.enabled = True
        self._mouse = None
        self._keyboard = None
        self._ctrl = None
        self._ready = False

    def _ensure(self) -> bool:
        if self._ready:
            return True
        try:
            from pynput import keyboard, mouse  # type: ignore

            self._mouse = mouse.Controller()
            self._keyboard = keyboard.Controller()
            self._ctrl = keyboard
            self._ready = True
        except Exception as e:
            self.log.warning("pynput 不可用，改用 SendInput：%s", e)
            self._ready = IS_WINDOWS
        return self._ready

    def apply(self, events: list[dict]) -> dict[str, Any]:
        if not self.enabled:
            return {"applied": 0, "skipped": len(events), "reason": "input_disabled"}
        if not self._ensure():
            return {"applied": 0, "reason": "no_input_backend"}
        applied = 0
        for ev in events or []:
            try:
                self._one(ev)
                applied += 1
            except Exception as e:
                self.log.debug("输入事件失败 %s: %s", ev, e)
        return {"applied": applied, "total": len(events or [])}

    def _one(self, ev: dict) -> None:
        k = ev.get("k")
        if k == "move":
            self._move(float(ev.get("x", 0)), float(ev.get("y", 0)), abs_mode=bool(ev.get("abs", True)))
        elif k in ("down", "up"):
            self._button(k, ev.get("btn", "left"))
        elif k == "wheel":
            self._wheel(int(ev.get("dy", 0)))
        elif k in ("key_down", "key_up"):
            self._key(k, ev.get("key", ""))
        elif k == "text":
            self._type(str(ev.get("s", "")))

    # --- 具体实现 ---
    def _move(self, x: float, y: float, abs_mode: bool = True) -> None:
        if self._mouse is not None:
            if abs_mode:
                self._mouse.position = (x, y)
            else:
                self._mouse.move(x, y)
            return
        import ctypes

        if abs_mode:
            ctypes.windll.user32.SetCursorPos(int(x), int(y))
        else:
            ctypes.windll.user32.mouse_event(0x0001, int(x), int(y), 0, 0)

    def _button(self, action: str, btn: str) -> None:
        if self._mouse is not None:
            from pynput.mouse import Button  # type: ignore

            mapping = {"left": Button.left, "right": Button.right, "middle": Button.middle}
            b = mapping.get(btn, Button.left)
            self._mouse.press(b) if action == "down" else self._mouse.release(b)
            return
        import ctypes

        flags = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
        down, up = flags.get(btn, flags["left"])
        ctypes.windll.user32.mouse_event(down if action == "down" else up, 0, 0, 0, 0)

    def _wheel(self, dy: int) -> None:
        if self._mouse is not None:
            self._mouse.scroll(0, -dy / 120 if abs(dy) > 5 else -dy)
            return
        import ctypes

        ctypes.windll.user32.mouse_event(0x0800, 0, 0, int(dy), 0)

    def _key(self, action: str, key: str) -> None:
        if self._keyboard is None:
            return
        key = (key or "").lower()
        special = {
            "enter": "enter", "tab": "tab", "esc": "esc", "backspace": "backspace",
            "space": "space", "up": "up", "down": "down", "left": "left", "right": "right",
            "ctrl": "ctrl", "alt": "alt", "shift": "shift", "win": "cmd", "delete": "delete",
            "home": "home", "end": "end", "pageup": "page_up", "pagedown": "page_down",
        }
        name = special.get(key, key)
        try:
            target = getattr(self._ctrl.Key, name)
        except Exception:
            if len(key) == 1:
                target = key
            else:
                return
        self._keyboard.press(target) if action == "key_down" else self._keyboard.release(target)

    def _type(self, s: str) -> None:
        if self._keyboard is not None:
            self._keyboard.type(s)
