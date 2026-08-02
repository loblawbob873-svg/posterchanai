"""Budget: a plan's line items appear in the Expenses list (static/js/client/budget.js).

Run: venv-unified/bin/python -m unittest tests.test_budget_plan_expenses

Plans were built as a separate tab whose TOTAL feeds Bills Due, so a plan's individual line items
were visible nowhere in Expenses — the reported complaint ("my Plan is not showing on the Expenses").
They are now mirrored into that list as DERIVED, display-only rows.

"Derived" is the entire safety property, and it is invisible in the UI:

  * summary() ALREADY counts every unsettled plan through catTotal() in `due`. The obvious "fix" —
    pushing plan items into _doc.bills so they render like any other expense — makes every plan count
    TWICE, in a tool whose only job is arithmetic. The wrong total is perfectly plausible on screen;
    nothing errors.
  * paid state belongs to the PLAN (settled(cat)), not the item, so a derived row must not carry the
    `paid` action — it would toggle a bill id that does not exist, or silently no-op, while the money
    stays due.

So these tests run the ACTUAL shipped source of summary()/catTotal()/settled()/planItemRows() in
node against a synthetic document, rather than asserting on the text of the file: the numbers are the
thing the user checks, and a grep-level test passes happily while the total is doubled.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUDGET_JS = os.path.join(REPO, "static", "js", "client", "budget.js")


def _src():
    with open(BUDGET_JS, encoding="utf-8") as fh:
        return fh.read()

# Pulled out of budget.js by name and evaluated as-is, so the test exercises shipped code. Named
# function declarations and `const NAME = ...` one-liners both appear here.
_WANT = ["catTotal", "settled", "summary", "visibleCats", "bySort", "planItemRow", "planItemRows"]


def _extract(src, name):
    """Return the source text of a top-level `function NAME(...)` or `const NAME = ...;` in budget.js.

    Both forms are two-space indented inside the module IIFE, which is what anchors the match.
    """
    m = re.search(r"^  (?:const|let) " + re.escape(name) + r"\s*=.*?;\s*$", src, re.M)
    if m:
        return m.group(0)
    m = re.search(r"^  function " + re.escape(name) + r"\(", src, re.M)
    if not m:
        raise AssertionError(
            f"{name}() not found in budget.js — it was renamed or removed; this test needs updating"
        )
    # Brace-match to the end of the function body.
    i = src.index("{", m.start())
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
        j += 1
    raise AssertionError(f"unbalanced braces reading {name}()")


def _run(doc, show_hidden=False):
    """Eval the extracted functions against `doc` and return {summary, expensesHtml}."""
    src = _src()
    harness = """
