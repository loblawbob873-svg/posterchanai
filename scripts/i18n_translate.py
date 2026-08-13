#!/usr/bin/env python3
"""Fill a translation catalogue using THIS node's own LLM.

    venv-unified/bin/python scripts/i18n_translate.py ar
    venv-unified/bin/python scripts/i18n_translate.py ja --model Qwen3.5-9B-abliterated-Q4_K_M.gguf

Reads `static/i18n/en.json` (see i18n_extract.py) and writes `static/i18n/<lang>.json`.

WHY THE LOCAL MODEL. 3,500 strings x 2 languages is not a hand job, and it is not worth a paid API
either when this box already serves an OpenAI-compatible endpoint. The catalogue is REGENERABLE
(that is the whole point of the extractor), so a string translated badly is a re-run, not a rewrite.

RESUMABLE BY CONSTRUCTION. The output is written after every batch and existing translations are
re-read on start, so a run that dies at string 2,000 — or a node that reboots, or somebody wanting
to stop and look — costs one batch, not the run. `--redo` is the way to deliberately discard.

WHAT IS DELIBERATELY NOT TRANSLATED. Technical tokens (`npub1…`, a hostname, a path, a CLI command)
mean the same thing in every language and translating them makes an instruction WRONG rather than
foreign. They are passed through verbatim by `is_technical`, before the model ever sees them.

WHAT IS VALIDATED, because a model will cheerfully break all three: HTML entities must survive
(`&amp;`, `&#10;` — a mangled entity renders as literal text in the middle of a sentence), the
string must not come back wrapped in quotes the source did not have, and a batch must return every
key it was given or the missing ones go back in the queue. A batch that fails validation is retried
once and then left in English, which is the graceful failure this whole design is built around.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
I18N = ROOT / "static" / "i18n"
API = "http://127.0.0.1:3051/v1/chat/completions"
DEFAULT_MODEL = "Qwen3.5-9B-abliterated-Q4_K_M.gguf"

LANG_NAMES = {"ar": "Arabic", "ja": "Japanese"}

ENTITY = re.compile(r"&(?:[a-zA-Z]+|#\d+);")
# A string that is only an identifier, a host, a path, a key prefix or a command.
TECHNICAL = re.compile(
    r"""(?x)
    ^\s*(?:
        n(?:pub|sec|ote|profile|event|addr)1[0-9a-z]+
      | [a-z0-9.-]+\.(?:place|com|net|org|lan|local|example|io|dev)(?::\d+)?(?:/\S*)?
      | (?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?:/\S*)?
      | [./~][\w./-]+
      | (?:scripts|venv|app|static|docs|mobile|desktop)/\S+
      | \w+\.(?:sh|py|js|json|html|css|md|service|gguf|toml|yml|yaml)
      | wss?://\S+
    )\s*$
    """,
    re.I,
)

SYSTEM = (
    "You are a professional software localiser. You translate user-interface strings for a "
    "self-hosted social/chat application.\n"
    "RULES, all mandatory:\n"
    "1. Reply with ONE JSON object and nothing else. No prose, no code fence.\n"
    "2. Every key of the input object must appear in your output, spelled EXACTLY as given.\n"
    "3. Values are the translation of the key into {lang}.\n"
    "4. Keep HTML entities (&amp; &#10; &lt;) byte-for-byte. Keep leading/trailing spaces, "
    "punctuation, ellipses, emoji and any … or — exactly where they are.\n"
    "5. Do NOT translate: proper nouns, protocol words (Nostr, npub, relay, Blossom, NIP-05), "
    "hostnames, file paths, CLI commands, or anything inside backticks.\n"
    "6. These are BUTTONS and LABELS. Keep them short — a translation twice the length of the "
    "English breaks the layout. Prefer the wording a native speaker would meet in an app.\n"
    "7. If a string is a sentence fragment, translate it as a fragment; do not complete it.\n"
    "8. Answer ONLY in {lang}. Some strings NAME a language ('Vietnamese', 'Arabic', 'Japanese') "
    "because this app has a language menu — translate the NAME into {lang}. Never switch your "
    "output language to match a language mentioned in the text."
)


def is_technical(s: str) -> bool:
    return bool(TECHNICAL.match(s))


def chat(model: str, messages: list[dict], timeout: int = 300) -> str:
    body = json.dumps(
        {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": 4096}
    ).encode()
    req = urllib.request.Request(
        API, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read())
    return j["choices"][0]["message"]["content"]


def parse_obj(text: str) -> dict:
    """The model's answer, with the two wrappers it reaches for stripped."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("no JSON object in the reply")
    return json.loads(t[i : j + 1])


