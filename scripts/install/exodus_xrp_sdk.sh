#!/usr/bin/env bash
# Install the offline XRP SDK without changing the app's signer/relay dependencies.
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
sdk_target="${EXODUS_XRP_VENV:-/usr/local/libexec/pc-exodus/xrp-venv}"
bootstrap_python="${EXODUS_XRP_BOOTSTRAP_PYTHON:-python3}"
case "$sdk_target" in
  /*/xrp-venv|/*/pc-wallet-xrp-sdk-venv) ;;
  *) echo 'Use a dedicated absolute path ending in xrp-venv or pc-wallet-xrp-sdk-venv.' >&2; exit 2 ;;
esac
"$bootstrap_python" -m venv "$sdk_target"
"$sdk_target/bin/python" -m pip install --require-virtualenv -r "$repo_root/requirements-exodus-xrp.txt"
"$sdk_target/bin/python" -m pip check
"$sdk_target/bin/python" -I -c 'from importlib.metadata import version; print("Offline XRP SDK:", version("xrpl-py"), "isolated websockets:", version("websockets"))'
