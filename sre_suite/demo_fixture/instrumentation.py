"""Actual process measurements for the serving process; unsupported readings remain absent."""

import os
import resource
import subprocess
import sys
import time
from pathlib import Path


def process_metrics(started: float) -> dict[str, float]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    result = {
        "sre_demo_process_cpu_seconds_total": usage.ru_utime + usage.ru_stime,
        "sre_demo_process_uptime_seconds": max(0.0, time.monotonic() - started),
    }
    try:
        if sys.platform == "linux":
            resident = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf(
                "SC_PAGE_SIZE"
            )
        elif sys.platform == "darwin":
            resident = (
                int(
                    subprocess.run(
                        ["/bin/ps", "-o", "rss=", "-p", str(os.getpid())],
                        check=True,
                        capture_output=True,
                        timeout=0.2,
                        env={"LANG": "C"},
                    ).stdout.strip()
                )
                * 1024
            )
        else:
            resident = None
        if resident is not None and resident > 0:
            result["sre_demo_process_resident_memory_bytes"] = float(resident)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    try:
        directory = "/proc/self/fd" if sys.platform == "linux" else "/dev/fd"
        # listdir's own directory descriptor is closed before fstat; do not count it.
        opened = 0
        for name in os.listdir(directory):
            if name.isdigit():
                try:
                    os.fstat(int(name))
                    opened += 1
                except OSError:
                    pass
        result["sre_demo_process_open_fds"] = float(opened)
    except OSError:
        pass
    return result
