"""What a block bot may say, shared by the Nostr and the Pleroma block bots so they cannot drift.

The HEADLINE -- who blocked whom -- is always written by code, from the database, and never by the
model: that is the only way the names in a post are the right people. The model may add a line or
three of commentary underneath, and that commentary is thrown away if it names anybody at all. A
model that is shown the names "helps" by repeating them, and one that repeats them invents: '@@liminal',
'@npub1', a whole post of "BLOCKER: @x blocked @y" four times over, and -- on detroitriotcity -- "The
vampire brigade has been systematically silenced across the dark web. Starprophet and Judge Dread were
at the center of this operation", which the old Pleroma path posted as fact because its only check was
that the first two handles appeared somewhere.
"""
import logging
import re

COMMENTARY_ONLY = (" IMPORTANT: the post already begins with the line(s) above saying who did what; they "
                   "are added separately. Write ONLY the commentary that goes underneath them, at most "
                   "three sentences. Do NOT write any usernames, @handles, npubs, nostr: links or "
                   "domains, and do not repeat the line(s) above.")
NAMEISH = re.compile(r"@[\w.-]|\bnpub1|\bnprofile1|nostr:|https?://|\b[\w-]+\.(?:com|org|net|social|place|io|dev|club|moe|town|st)\b", re.I)
_CJK = re.compile(r"[一-鿿぀-ゟ゠-ヿ가-힯]")


def commentary(ai_msg: str, mutes: bool = False, names=()) -> str:
    """The model's text if it is usable as commentary under the headline, else "".

    Unusable: it names anyone -- an @handle, an npub, a domain, OR the bare name of anybody in this
    batch ("Starprophet") -- repeats a headline, calls a mute a block, or drifts into another script.
    A name can only be wrong there: the right ones are already above."""
    text = re.sub(r"\b(?:BLOCKER|BLOCKEE|MUTER|MUTEE):\s*", "", (ai_msg or "")).strip()
    if not text or text == "None":
        return ""
    if NAMEISH.search(text):
        logging.warning("AI block commentary named someone; posting the headline alone")
        return ""
    low = text.lower()
    for n in names or ():
        n = str(n or "").strip().lstrip("@").split("@")[0].lower()
        if len(n) >= 3 and re.search(r"(?<![\w])" + re.escape(n) + r"(?![\w])", low):
            logging.warning("AI block commentary named someone in the batch; posting the headline alone")
            return ""
    if re.search(r"\bblocked\b.*\bblocked\b", low):
        return ""                                     # it is writing headlines, not commentary
    if mutes and re.search(r"\bblock(?:ed|s)?\b", text, re.I) and not re.search(r"\bmute", text, re.I):
        logging.warning("AI block commentary called a mute a block; posting the headline alone")
        return ""
    if _CJK.search(text):
        return ""
    return text
