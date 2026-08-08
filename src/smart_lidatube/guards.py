"""Small fail-closed production guards for background audit work."""
import os
import time
from pathlib import Path


class HostResourceGuard:
    """Allow work when load and whole-device busy milliseconds per second are bounded."""

    def __init__(self, max_load_per_cpu=0.75, max_disk_io_ms=250,
                 cpu_count=os.cpu_count, read_text=None, monotonic=time.monotonic,
                 whole_device=None):
        self.max_load_per_cpu = max(0.0, float(max_load_per_cpu))
        self.max_disk_io_ms = max(0, int(max_disk_io_ms))
        self.cpu_count = cpu_count
        self.read_text = read_text or (lambda path: Path(path).read_text())
        self.monotonic = monotonic
        self.whole_device = whole_device or (
            lambda name: (Path("/sys/block") / name).exists()
        )
        self._disk_sample = None

    def __call__(self):
        try:
            load = float(self.read_text("/proc/loadavg").split()[0])
            cpus = max(1, int(self.cpu_count() or 1))
            io_ticks = {}
            for line in self.read_text("/proc/diskstats").splitlines():
                fields = line.split()
                if len(fields) > 12 and self.whole_device(fields[2]):
                    io_ticks[fields[2]] = int(fields[12])
            now = float(self.monotonic())
        except (OSError, ValueError, IndexError, TypeError):
            return False

        prior, self._disk_sample = self._disk_sample, (now, io_ticks)
        if prior is None:
            return False
        elapsed = now - prior[0]
        if elapsed <= 0:
            return False
        if io_ticks.keys() != prior[1].keys():
            return False
        if any(ticks < prior[1][name] for name, ticks in io_ticks.items()):
            return False
        disk_delta = sum(
            ticks - prior[1][name]
            for name, ticks in io_ticks.items()
        )
        busy_ms_per_second = disk_delta / elapsed
        return (load / cpus <= self.max_load_per_cpu
                and busy_ms_per_second <= self.max_disk_io_ms)
