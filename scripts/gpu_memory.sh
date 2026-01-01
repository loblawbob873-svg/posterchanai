#!/bin/bash
# GPU Memory Check Helper Script
# Reads Intel Arc GPU memory from debugfs and outputs percentage
# Usage: sudo /path/to/gpu_memory.sh

DEBUGFS="/sys/kernel/debug/dri/0/i915_gem_objects"

if [ ! -r "$DEBUGFS" ]; then
    echo "error: cannot read $DEBUGFS"
    exit 1
fi

# Parse visible_size and visible_avail from debugfs
content=$(cat "$DEBUGFS")
total=$(echo "$content" | grep -oP 'visible_size:\s*\K\d+')
avail=$(echo "$content" | grep -oP 'visible_avail:\s*\K\d+')

if [ -z "$total" ] || [ -z "$avail" ]; then
    echo "error: could not parse memory values"
    exit 1
fi

used=$((total - avail))
# Use awk for floating point percentage
percentage=$(awk "BEGIN {printf \"%.1f\", ($used / $total) * 100}")

echo "$percentage"
