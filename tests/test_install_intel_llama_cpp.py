"""The installer builds the llama.cpp production runs on Intel -- and never downgrades it.

Found writing the Intel/Gentoo update guide (2026-10-20): `setup_llama_cpp_intel` force-reinstalled
llama-cpp-python 0.3.22 with the oneAPI that `detect_oneapi_path` prefers (2025.0), on every run. server1
runs 0.3.28 built with oneAPI 2025.2 (the Dockerfile builds 0.3.28 too), so re-running the installer after
an OS update DOWNGRADED it -- to the build whose 2025.0 codegen emits empty generations on the Arc.

Runs the SHIPPED scripts under bash against a fake /opt/intel/oneapi (PC_ONEAPI_BASE) with stub pip/python:
  * oneAPI 2025.2 present -> 0.3.28, compiled by 2025.2's icx/icpx, with 2025.2's compiler + MKL + TBB env,
    and NOTHING from 2025.0 in the build's environment;
  * only 2025.0 -> the 0.3.22 fallback;
  * 0.3.28 already installed with the GPU -> no rebuild at all.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "scripts" / "install"


def _fake_oneapi(base: Path, versions):
    for v in versions:
        cc = base / "compiler" / v
        (cc / "bin").mkdir(parents=True)
        (cc / "env").mkdir(parents=True)
        for tool in ("icx", "icpx"):
            t = cc / "bin" / tool
            t.write_text("#!/bin/sh\n")
            t.chmod(0o755)
        (cc / "env" / "vars.sh").write_text(f'export PC_SOURCED="${{PC_SOURCED}} compiler-{v}"\n')
        for lib in ("mkl",):
            d = base / lib / v / "env"
            d.mkdir(parents=True)
            (d / "vars.sh").write_text(f'export PC_SOURCED="${{PC_SOURCED}} {lib}-{v}"\n')
    d = base / "tbb" / "latest" / "env"
    d.mkdir(parents=True)
    (d / "vars.sh").write_text('export PC_SOURCED="${PC_SOURCED} tbb-latest"\n')


def run(versions, have_gpu_build=False):
    tmp = Path(tempfile.mkdtemp(prefix="pc-intel-llama-"))
    try:
        base, stubs = tmp / "oneapi", tmp / "bin"
        _fake_oneapi(base, versions)
        stubs.mkdir()
        log = tmp / "pip.log"
        (stubs / "pip").write_text(f'#!/bin/bash\necho "ARGS=$* CMAKE=$CMAKE_ARGS SOURCED=$PC_SOURCED" >> {log}\n')
        (stubs / "python").write_text("#!/bin/bash\n"
                                      'case "$*" in *sys.prefix*) echo /venv ;; esac\n'
                                      f'exit {0 if have_gpu_build else 1}\n')
        (stubs / "ldconfig").write_text("#!/bin/bash\necho libze_loader.so.1\n")
        (stubs / "patchelf").write_text("#!/bin/bash\n")
        for f in stubs.iterdir():
            f.chmod(0o755)
        script = f"""
set -e
SCRIPT_DIR={ROOT}
source {INSTALL}/utils.sh
source {INSTALL}/detect.sh
source {INSTALL}/llama_cpp.sh
DISTRO=other
detect_oneapi_path() {{ echo {base}/compiler/2025.0/env/vars.sh; }}
detect_oneapi_root() {{ echo {base}/compiler/{versions[0]}; }}
create_vtune_stub() {{ :; }}
ensure_igc_235() {{ :; }}
setup_llama_cpp_intel
"""
        env = {**os.environ, "PATH": f"{stubs}:/usr/bin:/bin", "PC_ONEAPI_BASE": str(base),
               "HOME": str(tmp), "PC_NONINTERACTIVE": "1"}
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
        calls = log.read_text().splitlines() if log.exists() else []
        return r, calls, str(base)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


needs_bash = pytest.mark.skipif(not shutil.which("bash"), reason="bash required")


@needs_bash
def test_with_oneapi_2025_2_it_builds_0_3_28_with_that_compiler_only():
    r, calls, base = run(["2025.0", "2025.2"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(calls) == 1, calls
    c = calls[0]
    assert "llama-cpp-python==0.3.28" in c and "--no-deps" in c, c
    assert f"{base}/compiler/2025.2/bin/icpx" in c, ("built with the wrong compiler", c)
    assert "compiler-2025.2" in c and "mkl-2025.2" in c and "tbb-latest" in c, ("compiler/MKL/TBB env not sourced", c)
    assert "2025.0" not in c.split("SOURCED=")[1], ("the old oneAPI leaked into the build", c)


@needs_bash
def test_an_older_oneapi_falls_back_to_0_3_22():
    r, calls, _ = run(["2025.0"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls and all("llama-cpp-python==0.3.22" in c for c in calls), calls


@needs_bash
def test_a_working_0_3_28_is_left_alone():
    r, calls, _ = run(["2025.0", "2025.2"], have_gpu_build=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls == [], ("re-running the installer rebuilt a working llama.cpp", calls)
    assert "not rebuilding" in r.stdout, r.stdout
