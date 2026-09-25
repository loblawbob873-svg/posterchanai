"""A brand-new relay builds its web of trust at once -- it does not sit on the operator alone for a day.

Found 2026-09-25 by the ISO release gate: a freshly installed PosterChanOS server logged "WoT warm from
snapshot (1 members) -- last build 497312h ago, skipping rebuild (daily cadence)" and received 0 posts
in 15 minutes. The operator is admitted before any build, so "the snapshot is empty" was never true on
a fresh node; the build STAMP (0 = never built) is the real first-run signal. A node that HAS built
must still never crawl on an ordinary restart.
"""
from pathlib import Path

from app.services.nostr_relay import thread

ROOT = Path(__file__).resolve().parents[1]


def test_a_fresh_node_with_only_its_operator_builds_now():
    assert thread._needs_first_wot_build(1, 0.0), "a never-built graph was treated as warm"
    assert thread._needs_first_wot_build(0, 0.0)
    assert thread._needs_first_wot_build(0, 1_790_000_000.0), "an emptied snapshot must rebuild"


def test_a_node_that_has_built_never_crawls_on_restart():
    assert not thread._needs_first_wot_build(37_000, 1_790_000_000.0)
    assert not thread._needs_first_wot_build(1, 1_790_000_000.0)


def test_startup_asks_that_question():
    src = (ROOT / "app/services/nostr_relay/thread.py").read_text()
    assert "elif _needs_first_wot_build(len(gate.members()), _read_wot_stamp(cfg)):" in src
