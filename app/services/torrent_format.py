"""The `torrents` list as the chat shows it: one card per download with a progress bar and its buttons.

One copy, importable without libtorrent: the chat's `torrents` command needs it on a node that runs no
torrent client of its own (it lists a remote one's), so it cannot live in libtorrent_service, which is where
the first of the three copies this replaces was.
"""
_STATUS = {
    "downloading": "⬇️ **DOWNLOADING**",
    "seeding": "⬆️ **SEEDING**",
    "finished": "✅ **FINISHED**",
    "checking": "🔍 **CHECKING**",
    "metadata": "📥 **FETCHING METADATA**",
}


def format_torrent_dicts(torrents: list) -> str:
    """Format torrents given as dicts (a client's API answer) as markdown."""
    if not torrents:
        return "No torrents."
    lines = ["**Torrents:**\n"]
    for i, t in enumerate(torrents, 1):
        progress = t.get("progress", 0)
        filled = int(progress / 100 * 10)
        bar = "█" * filled + "░" * (10 - filled)
        download_rate, upload_rate = t.get("download_rate", 0), t.get("upload_rate", 0)
        down = f"{download_rate / 1024:.1f} KB/s" if download_rate > 0 else "-"
        up = f"{upload_rate / 1024:.1f} KB/s" if upload_rate > 0 else "-"
        size_mb = t.get("size", 0) / (1024 * 1024)
        size_str = f"{size_mb:.1f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"
        state = t.get("state", "unknown")
        paused = t.get("is_paused", False) or state == "paused"
        status = "⏸️ **PAUSED**" if paused else _STATUS.get(state, f"❓ **{state.upper()}**")
        toggle_btn = f"[▶ Resume](cmd:torrents resume {i})" if paused else f"[⏸ Pause](cmd:torrents pause {i})"
        lines.append(
            f"**{i}. {t.get('name', 'Unknown')}**\n"
            f"   Status: {status}\n"
            f"   [{bar}] {progress:.1f}% | {size_str}\n"
            f"   ↓{down} ↑{up} | {t.get('seeders', 0)}S/{t.get('peers', 0)}P\n"
            f"   {toggle_btn} | [🗑 Remove](cmd:torrents rm {i})"
        )
    return "\n".join(lines)


def format_torrent_infos(torrents: list) -> str:
    """Format libtorrent TorrentInfo objects (or anything with the same attributes) as markdown."""
    return format_torrent_dicts([
        {"name": t.name, "size": t.size, "progress": t.progress, "download_rate": t.download_rate,
         "upload_rate": t.upload_rate, "state": t.state, "seeders": t.seeders, "peers": t.peers,
         "is_paused": getattr(t, "is_paused", False)}
        for t in torrents])
