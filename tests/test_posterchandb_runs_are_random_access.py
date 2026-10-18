"""An index run on disk is mapped RANDOM-access, as the kernel itself reports it.

A run is read by binary search and one posting slice per key. Mapped with the default advice, every page fault
reads around the device's whole readahead window -- 2 MB on server1's md RAID (8 MB on its disks), 128 KB on an
NVMe. Measured 2026-10-18 on a 30M-event store probed cold under a 1.6 GB cap (the 200 GB : 30 GB ratio): 47.2 GB
read from a 10.5 GB store for 100 rounds of queries; with the runs marked random, 7.5 GB.
"""
import sys

import numpy as np
import pytest

from app.services.posterchandb import disk


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/self/smaps")
def test_a_run_on_disk_is_mapped_random_access(tmp_path):
    keys = np.arange(0, 400_000, dtype=np.uint64)
    run = disk.write_run([keys, np.arange(len(keys), dtype=np.uint32)], disk.KV_DTYPES, str(tmp_path), "id-1")
    assert len(run[0]) == len(keys)
    paths = disk.run_files(str(tmp_path), "id-1", 2)
    flags, cur = {}, None
    for line in open("/proc/self/smaps"):
        head = line.split()
        if line[0] in "0123456789abcdef" and "-" in head[0]:
            cur = head[5] if len(head) > 5 else None
        elif line.startswith("VmFlags:") and cur in paths:
            flags[cur] = head[1:]
    assert set(flags) == set(paths), "the run's files are not mapped: %s" % flags
    for p, f in flags.items():
        assert "rr" in f, "%s is mapped without MADV_RANDOM (VmFlags %s)" % (p, " ".join(f))
