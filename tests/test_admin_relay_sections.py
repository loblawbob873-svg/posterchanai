"""Admin → Relay is split into sections ("Settings → Relay is too clunky and should be broken up", 2026-10-11).

Moving markup must not lose a setting: admin.js hydrates by id and saves by name, so a key that vanished from the
tab would silently stop loading (and a checkbox would save `false` over the stored value). The lists below are
EVERY name and id the tab had before it was split (templates/admin/tabs/nostr_relay.html at 3af14660d), pinned
as literals -- a later edit that drops one fails here, not on a node.

The rest is the shape the operator asked for: every limit in ONE section (the posting limits used to sit inside
Auto-clean), a Database section with the PosterChanDB knobs, every setting inside exactly one section, and every
section reachable from the section bar.
"""
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAB = ROOT / "templates/admin/tabs/nostr_relay.html"

OLD_NAMES = [
    'apps_require_nip05', 'nip05_grants_access', 'nostr_dvm_blossom_url', 'nostr_dvm_enabled', 'nostr_dvm_peers',
    'nostr_relay_backup_datastore', 'nostr_relay_bind', 'nostr_relay_block_bridged', 'nostr_relay_block_json_posts',
    'nostr_relay_blocked_langs', 'nostr_relay_blocked_pubkeys', 'nostr_relay_blocked_relays',
    'nostr_relay_blocked_words', 'nostr_relay_contact', 'nostr_relay_description', 'nostr_relay_disable_proxy',
    'nostr_relay_fetch_ancestors', 'nostr_relay_free_retention_days', 'nostr_relay_gift_wraps_per_recipient',
    'nostr_relay_ingest_kinds', 'nostr_relay_max_events', 'nostr_relay_name', 'nostr_relay_nip05_domain',
    'nostr_relay_nip05_enabled', 'nostr_relay_nip05_names', 'nostr_relay_nip05_relays', 'nostr_relay_paid_lud16',
    'nostr_relay_paid_pubkey', 'nostr_relay_paid_retention_days', 'nostr_relay_paid_retention_enabled',
    'nostr_relay_paid_sats_per_month', 'nostr_relay_port', 'nostr_relay_posterchan_clients_only',
    'nostr_relay_posterchan_origins', 'nostr_relay_posts_per_min', 'nostr_relay_private_relays',
    'nostr_relay_pubkey', 'nostr_relay_retention_days', 'nostr_relay_same_per_hour', 'nostr_relay_send_only',
    'nostr_relay_supported_grasps', 'nostr_relay_upstream_relays', 'nostr_relay_wot_depth',
    'nostr_relay_wot_depth3_crawl_max', 'nostr_relay_wot_enabled', 'nostr_relay_wot_refresh_sec',
    'nostr_relay_wot_seeds', 'posterchandb_flush_seconds', 'posterchandb_mode', 'posterchandb_read_cache_mb',
]
# Ids the tab's own scripts and admin-*.js bind to (lists, buttons, status lines) -- not settings, but a missing
# one is a dead control.
OLD_IDS = OLD_NAMES + [
    'access-policy-affected', 'access-policy-enabled', 'access-policy-fedi', 'access-policy-preview',
    'access-policy-run', 'access-policy-save', 'access-policy-status', 'blk_list', 'blk_panel', 'blk_search',
    'blk_summary', 'ids_filter', 'ids_list', 'ids_panel', 'ids_retire', 'ids_search', 'ids_sel_all', 'ids_sel_none',
    'ids_summary', 'pcdb-status', 'relay-access-policy', 'relay-backup-btn', 'relay-npub', 'relay-restore-btn',
    'relay-restore-status', 'relayBackfillBtn', 'relayBackfillNpub', 'relayBackfillStatus', 'relayLangGrid',
    'relayPaidGrantBtn', 'relayPaidGrantDays', 'relayPaidGrantPk', 'relayPaidList', 'relayPaidRefresh',
    'relayPaidStatus', 'relayPruneBtn', 'relayPruneDryBtn', 'relayPruneStatus', 'relayPurgeBlocksBtn',
    'relayPurgeBlocksStatus', 'relayRefreshWotBtn', 'relayWotStatus', 'rep_list', 'rep_reload', 'rep_summary',
    'tab-relay',
]

LIMIT_KEYS = ['nostr_relay_posts_per_min', 'nostr_relay_same_per_hour', 'nostr_relay_gift_wraps_per_recipient',
              'nostr_relay_max_connections', 'nostr_relay_max_subs_per_conn', 'nostr_relay_max_filters_per_req',
              'nostr_relay_max_message_kb']


