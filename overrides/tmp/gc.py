#!/usr/bin/env python3
import re

colors = ["1;36", "1;35", "1;32", "1;33"]
with open("/opt/gentoo-installer/gentoo.sh") as f:
    lines = f.readlines()
changed = 0
for i, line in enumerate(lines):
    stripped = line.strip()
    if not stripped.startswith("echo "):
        continue
    m = re.search(r'echo\s+"([^"]*)"', line)
    if not m:
        continue
    after = line[m.end():]
    if ">" in after or "|" in after:
        continue
    text = m.group(1)
    color = colors[changed % len(colors)]
    indent = line[:m.start()]
    new_line = indent + 'echo -e "\\033[' + color + 'm' + text + '\\033[0m"' + after
    if new_line != line:
        lines[i] = new_line
        changed += 1
with open("/opt/gentoo-installer/gentoo.sh", "w") as f:
    f.writelines(lines)
print(changed)