# The script each language is actually written in. A translation that carries Latin WORDS and none
# of its target script is either untranslated or in the wrong language entirely.
SCRIPT = {
    "ar": re.compile(r"[؀-ۿݐ-ݿ]"),
    "ja": re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]"),
}
LATIN_WORD = re.compile(r"[A-Za-zÀ-ỹ]{2,}")


def acceptable(src: str, dst: str, lang: str = "") -> bool:
    """…and the check that a translation is in the LANGUAGE THAT WAS ASKED FOR.

    Measured, not anticipated: the Japanese catalogue came back with a run of Vietnamese in it —
    "View on GitHub" as "Xem trên GitHub", "VRAM Mode" as "Chế độ VRAM". The cause is visible in the
    alphabetical ordering of the batches: this app has a language list, so one batch contained the
    string "Vietnamese", and the model took it as an instruction and switched output language for
    everything after it. 37 of 3,469 entries, every one of them fluent, confident and wrong — and
    invisible to an acceptance-rate check, which counted them as successes.

    Rejecting is the right response rather than repairing: an unaccepted string stays in the todo
    list and is retried on the next run, and if it never lands it falls back to English, which is the
    graceful failure this whole design rests on.
    """
    rx = SCRIPT.get(lang)
    if rx and dst.strip() != src.strip() and LATIN_WORD.search(dst) and not rx.search(dst):
        return False
    return _shape_ok(src, dst)


def _shape_ok(src: str, dst: str) -> bool:
    if not isinstance(dst, str) or not dst.strip():
        return False
    # Entities must survive exactly — a mangled one renders as literal text mid-sentence.
    if sorted(ENTITY.findall(src)) != sorted(ENTITY.findall(dst)):
        return False
    # A model that "helpfully" quotes its answer.
    if dst.startswith('"') and dst.endswith('"') and not src.startswith('"'):
        return False
    # Runaway length is a paragraph where a button belongs.
    if len(dst) > max(80, len(src) * 4):
        return False
    return True


"""Entity masking, kept as a BELT-AND-BRACES path rather than the load-bearing one.

The real fix is upstream: the extractor decodes entities, because the DOM layer matches against a
text node the browser has already parsed — `Save &amp; reload` in source is `Save & reload` on
screen, and a catalogue keyed on the raw form is a translation that is present, correct and silently
never applied. So a catalogue string should no longer carry an entity at all, and mask() is a no-op
on every one of them today. It stays because a hand-edited catalogue entry can still carry one, and
because a model translates `&amp;` into the local word for "and" about as often as it leaves it
alone. ⟦n⟧ was chosen for surviving tokenisation and meaning nothing worth translating — though note
it does NOT always survive: Japanese answers dropped the brackets and kept the digit, which is
exactly why this is the fallback and the decode is the fix."""
MASK = "⟦{}⟧"
MASK_RX = re.compile(r"⟦(\d+)⟧")


def mask(s: str) -> tuple[str, list[str]]:
    found: list[str] = []

    def sub(m: re.Match) -> str:
        found.append(m.group(0))
        return MASK.format(len(found) - 1)

    return ENTITY.sub(sub, s), found


def unmask(s: str, found: list[str]) -> str:
    def sub(m: re.Match) -> str:
        i = int(m.group(1))
        return found[i] if i < len(found) else m.group(0)

    return MASK_RX.sub(sub, s)


