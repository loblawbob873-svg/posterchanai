"""The Calendar's Nostr layer — NIP-52 public events (31922 date-based, 31923 time-based).

A MIRROR beside the personal calendar, like a subscribed .ics: drawn, openable, never written to
CalDAV and never editable — a network event is somebody's published statement, not a row of yours.
The parser and the window expansion are LIFTED and RUN, because dates are exactly where a reader
and a spec quietly disagree (NIP-52's date-based `end` is EXCLUSIVE)."""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CAL = os.path.join(ROOT, "static", "js", "client", "calendar.js")
NODE = shutil.which("node") or shutil.which("nodejs")


def _lift(names):
    src = open(CAL, encoding="utf-8").read()
    out = []
    for n in names:
        m = re.search(r"\n    (?:(?:async )?function %s|const %s = )" % (re.escape(n), re.escape(n)), src)
        assert m, "%s moved in calendar.js" % n
        start = m.start() + 1
        i = src.index("{", m.end() - 1)
        d = 0
        while i < len(src):
            if src[i] == "{": d += 1
            elif src[i] == "}":
                d -= 1
                if not d: break
            i += 1
        out.append(src[start:i + 1])
    return "\n".join(out)


@unittest.skipIf(not NODE, "no node on this node")
class ParseTests(unittest.TestCase):
    def _run(self, body):
        js = """
        const pad = n => String(n).padStart(2, '0');
        const ymd = d => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
        let _n52evs = null;
        %s
        %s
        """ % (_lift(["_n52parse", "_n52occ"]).replace("const NOSTR_CAL = '__nostr';", "")
               .replace("NOSTR_CAL", "'__nostr'"), body)
        js = "const NOSTR_CAL='__nostr';\n" + js
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout)

    def test_a_time_based_event_lands_on_its_day(self):
        got = self._run("""
          const e = _n52parse({ kind: 31923, pubkey: 'p', created_at: 5, content: 'roll up',
            tags: [['d','x'],['title','Meetup'],['start','1787032800'],['end','1787036400']] });
          _n52evs = [e];
          const occ = _n52occ(new Date(2026, 7, 1), new Date(2026, 8, 15));
          process.stdout.write(JSON.stringify({ title: e.title, n: occ.length,
            key: occ[0] && occ[0].key, allDay: occ[0] && occ[0].allDay }));
        """)
        self.assertEqual(got["title"], "Meetup")
        self.assertEqual(got["n"], 1)
        self.assertFalse(got["allDay"])

    def test_a_date_based_events_end_is_exclusive(self):
        """start 08-20, end 08-22 = the 20th and the 21st. Counting the 22nd is the off-by-one
        every calendar reader makes once."""
        got = self._run("""
          const e = _n52parse({ kind: 31922, pubkey: 'p', created_at: 5, content: '',
            tags: [['d','x'],['title','Conf'],['start','2026-08-20'],['end','2026-08-22']] });
          _n52evs = [e];
          const occ = _n52occ(new Date(2026, 7, 1), new Date(2026, 8, 1));
          process.stdout.write(JSON.stringify(occ.map(o => o.key)));
        """)
        self.assertEqual(got, ["2026-08-20", "2026-08-21"])

    def test_junk_is_refused_not_guessed(self):
        got = self._run("""
          const bad = [
            _n52parse({ kind: 31923, pubkey: 'p', tags: [['title','no d tag'],['start','123']] }),
            _n52parse({ kind: 31923, pubkey: 'p', tags: [['d','x'],['start','soon']] }),
            _n52parse({ kind: 31922, pubkey: 'p', tags: [['d','x'],['start','tomorrow']] }),
          ];
          process.stdout.write(JSON.stringify(bad));
        """)
        self.assertEqual(got, [None, None, None])


class WiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = open(CAL, encoding="utf-8").read()

    def test_a_network_event_is_never_editable(self):
        at = self.src.index("cal-edit")
        self.assertIn("e.cal === NOSTR_CAL ? ''", self.src, "the day panel offers Edit on a network event")
        self.assertIn("nostrDetails(el.dataset.uid)", self.src, "clicking one opens the editor instead of the card")

    def test_the_layer_never_blocks_the_personal_calendar(self):
        at = self.src.index("loadNostr()")
        seg = self.src[self.src.index("async function load()"):self.src.index("async function load()") + 800]
        self.assertNotIn("await loadNostr", seg, "the relay query gates the CalDAV load")

    def test_publishing_is_a_31923_with_the_nip52_tags(self):
        at = self.src.index("function publishNostrEvent()")
        body = self.src[at:at + 3000]
        self.assertIn("PC.publish(31923", body)
        for t in ("['title'", "['start'", "['end'", "['d'"):
            self.assertIn(t, body)

    def test_your_own_and_your_follows_events_are_asked_for_by_name(self):
        """Reported: "I see nostr events in the calendar, but not from my npub". A bare
        {kinds, limit:500} answers with the relay's 500 NEWEST calendar events, so a busy network
        crowds the user's own appointments out of their own calendar. loadNostr is RUN against a
        stub relay whose firehose answer does NOT include the user's event — it must still land,
        via the authors filter."""
        js = """
        let _n52evs = null, _n52at = 0;
        const S = { rev: 0 };
        const inView = () => false, paint = () => {};
        const PC = { me: () => ({ pubkey: 'me'.padEnd(64,'0') }) };
        const ME = PC.me().pubkey, FRIEND = 'f'.repeat(64);
        const mkEv = (pk, d) => ({ kind: 31923, pubkey: pk, created_at: 100,
          tags: [['d', d], ['title', 'ev-' + d], ['start', '1750000000']] });
        const asked = [];
        const Relay = {
          ready: async () => {},
          query: async (fils) => {
            asked.push(fils);
            const f = fils[0];
            if(f.kinds && f.kinds[0] === 3)
              return [{ kind: 3, created_at: 5, tags: [['p', FRIEND]] }];
            if(f.authors) return [mkEv(ME, 'mine'), mkEv(FRIEND, 'theirs')];
            return Array.from({ length: 3 }, (_, i) => mkEv('s' + i, 'noise' + i));
          },
        };
        %s
        (async () => {
          await loadNostr();
          const authorFil = asked.flat().find(f => f.authors && f.kinds && f.kinds.includes(31923));
          process.stdout.write(JSON.stringify({
            askedByName: !!authorFil,
            hasMe: !!(authorFil && authorFil.authors.includes(ME)),
            hasFriend: !!(authorFil && authorFil.authors.includes(FRIEND)),
            mineLanded: (_n52evs || []).some(e => e.pk === ME),
            friendLanded: (_n52evs || []).some(e => e.pk === FRIEND),
          }));
        })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % _lift(["_n52parse", "loadNostr"])
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        out = json.loads(r.stdout)
        self.assertTrue(out["askedByName"], "no authors filter was sent at all")
        self.assertTrue(out["hasMe"] and out["hasFriend"], out)
        self.assertTrue(out["mineLanded"], "the user's own event was left to the firehose")
        self.assertTrue(out["friendLanded"], out)

    def test_the_kinds_survive_the_relays_cleaners(self):
        store = open(os.path.join(ROOT, "app", "services", "nostr_relay", "store.py"),
                     encoding="utf-8").read()
        m = re.search(r"_PRUNABLE_KINDS\s*=\s*\(([^)]*)\)", store)
        self.assertIsNotNone(m)
        kinds = m.group(1)
        self.assertNotIn("31922", kinds, "a published event can be pruned before it happens")
        self.assertNotIn("31923", kinds, "a published event can be pruned before it happens")


if __name__ == "__main__":
    unittest.main()


class RelayIngestTests(unittest.TestCase):
    """The relay must PULL the calendar kinds, not merely accept them: a member's events are usually
    published from another calendar app to other relays, and without 3192x in the sync lists
    "Calendar isn't showing my nostr events" is structural — the events exist and never arrive."""

    def test_the_firehose_default_carries_the_calendar_kinds(self):
        src = open(os.path.join(ROOT, "app", "services", "nostr_relay", "thread.py"),
                   encoding="utf-8").read()
        m = re.search(r'nostr_relay_ingest_kinds", "([0-9,]+)"', src)
        self.assertIsNotNone(m)
        kinds = m.group(1).split(",")
        for k in ("31922", "31923", "31925"):
            self.assertIn(k, kinds, "kind %s is never synced in from upstream" % k)

    def test_the_member_backfill_carries_them_too(self):
        src = open(os.path.join(ROOT, "app", "services", "nostr_relay", "ingest.py"),
                   encoding="utf-8").read()
        m = re.search(r"kinds = kinds or \[([^\]]+)\]", src)
        self.assertIsNotNone(m)
        for k in ("31922", "31923"):
            self.assertIn(k, m.group(1), "a new member's existing events are never backfilled")
