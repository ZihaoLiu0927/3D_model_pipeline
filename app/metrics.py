# app/metrics.py  —— 替换整个文件
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

try:
    import psutil  # 仅用于采集进程指标；若失败也能退化运行
except Exception:  # pragma: no cover
    psutil = None  # type: ignore


def _now_ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000.0, 2)


class StageTimer:
    """
    轻量时间轴/资源打点工具：
      - mark("stage name") 记录阶段时间与资源快照
      - snapshot() 返回结构化 JSON，便于日志或前端展示
    在 macOS / Linux 下均可运行；无 psutil 时自动降级为“仅时间线”模式。
    """

    def __init__(self) -> None:
        self._t0 = time.perf_counter()
        self._marks: List[Dict[str, Any]] = []

        # 显式拿 psutil.Process(os.getpid())，避免被 multiprocessing.Process 遮蔽
        self._proc = None  # type: Optional["psutil.Process"]
        if psutil is not None:
            try:
                self._proc = psutil.Process(os.getpid())
                # 预热一次 CPU 采样，避免第一次恒为 0.0
                try:
                    self._proc.cpu_percent(interval=None)
                except Exception:
                    pass
            except Exception:
                self._proc = None

    def _snapshot(self) -> Dict[str, Any]:
        snap: Dict[str, Any] = {"ts_ms": _now_ms(self._t0)}

        if self._proc is None:
            return snap

        # 内存
        try:
            mem = self._proc.memory_info()
            snap["rss_bytes"] = getattr(mem, "rss", None)
            snap["vms_bytes"] = getattr(mem, "vms", None)
        except Exception:
            pass

        # CPU
        try:
            snap["cpu_percent"] = self._proc.cpu_percent(interval=None)
        except Exception:
            pass

        # I/O（在部分平台/权限下可能不可用）
        try:
            io = self._proc.io_counters()
            snap["io_read_bytes"] = getattr(io, "read_bytes", None)
            snap["io_write_bytes"] = getattr(io, "write_bytes", None)
        except Exception:
            # macOS 或容器中缺失该能力时静默降级
            pass

        return snap

    def mark(self, label: str) -> None:
        self._marks.append({"label": label, **self._snapshot()})

    def snapshot(self) -> Dict[str, Any]:
        return {"timeline": self._marks}