def translate_batch(model: str, lang: str, batch: list[str]) -> dict:
    """NUMBERED, not keyed by the English string.

    The first version sent `{"Save": "", "Bookmarks": ""}` and asked for the values to be filled in.
    A model hands that straight back — the reply was the request verbatim, empty strings and all, so
    three batches in five contributed NOTHING while the run reported itself as working. Numbering
    makes the answer structurally different from the question, which is the only reliable way to
    tell "it translated" from "it echoed".

    It fixes two lesser things for free: a key is now `"7"` rather than a sentence containing quotes,
    apostrophes and newlines (the `Expecting ':' delimiter` parse failures), and a reply that drops
    or reorders entries is still matched correctly instead of being silently discarded for not
    matching a key byte-for-byte.
    """
    masked: dict[str, tuple[str, list[str]]] = {}
    payload: dict[str, str] = {}
    for i, s in enumerate(batch):
        m, found = mask(s)
        masked[str(i)] = (s, found)
        payload[str(i)] = m
    msgs = [
        {"role": "system", "content": SYSTEM.format(lang=LANG_NAMES[lang])},
        {
            "role": "user",
            "content": "Translate each value. Reply with the same keys and the translations "
            "as values.\n" + json.dumps(payload, ensure_ascii=False),
        },
    ]
    try:
        obj = parse_obj(chat(model, msgs))
    except (urllib.error.URLError, ValueError, KeyError, json.JSONDecodeError, TimeoutError) as e:
        print(f"    batch failed ({type(e).__name__}: {e}) — retrying once", flush=True)
        try:
            obj = parse_obj(chat(model, msgs))
        except Exception as e2:  # noqa: BLE001 - a dead batch must not kill the run
            print(f"    batch failed again ({e2}) — left in English", flush=True)
            return {}
    out = {}
    echoed = 0
    for idx, val in obj.items():
        if str(idx) not in masked or not isinstance(val, str):
            continue
        english, found = masked[str(idx)]
        restored = unmask(val, found)
        if restored.strip() == english.strip():
            # Legitimate for a proper noun ("Nostr"), so it is counted rather than rejected — an
            # ECHOED BATCH is the failure, not an untranslated string.
            echoed += 1
        if acceptable(english, restored, lang):
            out[english] = restored
    if out and echoed == len(out) and len(out) > 2:
        print(f"    batch came back identical to the input ({echoed}) — treating as an echo", flush=True)
        return {}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("lang", choices=sorted(LANG_NAMES))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--batch", type=int, default=25, help="strings per request")
    ap.add_argument("--redo", action="store_true", help="discard existing translations")
    ap.add_argument("--verify", action="store_true",
                    help="re-check the existing catalogue and DROP entries that fail validation, "
                         "so a later run retranslates them. Use after tightening a rule.")
    args = ap.parse_args()

    src_path = I18N / "en.json"
    if not src_path.exists():
        print("run scripts/i18n_extract.py first", file=sys.stderr)
        return 2
    english = list(json.load(src_path.open()))

    out_path = I18N / f"{args.lang}.json"
    have: dict[str, str] = {}
    if out_path.exists() and not args.redo:
        try:
            have = json.load(out_path.open())
        except json.JSONDecodeError:
            have = {}

    if args.verify:
        # A validation rule is only worth tightening if it can be applied to what was already
        # written — otherwise the bad entries from before the rule stay for ever, and they are
        # exactly the ones nobody will notice, because they are fluent.
        bad = {k: v for k, v in have.items() if k in english and not acceptable(k, v, args.lang)}
        for k in bad:
            del have[k]
        out_path.write_text(json.dumps(have, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        print(f"{args.lang}: dropped {len(bad)} entries that fail the current rules")
        for k, v in list(bad.items())[:10]:
            print(f"    {k[:40]!r:44} -> {str(v)[:40]!r}")

    todo = [s for s in english if s not in have]
    # Technical tokens are answered here rather than by the model — see the header.
    passthrough = [s for s in todo if is_technical(s)]
    for s in passthrough:
        have[s] = s
    todo = [s for s in todo if not is_technical(s)]

    print(
        f"{args.lang}: {len(english)} strings, {len(have) - len(passthrough)} already done, "
        f"{len(passthrough)} passed through as technical, {len(todo)} to translate",
        flush=True,
    )

    started = time.time()
    for i in range(0, len(todo), args.batch):
        batch = todo[i : i + args.batch]
        got = translate_batch(args.model, args.lang, batch)
        have.update(got)
        out_path.write_text(json.dumps(have, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        done = min(i + args.batch, len(todo))
        rate = done / max(1e-9, time.time() - started)
        left = (len(todo) - done) / rate if rate else 0
        print(
            f"  {done}/{len(todo)}  (+{len(got)}/{len(batch)} accepted)  ~{left/60:.0f} min left",
            flush=True,
        )

    missing = [s for s in english if s not in have]
    print(f"done: {len(have)}/{len(english)} translated, {len(missing)} left in English", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
