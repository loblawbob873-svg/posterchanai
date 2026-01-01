#!/bin/bash
# Source Intel oneAPI environment
source /home/verita84/intel/oneapi/setvars.sh >/dev/null 2>&1

# Set additional environment variables
export ZES_ENABLE_SYSMAN=1
export OCL_ICD_FILENAMES=/usr/lib64/libintelocl.so

# Start the application
exec /home/verita84/posterchanai/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 3051
