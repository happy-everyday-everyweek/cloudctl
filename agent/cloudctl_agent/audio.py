"""麦克风电平监测与声音阀值门控。

依赖策略：优先 sounddevice + numpy，其次 pyaudio + numpy，都没有则返回不可用，
由调用方降级。只做电平计算，不落盘、不联网，开销极小。
"""
from __future__ import annotations

import math
import time
from typing import Any


def _np():
    try:
        import numpy  # type: ignore
        return numpy
    except Exception:
        return None


class AudioMeter:
    """打开一个输入设备，读取块电平（dBFS）。"""

    def __init__(self, device: int = -1, sample_rate: int = 16000, block_ms: int = 100) -> None:
        self.device = int(device)
        self.sample_rate = max(8000, int(sample_rate))
        self.block_ms = max(20, min(500, int(block_ms)))
        self.blocksize = max(160, int(self.sample_rate * self.block_ms / 1000))
        self.engine = ""
        self.error = ""
        self._sd = None
        self._pa = None
        self._stream = None
        self._np = _np()

    @property
    def available(self) -> bool:
        return bool(self.engine)

    def open(self) -> bool:
        if self._stream is not None:
            return True
        if self._np is None:
            self.error = "缺少 numpy"
            return False
        try:
            import sounddevice as sd  # type: ignore
            dev = None if self.device < 0 else self.device
            self._stream = sd.RawInputStream(samplerate=self.sample_rate, blocksize=self.blocksize,
                                             dtype="int16", channels=1, device=dev)
            self._stream.start()
            self._sd = sd
            self.engine = "sounddevice"
            return True
        except Exception as e:
            self.error = f"sounddevice 不可用：{e}"
        try:
            import pyaudio  # type: ignore
            pa = pyaudio.PyAudio()
            dev = None if self.device < 0 else self.device
            self._stream = pa.open(format=pyaudio.paInt16, channels=1, rate=self.sample_rate,
                                   input=True, frames_per_buffer=self.blocksize, input_device_index=dev)
            self._pa = pa
            self.engine = "pyaudio"
            return True
        except Exception as e:
            self.error = f"{self.error}；pyaudio 不可用：{e}"
        return False

    def read_db(self) -> float | None:
        """读一个块，返回 dBFS（满量程 0，安静约 -70）。失败返回 None。"""
        if self._stream is None:
            return None
        np = self._np
        try:
            if self.engine == "sounddevice":
                data, _overflow = self._stream.read(self.blocksize)
                buf = bytes(data)
            else:
                buf = self._stream.read(self.blocksize, exception_on_overflow=False)
            samples = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
            if samples.size == 0:
                return None
            rms = float(np.sqrt(np.mean(samples * samples)))
            if rms <= 1e-7:
                return -100.0
            return 20.0 * math.log10(rms)
        except Exception as e:
            self.error = str(e)
            return None

    def close(self) -> None:
        try:
            if self._stream is not None and self.engine == "sounddevice":
                self._stream.stop()
                self._stream.close()
            elif self._stream is not None:
                self._stream.stop_stream()
                self._stream.close()
        except Exception:
            pass
        self._stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None

    def devices(self) -> list[dict]:
        out: list[dict] = []
        sd = self._sd
        if sd is None:
            try:
                import sounddevice as sd  # type: ignore
            except Exception:
                return out
        try:
            for i, d in enumerate(sd.query_devices()):
                if int(d.get("max_input_channels") or 0) > 0:
                    out.append({"index": i, "name": d.get("name"),
                                "channels": int(d.get("max_input_channels") or 0),
                                "rate": int(d.get("default_samplerate") or 0)})
        except Exception:
            return out
        return out


class AudioGate:
    """声音阀值门控：连续超阀值 attack_s 判为开始，静音持续 hold_s 判为结束。"""

    def __init__(self, threshold_db: float = -35.0, attack_s: float = 0.3,
                 hold_s: float = 3.0, block_ms: int = 100) -> None:
        self.threshold_db = float(threshold_db)
        self.attack_s = max(0.0, float(attack_s))
        self.hold_s = max(0.0, float(hold_s))
        self.block_s = max(0.02, float(block_ms) / 1000.0)
        self.loud_s = 0.0
        self.quiet_s = 0.0
        self.speaking = False
        self.peak_db = -100.0
        self.last_db = -100.0

    def feed(self, db: float | None) -> str:
        """返回空串、start 或 stop。"""
        if db is None:
            return ""
        self.last_db = float(db)
        self.peak_db = max(self.peak_db, float(db))
        if db >= self.threshold_db:
            self.loud_s += self.block_s
            self.quiet_s = 0.0
        else:
            self.quiet_s += self.block_s
            self.loud_s = 0.0
        if not self.speaking and self.loud_s >= self.attack_s:
            self.speaking = True
            self.peak_db = float(db)
            return "start"
        if self.speaking and self.hold_s > 0 and self.quiet_s >= self.hold_s:
            self.speaking = False
            return "stop"
        if self.speaking and self.hold_s <= 0 and self.quiet_s >= self.block_s * 2:
            self.speaking = False
            return "stop"
        return ""

    def status(self) -> dict[str, Any]:
        return {"speaking": self.speaking, "last_db": round(self.last_db, 1),
                "peak_db": round(self.peak_db, 1), "threshold_db": self.threshold_db,
                "attack_s": self.attack_s, "hold_s": self.hold_s}
