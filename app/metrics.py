import time, os, psutil

class StageTimer:
    def __init__(self): 
        self.t0 = time.perf_counter()
        self.stages = []
        self._proc = psutil.Process(os.getpid())
        self._peak_rss = 0  # bytes
        self._cpu_user0 = 0.0
        self._cpu_sys0 = 0.0
        self._io_r0 = 0
        self._io_w0 = 0

        # 初始 CPU/IO 快照（含子进程）
        for p in [self._proc] + self._proc.children(recursive=True):
            try:
                t = p.cpu_times()
                self._cpu_user0 += getattr(t, "user", 0.0)
                self._cpu_sys0  += getattr(t, "system", 0.0)
                io = p.io_counters()
                self._io_r0 += getattr(io, "read_bytes", 0)
                self._io_w0 += getattr(io, "write_bytes", 0)
            except psutil.Error:
                pass

    def _agg_children(self):
        procs = [self._proc] + self._proc.children(recursive=True)
        rss_sum, u, s, rbytes, wbytes = 0, 0.0, 0.0, 0, 0
        for p in procs:
            try:
                m = p.memory_info().rss
                rss_sum += m
                t = p.cpu_times()
                u += getattr(t, "user", 0.0); s += getattr(t, "system", 0.0)
                io = p.io_counters()
                rbytes += getattr(io, "read_bytes", 0)
                wbytes += getattr(io, "write_bytes", 0)
            except psutil.Error:
                pass
        return rss_sum, u, s, rbytes, wbytes

    def mark(self, name: str):
        rss, *_ = self._agg_children()
        self._peak_rss = max(self._peak_rss, rss)
        now = time.perf_counter()
        self.stages.append((name, now - self.t0))

    def snapshot(self):
        rss, u, s, rbytes, wbytes = self._agg_children()
        total = time.perf_counter() - self.t0
        return {
            "total_seconds": round(total, 3),
            "stages": [{"name": n, "t_rel_seconds": round(t, 3)} for (n, t) in self.stages],
            "peak_rss_mb": round(self._peak_rss / (1024 * 1024), 1),
            "cpu_user_seconds": round(u - self._cpu_user0, 3),
            "cpu_sys_seconds": round(s - self._cpu_sys0, 3),
            "io_read_mb": round((rbytes - self._io_r0) / (1024*1024), 1),
            "io_write_mb": round((wbytes - self._io_w0) / (1024*1024), 1),
        }
