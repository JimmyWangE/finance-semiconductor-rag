"""Optional Linux process telemetry; never inspect process arguments or secrets."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from semi_rag.eval_metrics import percentiles


class ResourceSampler:
    def __init__(self, pid: int | None, interval: float):
        self.pid = pid
        self.interval = interval
        self.samples = []
        self.stop = asyncio.Event()
        self.previous = None
        self.process_start = None

    def sample(self):
        now = time.monotonic()
        row = {'monotonic_s': now, 'host_load_1m': os.getloadavg()[0]}
        memory = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            if key in ('MemTotal', 'MemAvailable'):
                memory[key] = int(value.split()[0])
        row['host_available_memory_mb'] = memory['MemAvailable'] / 1024
        if self.pid:
            try:
                stat = Path(f'/proc/{self.pid}/stat').read_text().rsplit(')', 1)[1].split()
                ticks = int(stat[11]) + int(stat[12])
                start = stat[19]
                if self.process_start is not None and start != self.process_start:
                    raise ValueError('PID was reused')
                self.process_start = start
                rss = 0
                for line in Path(f'/proc/{self.pid}/status').read_text().splitlines():
                    if line.startswith('VmRSS:'):
                        rss = int(line.split()[1]) / 1024
                row['server_rss_mb'] = rss
                if self.previous:
                    elapsed = now - self.previous[0]
                    row['server_cpu_percent'] = 100 * (ticks - self.previous[1]) / os.sysconf('SC_CLK_TCK') / elapsed
                self.previous = (now, ticks)
            except (OSError, ValueError, IndexError):
                row['server_unavailable'] = True
        self.samples.append(row)

    async def run(self):
        while not self.stop.is_set():
            self.sample()
            try:
                await asyncio.wait_for(self.stop.wait(), self.interval)
            except TimeoutError:
                pass

    def summary(self):
        return {'pid': self.pid, 'sample_interval_seconds': self.interval, 'samples': len(self.samples),
            'server_rss_mb': percentiles([r['server_rss_mb'] for r in self.samples if 'server_rss_mb' in r]),
            'server_cpu_percent': percentiles([r['server_cpu_percent'] for r in self.samples if 'server_cpu_percent' in r]),
            'server_unavailable_samples': sum(r.get('server_unavailable', False) for r in self.samples),
            'scope': 'Local HTTP server process only, excludes vLLM workers and accelerator memory; CPU can exceed 100%'}
