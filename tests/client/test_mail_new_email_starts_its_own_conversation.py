"""A new email with a used subject starts its OWN conversation, in the list and when opened.

Reported on a real mailbox: today's "Payroll" (a new email, no reply headers) opened with every
"Payroll" and "Re: Payroll" back to November 2025 above it -- "emails from Nov 2025 should not be
shown". The server's /thread already answered with the one message; the browser grouped purely by
subject, painted that group first, and kept it when the server found nothing more. Walking newest ->
oldest, a message that is not a reply is where its conversation began, so nothing older joins it.
Runs the SHIPPED _key / _convKey / _isReplyish / _conversations under node.
"""
import json
import subprocess

from tests.client.test_the_mail_list_shows_one_row_per_conversation import _lift

DAY = 86400


def _lift_opt(name):
    try:
        return _lift(name) + ","
    except ValueError:          # an older mail.js without the helper: let the grouping itself be judged
        return ""


def rows(msgs, sent):
    program = """
      const Mail = { acct:'me@x', folder:'INBOX', %s, %s, %s %s, msgs:%s, convSent:%s };
      const rows = Mail._conversations();
      const opened = uid => { const seedK = Mail._key(Mail.msgs.find(m => m.uid === uid));
        const r = rows.find(r => r.all.some(x => Mail._key(x) === seedK));
        return r.all.concat(r.mine).map(m => m.uid).sort(); };
      process.stdout.write(JSON.stringify({
        rows: rows.map(r => ({ uids: r.all.map(m => m.uid), mine: r.mine.map(m => m.uid), count: r.count })),
        today: opened('today') }));
    """ % (_lift("_key"), _lift("_convKey"), _lift_opt("_isReplyish"), _lift("_conversations"),
           json.dumps(msgs), json.dumps(sent))
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-800:]
    return json.loads(done.stdout)


def m(uid, subject, ts, folder="INBOX"):
    return {"uid": uid, "subject": subject, "ts": ts, "read": True, "account": "me@x", "folder": folder}


NOW = 1790870000
INBOX = [                                                     # newest first, as the list holds them
    m("today", "Payroll", NOW),
    m("sep25", "Payroll", NOW - 6 * DAY),
    m("nov-re", "Re: Payroll", NOW - 330 * DAY),
    m("nov", "Payroll", NOW - 335 * DAY),
]
SENT = [m("my-reply", "Re: Payroll", NOW + 600, "Sent"), m("old-reply", "Re: Payroll", NOW - 332 * DAY, "Sent")]


def test_each_new_payroll_email_is_its_own_conversation():
    got = rows(INBOX, SENT)
    assert [r["uids"] for r in got["rows"]] == [["today"], ["sep25"], ["nov-re", "nov"]], got["rows"]


def test_a_reply_joins_the_conversation_it_answers_not_2025s():
    got = rows(INBOX, SENT)
    by_first = {r["uids"][0]: r for r in got["rows"]}
    assert by_first["today"]["mine"] == ["my-reply"], got["rows"]
    assert by_first["nov-re"]["mine"] == ["old-reply"], got["rows"]
    assert by_first["sep25"]["mine"] == [], got["rows"]


def test_opening_todays_email_shows_no_2025_mail():
    got = rows(INBOX, SENT)
    assert got["today"] == ["my-reply", "today"], got["today"]
