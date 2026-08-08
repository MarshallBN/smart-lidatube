"""Small fail-closed production guards for background audit work."""
import os
from pathlib import Path


class HostResourceGuard:
    """Allow audit work only when host load and recent disk busy time are bounded."""

    def __init__(self, max_load_per_cpu=0.75, max_disk_io_ms=250,
                 cpu_count=os.cpu_count, read_text=None):
        self.max_load_per_cpu = max(0.0, float(max_load_per_cpu))
        self.max_disk_io_ms = max(0, int(max_disk_io_ms))
        self.cpu_count = cpu_count
        self.read_text = read_text or (lambda path: Path(path).read_text())
        self._disk_io_ms = None

    def __call__(self):
        try:
            load = float(self.read_text("/proc/loadavg").split()[0])
            cpus = max(1, int(self.cpu_count() or 1))
            disk_io_ms = 0
            for line in self.read_text("/proc/diskstats").splitlines():
                fields = line.split()
                if len(fields) > 12:
                    disk_io_ms += int(fields[12])
            prior, self._disk_io_ms = self._disk_io_ms, disk_io_ms
            disk_delta = 0 if prior is None else max(0, disk_io_ms - prior)
            return load / cpus <= self.max_load_per_cpu and disk_delta <= self.max_disk_io_ms
        except (OSError, ValueError, IndexError, TypeError):
            return False
