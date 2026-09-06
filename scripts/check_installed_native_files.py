#!/usr/bin/env python3
"""Click local files through an installed Electron renderer.

This gate needs no account and therefore runs safely in an isolated Electron profile.  It creates
two fixtures in a temporary directory on the target machine, reaches that directory through the
packaged ``pcHost`` bridge and Files UI, checks the .conf Open With choices, and opens an SVG in the
real Preview surface.  Temporary files are removed even when an assertion fails.
"""

import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.request

from check_installed_desktop_account import BASE, CDP, native_files_check, choose_shell_page


async def choose_page():
    pages = [p for p in json.load(urllib.request.urlopen(BASE + "/json/list", timeout=5))
             if p.get("type") == "page" and p.get("url", "").startswith("app://posterchan/")]
    if not pages:
        raise RuntimeError("no installed PosterChan page is attached")
    # THE SHELL, not whatever /json/list happened to list first — see choose_shell_page.
    return await choose_shell_page()


async def main():
    page = await choose_page()
    supplied = os.environ.get("PC_NATIVE_FILES_FIXTURE", "").strip()
    owner = nullcontext(supplied) if supplied else tempfile.TemporaryDirectory(
        prefix="posterchan-installed-files-")
    with owner as fixture_dir:
        fixture = Path(fixture_dir)
        if not supplied:
            (fixture / "posterchan-installed.conf").write_text("installed=true\n", encoding="utf-8")
            (fixture / "posterchan-installed.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
                '<rect width="8" height="8" fill="#713dd8"/></svg>', encoding="utf-8")
        async with CDP(page["webSocketDebuggerUrl"]) as cdp:
            result = await cdp.eval(native_files_check(fixture))
        assert result["path"] and result["rows"] == 2, result
        assert {"code", "host"}.issubset(result["confChoices"]), result
        assert result["preview"] and not result["errors"], result
    print("OK installed native Files navigation, Open With and Preview")
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (urllib.error.URLError, ConnectionRefusedError) as exc:
        print("SKIP installed Electron is not attached on the loopback CDP port (" + str(exc) + ")")
        sys.exit(2)
