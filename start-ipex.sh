#!/bin/bash
# Start posterchanai with IPEX-LLM backend

# Set Intel oneAPI environment
if [ -f /opt/intel/oneapi/2024.2/oneapi-vars.sh ]; then
    source /opt/intel/oneapi/2024.2/oneapi-vars.sh
elif [ -f /opt/intel/oneapi/setvars.sh ]; then
    source /opt/intel/oneapi/setvars.sh
else
    echo "Warning: Intel oneAPI not found. XPU acceleration may not work."
fi

# Activate IPEX venv
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/venv-ipex/bin/activate"

# Run the app
exec python "$SCRIPT_DIR/run.py" "$@"
