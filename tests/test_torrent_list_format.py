"""The chat's torrent list has ONE formatter (app/services/torrent_format.py), importable without libtorrent.

There were three copies (libtorrent_service x2, command_service) -- proven identical over 504 generated torrents
before they were folded. What a person reads: a card per torrent with its state, a 10-cell bar, size, speeds, and
the Resume-or-Pause plus Remove buttons numbered by position; the object and the dict forms render the same.
"""
import types

from app.services.torrent_format import format_torrent_dicts, format_torrent_infos


def test_a_card_per_torrent_with_the_right_buttons():
    rows = [dict(name="Ubuntu", size=3 * 2**30, progress=50, download_rate=2048, upload_rate=0, state="downloading",
                 seeders=4, peers=7, is_paused=False),
            dict(name="Song", size=5 * 2**20, progress=100, download_rate=0, upload_rate=0, state="seeding",
                 seeders=1, peers=0, is_paused=True)]
    out = format_torrent_dicts(rows)
    assert out.startswith("**Torrents:**\n")
    assert ("**1. Ubuntu**\n   Status: ⬇️ **DOWNLOADING**\n   [█████░░░░░] 50.0% | 3.00 GB\n   ↓2.0 KB/s ↑- | 4S/7P\n"
            "   [⏸ Pause](cmd:torrents pause 1) | [🗑 Remove](cmd:torrents rm 1)") in out
    assert "**2. Song**\n   Status: ⏸️ **PAUSED**" in out and "[▶ Resume](cmd:torrents resume 2)" in out
    assert format_torrent_infos([types.SimpleNamespace(**r) for r in rows]) == out
    assert format_torrent_dicts([]) == "No torrents."
    assert "❓ **WEIRD**" in format_torrent_dicts([{"state": "weird"}])


def test_the_old_names_still_answer():
    from app.services.command_service._common import _format_bt_list_from_dicts
    assert _format_bt_list_from_dicts is format_torrent_dicts
