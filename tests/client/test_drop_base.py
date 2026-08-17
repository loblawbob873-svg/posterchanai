"""Clearing a folder's agreement is verified by a read, never assumed.

"Stop syncing" exists almost entirely to clear this record, and the record is what decides whether
the next sweep sees a folder full of files or a folder full of deletions. The first version swallowed
its own failure: the delete threw, the card was removed, the agreement survived — so re-adding the
folder proposed moving every file to the trash, repeatedly, with nothing admitting the clear had not
happened. Reported as "I already went through that process a few times".

Structural, and labelled as such: driving a real IndexedDB needs a browser. What it pins down is the
shape that failure taught us — three strategies, each CHECKED by reading the value back, and a throw
rather than a silent return when the value survives all three.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNC = os.path.join(ROOT, "static", "js", "client", "sync.js")


def _fn(name):
    src = open(SYNC, encoding="utf-8").read()
    at = src.index("async function %s(" % name)
    i = src.index("{", at)
    depth = 0
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return src[at:i + 1]


def test_it_reads_the_value_back_rather_than_trusting_the_delete():
    body = _fn("_dropBase")
    assert "readonly" in body and "st.get(key)" in body, \
        "nothing reads the record back, so a failed delete still reports success"
    assert body.count("cleared()") >= 3, "the read-back is not applied after each attempt"


def test_it_has_a_fallback_when_the_delete_is_refused():
    body = _fn("_dropBase")
    assert "st.put({}, key)" in body, \
        "no overwrite fallback — a store that refuses deletes may still accept a put, and an empty " \
        "agreement is the same thing to the engine"
    assert "deleteDatabase" in body, "no last resort; the database holds nothing but agreements"


def test_it_throws_when_the_record_survives():
    body = _fn("_dropBase")
    assert re.search(r"throw err \|\|", body), \
        "it returns quietly when everything failed, which is the original bug"


def test_the_caller_keeps_the_folder_when_clearing_failed():
    """Removing the card while the agreement lives is what turns a failed clear into an endless loop
    of the most alarming dialog in the app."""
    src = open(SYNC, encoding="utf-8").read()
    at = src.index(".sync-forget")
    block = src[at:at + 1800]
    assert "could not clear" in block, "the failure is not reported to the user"
    assert "return;" in block.split("could not clear")[1][:200], \
        "it carries on and removes the folder anyway"


def test_adding_a_folder_clears_any_record_left_under_that_name():
    """THE ONE THAT MAKES IT SIMPLE FOR THE USER. The agreement is keyed on the NAME, so a record
    from a previous pairing outlives the folder being removed — and if Stop syncing failed to clear
    it, remove-and-re-add changed nothing, because the thing that had to change was the record
    neither step could be trusted to clear.

    A folder you have just added has agreed nothing by definition, so clearing here removes the whole
    class. It is also the safe direction: deletion requires an agreement, so a folder with none can
    only upload."""
    src = open(SYNC, encoding="utf-8").read()
    at = src.index("const add = document.getElementById('sync-add')")
    block = src[at:src.index("feed.querySelectorAll('.sync-card')", at)]
    assert "_dropBase(key)" in block, "adding a folder does not clear a stale agreement for its name"
    assert block.index("_dropBase(key)") < block.index("list2.push("), \
        "it is cleared AFTER the folder is added, so the first sweep can still see the old record"
    assert "was NOT added" in block, "a failed clear adds the folder anyway, which is the bug"
