"""Freed model memory goes back to the OS, at every model unload.

server1's app process reached 38.5 GB on 2026-10-16 and starved the deploy gate: every chat<->voice swap (a talking
bot swaps on each reply) left 0.6-1.1 GB behind, because glibc keeps large freed blocks in the process. Measured on
the voice model itself (load + unload on CPU): 4.2 GB kept without `malloc_trim`, 1.2 GB with it. This holds the
helper to that -- a process that frees a big pile of host allocations gets its RSS back -- and holds the wiring:
every model unload goes through reset_vram_mode, which must call it.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBE = r"""
import sys, gc
sys.path.insert(0, %r)
from app.services import vram_manager
def rss(): return int(open('/proc/self/status').read().split('VmRSS:')[1].split()[0]) // 1024
# model-loading shape: many large buffers (tensors read from a checkpoint), freed when the model is dropped
# glibc's mmap threshold rises to the size of the largest block freed (a model's biggest tensor), after which blocks
# below it come from the heap and are NOT returned when freed -- which is what a reload after an unload looks like
big = bytearray(16 * 1024 * 1024); del big
bufs = [bytearray(3 * 1024 * 1024) for _ in range(400)]           # ~1.2 GB in 3 MB pieces, now on the heap
for b in bufs: b[::4096] = b'\1' * len(b[::4096])
pin = bytearray(1024)                                               # something allocated after them stays alive
peak = rss()
del bufs; gc.collect()
before = rss()
vram_manager.reset_vram_mode()                                      # what every model unload calls
after = rss()
print(peak, before, after)
"""


def test_reset_vram_mode_returns_freed_memory_to_the_os():
    r = subprocess.run([sys.executable, "-c", PROBE % ROOT], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    peak, before, after = map(int, r.stdout.split()[-3:])
    assert peak > 1000, ("the probe did not allocate", peak)
    assert before > peak * 0.5, ("glibc returned it on its own; this measures nothing", peak, before)
    assert after < before - 500, ("freed model memory stayed in the process", peak, before, after)


def test_every_model_unload_goes_through_it():
    src = open(os.path.join(ROOT, "app/services/vram_manager.py")).read()
    body = src[src.index("def reset_vram_mode():"):]
    body = body[:body.index("\ndef ")]
    assert "release_host_memory()" in body
    for svc in ("voice_local", "music_local", "video_service", "llama_service", "diffusers_service"):
        assert "reset_vram_mode()" in open(os.path.join(ROOT, "app/services/%s.py" % svc)).read(), svc