class _Sections(HTMLParser):
    """name -> the data-sec of the <section class="admin-sec"> holding it (None = outside every section)."""

    def __init__(self):
        super().__init__()
        self.stack, self.where, self.sections, self.nav = [], {}, [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "section" and "admin-sec" in (a.get("class") or "").split():
            self.sections.append(a.get("data-sec"))
            self.stack.append(a.get("data-sec"))
        elif tag == "section":
            self.stack.append(self.stack[-1] if self.stack else None)
        if tag == "button" and "subnav-btn" in (a.get("class") or "").split():
            self.nav.append(a.get("data-sec"))
            assert a.get("type") == "button", "a section button inside the settings form must not submit it"
        if tag in ("input", "select", "textarea") and a.get("name"):
            self.where.setdefault(a["name"], []).append(self.stack[-1] if self.stack else None)

    def handle_endtag(self, tag):
        if tag == "section" and self.stack:
            self.stack.pop()


def _parse():
    p = _Sections()
    p.feed(TAB.read_text(encoding="utf-8"))
    return p


def test_no_setting_or_control_was_lost_in_the_move():
    html = TAB.read_text(encoding="utf-8")
    names = set(re.findall(r'\bname="([^"]+)"', html))
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    assert not set(OLD_NAMES) - names, "settings lost from the Relay tab: %s" % sorted(set(OLD_NAMES) - names)
    assert not set(OLD_IDS) - ids, "ids lost from the Relay tab: %s" % sorted(set(OLD_IDS) - ids)
    for k in OLD_NAMES:     # same element: id == name (admin.js hydrates by one and saves by the other)
        assert re.search(r'<(?:input|select|textarea)\b[^>]*\bid="%s"[^>]*\bname="%s"' % (k, k), html), k
    dup = [i for i in ids if len(re.findall(r'\bid="%s"' % re.escape(i), html)) > 1]
    assert not dup, "duplicate ids: %s" % dup


def test_every_setting_is_in_exactly_one_section():
    p = _parse()
    stray = {k: v for k, v in p.where.items() if len(v) != 1 or v[0] is None}
    assert not stray, "settings outside a section (or in two): %s" % stray


def test_the_section_bar_reaches_every_section():
    p = _parse()
    assert p.sections == ["general", "access", "limits", "sync", "retention", "moderation", "database"]
    assert p.nav == p.sections + ["all"]


def test_every_limit_is_in_limits_and_none_left_in_auto_clean():
    p = _parse()
    for k in LIMIT_KEYS:
        assert p.where.get(k) == ["limits"], (k, p.where.get(k))
    html = TAB.read_text(encoding="utf-8")
    auto = html[html.index("<legend>Auto-clean"):html.index("</fieldset>", html.index("<legend>Auto-clean"))]
    assert "per_min" not in auto and "same_per_hour" not in auto and "gift_wraps" not in auto


def test_the_database_section_has_the_posterchandb_knobs_and_a_status_panel():
    from app.services import relay_tunables
    p = _parse()
    for k in list(relay_tunables.PCDB) + ["posterchandb_mode"]:
        assert p.where.get(k) == ["database"], (k, p.where.get(k))
    assert 'id="pcdb-stats"' in TAB.read_text(encoding="utf-8")


def test_every_tunable_input_carries_its_range():
    """The field says the same range the server enforces (relay_tunables), so the hint is not a guess."""
    from app.services import relay_tunables
    html = TAB.read_text(encoding="utf-8")
    for k, knob in relay_tunables.ALL.items():
        tag = re.search(r'<input\b[^>]*\bname="%s"[^>]*>' % k, html).group(0)
        lo = float(re.search(r'\bmin="([^"]+)"', tag).group(1))
        hi = float(re.search(r'\bmax="([^"]+)"', tag).group(1))
        assert (lo, hi) == (float(knob.lo), float(knob.hi)), (k, lo, hi, knob)


def test_the_section_script_ships_with_the_admin_page():
    admin = (ROOT / "templates/admin.html").read_text(encoding="utf-8")
    assert "/static/js/admin-subnav.js" in admin
    js = (ROOT / "static/js/admin.js").read_text(encoding="utf-8")
    assert ".split('/')[0]" in js, "a #tab-relay/limits deep link must still open the Relay tab"