'use strict';
const _doc = %s;
const _showHidden = %s;
// Stubs for the module-level helpers the extracted functions close over.
const thisMonth = () => '2026-08';
const money = n => '$' + Math.abs(Number(n)||0).toLocaleString('en-US',{minimumFractionDigits:2, maximumFractionDigits:2});
const enc = s => (s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
%s
console.log(JSON.stringify({ summary: summary(), expenses: planItemRows() }));
""" % (json.dumps(doc), "true" if show_hidden else "false",
       "\n".join(_extract(src, n) for n in _WANT))
    out = subprocess.run([shutil.which("node") or "node", "-e", harness],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError(f"node failed: {out.stderr.strip()}")
    return json.loads(out.stdout)


def _doc_with_plan():
    """One $100 income, one $30 unsettled bill, and a plan of $25 + $15 that is NOT settled."""
    return {
        "bills": [
            {"id": "i1", "name": "Salary", "cost": 100, "paid": "N", "is_income": True,
             "is_recurring": True, "sort_order": 0, "hidden_month": ""},
            {"id": "b1", "name": "Internet", "cost": 30, "paid": "N", "is_income": False,
             "is_recurring": True, "sort_order": 1, "hidden_month": ""},
        ],
        "cats": [{"id": "c1", "name": "Visa card", "paid": "N", "hidden_month": "", "sort_order": 0}],
        "items": [
            {"id": "t1", "cat": "c1", "name": "Groceries", "amount": 25, "sort_order": 0},
            {"id": "t2", "cat": "c1", "name": "Gas", "amount": 15, "sort_order": 1},
        ],
    }


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class PlanItemsInExpenses(unittest.TestCase):
    def test_plan_items_are_listed(self):
        """The actual report: a plan's items must be visible in Expenses, by name and amount."""
        got = _run(_doc_with_plan())
        self.assertIn("Groceries", got["expenses"])
        self.assertIn("Gas", got["expenses"])
        self.assertIn("$25.00", got["expenses"])
        self.assertIn("$15.00", got["expenses"])

    def test_each_row_names_the_plan_it_came_from(self):
        """Without this the list reads as loose bills you cannot find or edit anywhere."""
        html = _run(_doc_with_plan())["expenses"]
        # Once per row as the visible, clickable label (the name also appears in title= attributes,
        # so match the element's TEXT rather than counting the name across the whole string).
        self.assertEqual(len(re.findall(r'class="bg-flag bg-planflag"[^>]*>Visa card</i>', html)), 2)

    def test_showing_them_does_not_change_the_totals(self):
        """THE regression guard. Rendering is derived, so every number must be identical to what the
        Plans-tab-only version produced: due = 30 (bill) + 40 (plan), NOT 30 + 40 + 40."""
        s = _run(_doc_with_plan())["summary"]
        self.assertEqual(s["income"], 100)
        self.assertEqual(s["due"], 70)
        self.assertEqual(s["paid"], 0)
        self.assertEqual(s["remaining"], 30)

    def test_a_paid_plan_moves_to_paid_exactly_once(self):
        doc = _doc_with_plan()
        doc["cats"][0]["paid"] = "Y"
        s = _run(doc)["summary"]
        self.assertEqual(s["due"], 30)
        self.assertEqual(s["paid"], 40)
        self.assertEqual(s["remaining"], 30)

    def test_the_check_slot_uses_a_glyph_that_actually_draws(self):
        """U+25AB WHITE SMALL SQUARE measured 6.7px of ink beside the 24px emoji in the same row: in
        a 38px slot at 45% opacity it reads as an EMPTY box, which is how it was reported. Use the
        glyphs the real bill rows already prove render here; the not-a-button signal is in the CSS."""
        html = _run(_doc_with_plan())["expenses"]
        self.assertIn("\u2b1c", html)
        for tiny in ("\u25ab", "\u25aa", "\u00b7", "\u2610"):
            self.assertNotIn(tiny, html)

    def test_derived_rows_carry_no_paid_checkbox(self):
        """Paid state lives on the plan. A `paid` action here would target a bill id that does not
        exist; the row echoes the plan's state in a non-interactive slot instead."""
        html = _run(_doc_with_plan())["expenses"]
        self.assertNotIn('data-act="paid"', html)
        self.assertNotIn('data-act="delitem"', html)
        self.assertIn("bg-checkless", html)
        # ...but it must still show WHICH state the plan is in, or the row looks perpetually unpaid.
        doc = _doc_with_plan(); doc["cats"][0]["paid"] = "Y"
        self.assertIn("\u2705", _run(doc)["expenses"])

    def test_rows_reach_the_plan_that_owns_them(self):
        """The row must carry both ids: itemMenu() reads closest('[data-cat]') for the plan and
        closest('[data-item]') for the line it acts on."""
        html = _run(_doc_with_plan())["expenses"]
        self.assertIn('data-cat="c1"', html)
        self.assertIn('data-item="t1"', html)
        self.assertIn('data-act="gotoplan"', html)

    def test_the_row_menu_acts_on_the_item_not_the_plan(self):
        """Wired to catmenu, pressing ☰ on a row showing ONE $25 grocery line opened a menu offering
        "Delete plan" — a destructive action on something the row does not represent, reachable by
        one tap from a list that otherwise deletes only what you pressed."""
        html = _run(_doc_with_plan())["expenses"]
        self.assertIn('data-act="itemmenu"', html)
        self.assertNotIn('data-act="catmenu"', html)

    def test_a_paid_plan_strikes_through_rather_than_vanishing(self):
        doc = _doc_with_plan()
        doc["cats"][0]["paid"] = "Y"
        html = _run(doc)["expenses"]
        self.assertIn("Groceries", html)
        self.assertIn("done", html)

    def test_a_skipped_plan_follows_the_show_hidden_toggle(self):
        """Hidden plans hide their items too, exactly as hidden bills hide themselves — otherwise
        "skip this month" leaves the items sitting in Expenses looking due."""
        doc = _doc_with_plan()
        doc["cats"][0]["hidden_month"] = "2026-08"
        self.assertNotIn("Groceries", _run(doc)["expenses"])
        self.assertIn("Groceries", _run(doc, show_hidden=True)["expenses"])

    def test_no_plans_renders_nothing(self):
        """The Expenses empty-state is `bills || plans` — a stray wrapper here would suppress it."""
        doc = _doc_with_plan()
        doc["cats"], doc["items"] = [], []
        self.assertEqual(_run(doc)["expenses"], "")

    def test_plan_names_are_escaped_in_the_title_attribute(self):
        """These rows interpolate the plan name into an ATTRIBUTE, which no other budget row does —
        an unescaped quote would break out of it."""
        doc = _doc_with_plan()
        doc["cats"][0]["name"] = 'Bob"s <plan>'
        html = _run(doc)["expenses"]
        self.assertNotIn('Bob"s', html)
        self.assertNotIn("<plan>", html)
        self.assertIn("&quot;", html)


class Wiring(unittest.TestCase):
    def test_itemmenu_has_a_handler_and_a_menu(self):
        """The row renders a ☰; without both the dispatch case and the function it is a dead button
        (the click resolves to [data-act] and falls off the end of the if-chain, silently)."""
        src = _src()
        self.assertIn("act==='itemmenu'", src)
        self.assertIn("function itemMenu(", src)

    def test_the_item_menu_can_edit_and_does_not_delete_the_plan(self):
        """Editing an item had no home anywhere before this — the plan card offers only ✕, so a
        mis-keyed amount could only be deleted and re-added, and that amount is what feeds Bills
        Due. The menu must also not offer delCat: it acts on one line, not the plan."""
        body = _extract(_src(), "itemMenu")
        self.assertIn("itemForm(cid, i)", body)
        self.assertNotIn("delCat", body)
        self.assertIn("_doc.items.filter", body)
        self.assertIn("ed?'Edit item':'Add item'", _extract(_src(), "itemForm"))

    def test_gotoplan_has_a_handler(self):
        """The row renders a clickable plan name; without a handler it is a dead affordance that
        silently does nothing (the click resolves to [data-act] and falls off the end of the chain)."""
        src = _src()
        self.assertIn("act==='gotoplan'", src)

    def test_plan_items_are_not_pushed_into_the_bills_array(self):
        """The double-count trap, guarded at the source: these rows are computed from _doc.items at
        render time. Anything that copies them into _doc.bills would also PERSIST them into the
        encrypted document, so the doubling would survive a reload and outlive this feature."""
        src = _src()
        body = _extract(src, "planItemRows") + _extract(src, "planItemRow")
        self.assertNotIn("_doc.bills", body)
        self.assertNotIn("save(", body)

    def test_the_expenses_list_renders_both_sources(self):
        src = _src()
        self.assertRegex(src, r"out\.map\(billRow\)\.join\(''\)\s*\+\s*planItemRows\(\)")


if __name__ == "__main__":
    unittest.main()
