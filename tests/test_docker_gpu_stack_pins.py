"""Every GPU image's stack is ONE matched set: base image, torch index, torch/torchaudio version.

Upgraded 2026-09-25 (CUDA 13.0 / ROCm 7.2 / oneAPI 2025.3 / alpine 3.24). What these pin, each a way
the image built and then broke at runtime:
  * the CUDA image installed torch for CUDA 12.1 (`cu121`) inside a CUDA 12.5 base;
  * torchaudio was installed UNPINNED with --no-deps; it is pinned to 2.11.0, its LAST release (no 2.12
    wheel exists on any index -- the first version of this pin, torchaudio==torch, would have 404'd);
  * the Intel image must keep torch's bundled oneAPI runtime EQUAL to the base image's: torch 2.12.x+xpu
    bundles 2025.3.2, torch 2.14+xpu bundles 2026.1 -- and Intel ships no 2026 basekit (run-intel.sh:
    mixing the two breaks the loader). Production on server1 runs exactly 2.12.x+xpu on 2025.3.2;
  * ROCm 7.x moved AMD's graphics repo from amdgpu/<ver> to graphics/<ver>; the old path is a 404 and
    a 404 apt source fails `apt-get update`, i.e. the whole ROCm build.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
D = (ROOT / "Dockerfile").read_text()
C = (ROOT / "docker-compose.yml").read_text()


def _arg(name):
    m = re.search(rf"^ARG {name}=(\S+)", D, re.M)
    assert m, f"ARG {name} missing"
    return m.group(1)


def test_one_torch_version_for_torch_and_torchaudio_in_every_gpu_image():
    assert re.fullmatch(r"\d+\.\d+\.\d+", _arg("TORCH_VERSION"))
    assert 'pip install "torch==${TORCH_VERSION}" torchvision --index-url "$TORCH_CUDA_INDEX"' in D
    assert 'pip install "torch==${TORCH_VERSION}" torchvision --index-url "$TORCH_ROCM_INDEX"' in D
    assert _arg("TORCH_XPU_VERSION") == "${TORCH_VERSION}"
    assert 'pip install --no-deps "torchaudio==${TORCHAUDIO_VERSION}"' in D, "torchaudio unpinned beside a pinned torch"
    # torchaudio's last release is 2.11.0 -- pinning it to torch's version names a wheel that does not
    # exist. Same major, and never ahead of torch.
    ta = tuple(map(int, _arg("TORCHAUDIO_VERSION").split(".")))
    tv = tuple(map(int, _arg("TORCH_VERSION").split(".")))
    assert ta[0] == tv[0] and ta <= tv and ta <= (2, 11, 0), ta
    assert not re.search(r"pip install (--no-deps )?torchaudio --index-url", D)


def test_the_cuda_index_matches_the_cuda_base():
    base = re.search(r'CUDA_BASE:-nvidia/cuda:(\d+)\.(\d+)\.\d+-devel-ubuntu24\.04', C)
    assert base, "docker-compose's CUDA base is not a CUDA x.y.z devel image"
    idx = re.search(r"whl/cu(\d+)$", _arg("TORCH_CUDA_INDEX"))
    assert idx and idx.group(1) == base.group(1) + base.group(2), \
        f"torch for cu{idx and idx.group(1)} inside CUDA {base.group(1)}.{base.group(2)}"
    # CUDA 13 dropped Maxwell/Pascal/Volta: building for them is a compile error.
    if int(base.group(1)) >= 13:
        archs = _arg("CUDA_ARCHITECTURES")
        assert not re.search(r"(^|;)(5\d|6\d|70|72)(-|;|$)", archs), archs
        assert "-DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCHITECTURES}" in D


def test_the_rocm_index_matches_the_rocm_version_and_uses_the_7x_graphics_repo():
    ver = _arg("ROCM_VERSION")
    major, minor = ver.split(".")[:2]
    assert _arg("TORCH_ROCM_INDEX").endswith(f"whl/rocm{major}.{minor}")
    if int(major) >= 7:
        assert "repo.radeon.com/graphics/${ROCM_VERSION}/ubuntu" in D
        assert "repo.radeon.com/amdgpu/${ROCM_VERSION}" not in D


def test_the_intel_image_keeps_torchs_oneapi_equal_to_the_base():
    base = re.search(r"INTEL_BASE:-intel/oneapi-basekit:(\d{4}\.\d+)\.\d+-0-devel-ubuntu24\.04", C)
    assert base and base.group(1) == "2025.3", "the Intel base moved off oneAPI 2025.3"
    assert _arg("TORCH_VERSION").startswith("2.12."), \
        "torch past 2.12.x+xpu bundles oneAPI 2026.1 -- a second runtime beside the 2025.3 base"


def test_no_end_of_life_alpine():
    m = re.search(r"^FROM alpine:3\.(\d+) ", D, re.M)
    assert m and int(m.group(1)) >= 21, "alpine 3.20 reached end of life in April 2026"
