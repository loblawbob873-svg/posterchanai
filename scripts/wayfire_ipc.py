#!/usr/bin/env python3
"""One Wayfire IPC client for the installed-desktop gates.

Wayfire's IPC is a uint32 little-endian length followed by JSON -- no i3 magic header and no message
type, which is the whole difference from the Sway protocol these gates used to speak. It is factored
out here because three separate gates needed it at once and this package has already shipped two
copies of one helper that drifted apart (see pc-window-close).

`views()` returns a FLAT list. Sway answered a TREE and every caller walked it; Wayfire has no tree,
so a port that keeps the walk is walking a list of one.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess


class PrerequisiteMissing(RuntimeError):
    """There is no live Wayfire session to inspect. Not a failure -- a gate that cannot run."""


def socket_path(env=None):
    """This environment's socket, or the session manager's if we were not started inside one."""
    source = env if env is not None else os.environ
    path = source.get("WAYFIRE_SOCKET") or ""
    if not path:
        got = subprocess.run(["systemctl", "--user", "show-environment"], text=True,
                             capture_output=True)
        for line in (got.stdout or "").splitlines():
            if line.startswith("WAYFIRE_SOCKET="):
                path = line.split("=", 1)[1]
                break
    # A STALE PATH IS "NOT RUNNING", NOT "BROKEN". A WAYFIRE_SOCKET left over from a previous
    # session makes a gate connect, fail, and report a failure about a desktop that is not there.
    if not path or not os.path.exists(path):
        raise PrerequisiteMissing(
            "no installed Wayfire IPC session; run this gate on the active PosterChanOS desktop")
    return path


def call(method, path=None, data=None, timeout=10):
    body = json.dumps({"method": method, "data": data or {}}).encode()
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout)
        client.connect(path or socket_path())
        client.sendall(struct.pack("<I", len(body)) + body)
        head = client.recv(4)
        if len(head) != 4:
            raise RuntimeError("Wayfire IPC closed")
        size = struct.unpack("<I", head)[0]
        reply = b""
        while len(reply) < size:
            chunk = client.recv(size - len(reply))
            if not chunk:
                raise RuntimeError("Wayfire IPC closed mid-reply")
            reply += chunk
    result = json.loads(reply)
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(result["error"])
    return result


def _listing(result, key):
    if isinstance(result, dict) and isinstance(result.get(key), list):
        return result[key]
    return result if isinstance(result, list) else []


def views(path=None, globalise=True):
    """Every mapped view, with geometry in GLOBAL coordinates.

    Wayfire reports view geometry OUTPUT-LOCAL. Measured on a two-monitor desk (DP-1 at x=0, DP-2 at
    x=3840): both full-screen shell surfaces report `geometry.x = 0`. Compared against global output
    rectangles that reads as two desktops on the left monitor and none on the right -- which is
    exactly what the installed-surface gate reported the first time it could run at all.

    `globalise=False` returns what the compositor said, for a caller that wants to see it raw.
    """
    rows = _listing(call("window-rules/list-views", path), "views")
    if not globalise:
        return rows
    at = {}
    for output in outputs(path):
        r = rect_of(output)
        if output.get("id") is not None:
            at["id:%s" % output["id"]] = r
        if output.get("name"):
            at["name:%s" % output["name"]] = r
    for row in rows:
        base = at.get("id:%s" % row.get("output-id")) or at.get("name:%s" % row.get("output-name"))
        if not base:
            continue                        # an unknown output translates by zero, never by a guess
        g = rect_of(row)
        row["geometry"] = {"x": g["x"] + base["x"], "y": g["y"] + base["y"],
                           "width": g["width"], "height": g["height"]}
    return rows


def outputs(path=None):
    return _listing(call("window-rules/list-outputs", path), "outputs")


def rect_of(node):
    """Wayfire calls it `geometry`; Sway called it `rect`. Answer Sway's key names."""
    g = (node or {}).get("geometry") or (node or {}).get("rect") or {}
    return {k: int(g.get(k, 0) or 0) for k in ("x", "y", "width", "height")}


def app_of(node):
    return str((node or {}).get("app-id") or (node or {}).get("app_id") or "")


def title_of(node):
    return str((node or {}).get("title") or (node or {}).get("name") or "")


def output_for(rect, outs):
    """The output a rectangle's centre falls on, by geometry rather than by index."""
    cx = float(rect.get("x", 0)) + float(rect.get("width", 0)) / 2
    cy = float(rect.get("y", 0)) + float(rect.get("height", 0)) / 2
    for output in outs:
        r = rect_of(output)
        if r["x"] <= cx < r["x"] + r["width"] and r["y"] <= cy < r["y"] + r["height"]:
            return output
    return None
