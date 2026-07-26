"""The `bill` command — snap a photo of a bill, read it, set a reminder.

Everything else that used to live here (budget/bills/pay/addbill) is GONE. The budget is no longer
an external Flask app reached with a per-user API key; it's a Nostr event encrypted to the user's
OWN key, held in the client (static/js/client/budget.js, Discover -> Budget). The server has no key
that can read or write it, deliberately — so a server-side `pay` is not something we can offer any
more, on Telegram or anywhere else.

What survives here is the half only the server can do: OCR + the extraction call. The parse comes
back to the caller, the web client files it into the encrypted doc with one tap, and `bill add`
sets the reminder (reminders ARE server data).
"""
import logging

logger = logging.getLogger(__name__)


class _FinanceMixin:
    # ------------------------------------------------------------------ bill (snap a bill) ---
    # A photo of a bill → OCR → one small extraction call → a reminder before it's due, and (in the
    # web client) a one-tap file into your encrypted Budget. Three systems you already have, joined
    # by ~100 tokens of LLM.
    #
    # It PREVIEWS first and only acts on `bill add`. A mis-read amount is worth one confirmation tap
    # either way, and the preview is what lets the web client offer "Add to budget" — the parse is
    # held in-process (same pattern as the Telegram media/caption flows) so confirming doesn't mean
    # re-uploading the photo.
    _BILL_PENDING: dict = {}          # user_id -> {vendor, amount, due, ts}
    _BILL_PENDING_TTL = 900           # 15 min

    async def _bill_command(self, arg: str, attachments=None) -> dict:
        import json as _json
        import re as _re
        import time as _time
        from datetime import datetime, timedelta

        if self.user is None:
            return {"type": "text", "content": "Sign in to use bills."}
        low = (arg or "").strip().lower()

        # ---- confirm step: set the reminder. The BUDGET entry is written by the client, not here:
        # the budget doc is encrypted to the user's own key and the server holds nothing that can
        # open it. The web client's "Add to budget" button writes it (PCBudget.addParsed) and then
        # runs this, so one tap still does both. ----
        if low in ("add", "yes", "confirm", "ok"):
            pend = self._BILL_PENDING.get(self.user.id)
            if not pend or (_time.time() - pend.get("ts", 0)) > self._BILL_PENDING_TTL:
                self._BILL_PENDING.pop(self.user.id, None)
                return {"type": "text", "content": "Nothing to add — send a photo of a bill with `bill` first."}
            from app.services import reminder_service
            self._BILL_PENDING.pop(self.user.id, None)
            out = [f"📄 {pend['vendor']} — {pend['amount']:.2f}"]
            # Remind two days out, or first thing tomorrow when it's due sooner than that. A reminder
            # dated in the past would just fire immediately, which is noise, not help.
            due = pend.get("due")
            if due:
                try:
                    d = datetime.strptime(due, "%Y-%m-%d")
                    when = d - timedelta(days=2)
                    now = datetime.utcnow()
                    if when <= now:
                        when = now + timedelta(hours=12)
                    if d >= now - timedelta(days=1):
                        reminder_service.create_reminder(
                            self.db, self.user, f"Pay {pend['vendor']} — {pend['amount']:.2f} due {due}", when)
                        out.append(f"⏰ Reminder set for {when.strftime('%Y-%m-%d %H:%M')} (due {due})")
                    else:
                        out.append(f"📅 Due {due} — already past, no reminder set.")
                except ValueError:
                    pass
            out.append("Add it to your books in Discover → Budget (it's encrypted to your key, so only your client can write it).")
            return {"type": "text", "content": "\n".join(out)}

        # ---- read step: OCR the attachment, extract the fields, preview ----
        from app.services.media_service import is_image, is_pdf
        if not attachments or not any(is_image(fn, ct) or is_pdf(fn, ct) for fn, _d, ct in attachments):
            return {"type": "text", "content": "Attach a photo or PDF of a bill, then send `bill`."}
        import base64 as _b64
        import io as _io
        from app.services.document_service import extract_image_text, extract_pdf_text

        def _sharpen(raw: bytes) -> str:
            """Grayscale + 2x upscale before OCR, as base64 PNG.

            Tesseract at phone-photo resolution loses exactly the characters that matter on a bill.
            On the test bill it read 'AMOUNTDUE $14230' and 'August, 2028' — a lost decimal point
            turns $142.30 into $14,230, and a wrong year moves the due date two years out. The same
            image upscaled reads '$142.30' and 'August 1, 2026'. Done here rather than in
            extract_image_text so the shared `ocr` command's behaviour is untouched.
            EXIF is applied by hand because converting to grayscale drops it, and phone photos of
            bills are very often rotated."""
            from PIL import Image, ImageOps
            im = ImageOps.exif_transpose(Image.open(_io.BytesIO(raw)))
            im = ImageOps.grayscale(im)
            if max(im.size) < 2000:                       # don't blow up an already-large scan
                im = im.resize((im.width * 2, im.height * 2), Image.LANCZOS)
            buf = _io.BytesIO()
            im.save(buf, format="PNG")
            return _b64.b64encode(buf.getvalue()).decode()

        parts = []
        for fn, data, ct in attachments:
            try:
                b64 = _b64.b64encode(data).decode() if isinstance(data, (bytes, bytearray)) else data
            except Exception:
                continue
            if is_pdf(fn, ct):
                parts.append(extract_pdf_text(b64) or "")
            elif is_image(fn, ct):
                try:
                    b64 = _sharpen(data) if isinstance(data, (bytes, bytearray)) else b64
                except Exception as e:
                    logger.debug("[bill] preprocess failed, using the original: %s", e)
                parts.append(extract_image_text(b64) or "")
        text = "\n".join(p for p in parts if p and p.strip()).strip()
        if not text:
            return {"type": "text", "content": "Couldn't read any text off that — try a sharper, straight-on photo."}
        text = text[:4000]

        today = datetime.utcnow().strftime("%Y-%m-%d")
        try:
            raw = await self.chat_service.chat([
                {"role": "system", "content": (
                    "You extract bill details from OCR text. Reply with ONE line of JSON and nothing "
                    'else: {"vendor": str, "amount": number, "due_date": "YYYY-MM-DD"}. '
                    "vendor is the company being paid, short (2-4 words). amount is the TOTAL DUE as a "
                    "plain number, no currency symbol or thousands separators. due_date is when payment "
                    f"is due; today is {today}, so a date with no year is the next such date in the "
                    "future. Use null for anything not clearly stated. Never guess an amount.")},
                {"role": "user", "content": text},
            ]) or ""
        except Exception as e:
            return {"type": "text", "content": f"Couldn't read the bill: {e}"}

        m = _re.search(r"\{.*\}", raw, _re.S)
        data = {}
        if m:
            try:
                data = _json.loads(m.group(0))
            except Exception:
                data = {}
        vendor = str(data.get("vendor") or "").strip()[:60]
        due = str(data.get("due_date") or "").strip()
        if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
            due = ""
        amount = None
        try:
            if data.get("amount") is not None:
                amount = round(float(str(data["amount"]).replace(",", "").replace("$", "")), 2)
        except (TypeError, ValueError):
            amount = None

        # No amount = nothing worth writing to your books. Show what WAS read rather than inventing a
        # number, and hand back the manual command.
        if amount is None or amount <= 0 or not vendor:
            got = ", ".join(x for x in [f"vendor “{vendor}”" if vendor else "", f"due {due}" if due else ""] if x)
            return {"type": "text", "content":
                    "📄 I read the text but couldn't pin down the vendor and total"
                    + (f" (got {got})" if got else "")
                    + ".\nAdd it by hand in Discover → Budget."}

        self._BILL_PENDING[self.user.id] = {"vendor": vendor, "amount": amount, "due": due, "ts": _time.time()}
        lines = ["📄 **Bill read**", f"• Vendor: {vendor}", f"• Amount: {amount:.2f}"]
        lines.append(f"• Due: {due}" if due else "• Due: not found")
        # `bill` result type: the web client renders an "Add to budget" button from this instead of
        # asking the user to type `bill add`. The staged parse still has to be CONFIRMED — the finance
        # API has no delete — but confirming should be a tap, not a second typed command.
        return {"type": "bill", "content": "\n".join(lines),
                "vendor": vendor, "amount": amount, "due": due}
