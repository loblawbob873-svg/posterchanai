"""The bare-metal installer and the Docker images install the SAME GPU stack.

They drifted: the images moved to torch 2.12.1 on cu130 / rocm7.2 while install.sh still pulled torch
for CUDA 12.1 and a ROCm NIGHTLY. Everything here is read from the shipped scripts; the CUDA index
choice and the music step's torchaudio pick are RUN under bash.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKER = (ROOT / "Dockerfile").read_text()
UTILS = ROOT / "scripts/install/utils.sh"
U = UTILS.read_text()


def _arg(name):
    return re.search(rf"^ARG {name}=(\S+)", DOCKER, re.M).group(1)


def _var(name):
    return re.search(rf"^{name}=(\S+)", U, re.M).group(1)


def test_versions_and_indexes_match_the_dockerfile():
    assert _var("PC_TORCH_VERSION") == _arg("TORCH_VERSION")
    assert _var("PC_TORCHAUDIO_VERSION") == _arg("TORCHAUDIO_VERSION")
    assert _var("PC_TORCH_ROCM_INDEX") == _arg("TORCH_ROCM_INDEX")
    assert _var("PC_TORCH_XPU_INDEX") == _arg("TORCH_XPU_INDEX")


def test_no_installer_step_pins_its_own_torch_any_more():
    for f in ("image.sh", "setup.sh", "music.sh"):
        src = (ROOT / "scripts/install" / f).read_text()
        assert not re.search(r"whl/(cu121|nightly|rocm6\.3)", src), f"{f} still pins an old stack"
        assert not re.search(r"pip install[^\n]*\btorch==2\.\d+\.\d+", src), f"{f} hard-codes a torch version"


def _cuda_index(driver):
    stub = f'nvidia-smi() {{ {"echo " + driver if driver else "return 127"}; }}\n'
    r = subprocess.run(["bash", "-c", stub + U + "\npc_torch_cuda_index"], capture_output=True, text=True, timeout=20)
    return r.stdout.strip().rsplit("/", 1)[-1]


def test_the_driver_picks_the_cuda_build():
    """cu130 needs driver 580+; installing it on an older driver 'works' and then sees no GPU."""
    assert _cuda_index("595.84") == _arg("TORCH_CUDA_INDEX").rsplit("/", 1)[-1] == "cu130"
    assert _cuda_index("580.65") == "cu130"
    assert _cuda_index("575.51") == "cu129"
    assert _cuda_index("565.77") == "cu126"
    assert _cuda_index("") == "cu130", "no driver yet: the newest build"


def test_music_matches_torchaudio_to_the_torch_actually_installed():
    music = (ROOT / "scripts/install/music.sh").read_text()
    block = music[music.index("local TA_VER BASE"):music.index('if "$PY" -c \'import torchaudio\'')]
    case = music[music.index('case "$TORCH_VER" in'):music.index("esac", music.index('case "$TORCH_VER" in')) + 4]
    for torch, want_ta, want_idx in (("2.12.1+cu130", "2.11.0", "cu130"), ("2.12.1+xpu", "2.11.0", "xpu"),
                                     ("2.12.1+rocm7.2", "2.11.0", "rocm7.2"), ("2.5.1+cu121", "2.5.1", "cu121")):
        script = (f'PC_TORCHAUDIO_VERSION=2.11.0; PC_TORCH_XPU_INDEX=https://download.pytorch.org/whl/xpu\n'
                  f'f() {{ TORCH_VER="{torch}"\n{block}\n{case}\necho "$TA_VER $TORCH_IDX"; }}; f')
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20).stdout.split()
        assert out == [want_ta, f"https://download.pytorch.org/whl/{want_idx}"], (torch, out)
