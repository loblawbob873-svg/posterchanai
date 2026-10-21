# Intel Arc on Gentoo — updating the OS without breaking the GPU stack

The full walkthrough (with the dependency picture) is the shared doc
<https://claude.ai/artifact/2DTcxw4pST8CXLQE1bLniS>; this is the short form that ships with the code.

## The stack

| Layer | From | Notes |
|---|---|---|
| i915 kernel driver | `sys-kernel/gentoo-kernel-bin` | reboot + verify after a new kernel |
| compute-runtime, level-zero, gmmlib, system-controller, metrics libs | Portage | the GPU runtime torch and llama.cpp run on |
| IGC + `llvm`/`clang`/`lld`, `opencl-clang`, `spirv-llvm-translator` | Portage | IGC takes ONE LLVM slot (`LLVM_COMPAT`); the chain must share it |
| oneAPI runtime 2025.3.2 | pip (torch XPU wheel) in `venv-unified` | uses the SYSTEM Level Zero loader and driver |
| oneAPI compilers | `/opt/intel/oneapi` | only to build llama-cpp-python (0.3.28 needs 2025.2+) |

## Hold the set (once)

```bash
# package.mask is a directory here; ceilings = the versions that work
cat > /etc/portage/package.mask/intel-gpu <<'EOF'
>dev-util/intel-graphics-compiler-2.41.9-r1
>dev-libs/intel-compute-runtime-26.31.39395.14
>dev-libs/level-zero-1.32.0-r1
>media-libs/gmmlib-22.10.2
>dev-util/intel-graphics-system-controller-1.3.2
>dev-libs/intel-metrics-library-1.0.238
>dev-libs/intel-metrics-discovery-1.16.194
EOF
cat > /etc/portage/package.use/intel-gpu-llvm <<'EOF'
dev-util/intel-graphics-compiler LLVM_SLOT: -* 22
dev-libs/opencl-clang LLVM_SLOT: -* 22
EOF
```

Remove any `=…` Arc pins in `package.accept_keywords` that name versions no longer installed.

## Raise it, as one set

1. Read what the new `intel-compute-runtime` ebuild requires (IGC, level-zero range, gmmlib) and the new IGC's
   `LLVM_COMPAT`.
2. `quickpkg --include-config=y` the current set (rollback in minutes).
3. Raise every ceiling together; keyword exact `~amd64` versions; move the LLVM slot only with IGC.
4. `emerge -pv --tree --verbose-conflicts dev-libs/intel-compute-runtime` — fix the files until it resolves.
5. `emerge -B1 …` to build, then with `posterchanai.service` stopped `emerge -1 --usepkgonly …`,
   `rm -rf ~/.cache/libsycl_cache ~/.cache/neo_compiler_cache`, start the service.
6. Verify: `clinfo -l`; `sycl-ls` shows `[level_zero:gpu]`; in the service env
   (`LD_LIBRARY_PATH=venv-unified/lib:/usr/lib64 ONEAPI_DEVICE_SELECTOR=level_zero:gpu`) torch sees the XPU and
   `llama_cpp.llama_supports_gpu_offload()` is `True`; a long chat answers in sentences at ~19 tokens/s; `geni`
   returns an image.

## When to re-run the installer

A Portage GPU-set update needs **no** reinstall (the venv talks to the system driver). Re-run
`./install.sh --ai --backend intel` when the Python `venv-unified` was made with changed, or when llama.cpp
falls back to the CPU (~2 tokens/s). The installer builds llama-cpp-python **0.3.28** with the newest oneAPI
2025.2+ compiler it finds (compiler + MKL + TBB, in a clean environment), skips the rebuild when a GPU build of
that version is already installed, and builds 0.3.22 only when no 2025.2+ compiler exists
(`scripts/install/llama_cpp.sh`, `tests/test_install_intel_llama_cpp.py`). The Docker image builds the same
0.3.28 against its oneAPI 2025.3.2 base.
