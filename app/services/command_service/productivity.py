"""Auto-split from the original command_service.py monolith (mixin pattern). No behavior change."""
import logging

from ._common import Optional

logger = logging.getLogger(__name__)


class _ProductivityMixin:
    async def _todo_command(self, arg: str) -> dict:
        """Todo command - DISABLED (CalDAV removed)"""
        return {"type": "text", "content": "⚠️ The todo feature is temporarily unavailable."}

    async def _remind_command(self, arg: str, attachments=None) -> dict:
        """Set/cancel a reminder. `remind <what> <when>` parses natural language via the LLM and
        stores it; `remind list` shows them; `remind cancel <id>` cancels one. Delivered later by
        the reminder scheduler to the web UI (always) and Telegram (if configured).

        With an IMAGE attached and no text, it reads the picture instead: screenshot a ticket, an
        appointment confirmation or a "see you Thursday 3pm" message and it extracts what and when.
        Same shape as `bill` — OCR plus one small extraction call joining two things that already
        exist, rather than any new machinery."""
        from app.services import reminder_service

        if self.user is None:
            return {"type": "text", "content": "Sign in to set reminders."}
        arg = (arg or "").strip()
        low = arg.lower()

        if not arg and attachments:
            return await self._remind_from_image(attachments)
        if not arg or low == "list":
            return await self._reminders_command()
        if low.startswith("cancel"):
            rest = arg[len("cancel"):].strip()
            if rest.isdigit():
                ok = reminder_service.cancel_reminder(self.db, self.user, int(rest))
                return {"type": "text", "content": ("🗑️ Reminder cancelled." if ok
                                                    else "No matching pending reminder for that id.")}
            return {"type": "text", "content": "Usage: `remind cancel <id>` — see ids with `reminders`."}

        tz = reminder_service.get_user_tzinfo(self.db, self.user.id)
        parsed = await reminder_service.parse_reminder(arg, self.chat_service, tz=tz)
        if not parsed.get("ok"):
            return {"type": "text", "content": parsed.get("error", "Couldn't set that reminder.")}
        r = reminder_service.create_reminder(self.db, self.user, parsed["text"], parsed["due_at"])
        human = reminder_service.humanize_due(r.due_at, tz=tz)
        return {"type": "text", "content": (
            f"⏰ Reminder set: **{r.text}** — {human}.\n_id {r.id} · `reminders` to view or cancel._")}

    async def _remind_from_image(self, attachments) -> dict:
        """Screenshot → reminder. OCR, one extraction call, done."""
        import base64 as _b64
        import re as _re
        import json as _json
        from datetime import datetime, timedelta
        from app.services import reminder_service
        from app.services.document_service import extract_image_text, extract_pdf_text
        from app.services.media_service import is_image, is_pdf

        parts = []
        for fn, data, ct in attachments:
            try:
                b64 = _b64.b64encode(data).decode() if isinstance(data, (bytes, bytearray)) else data
            except Exception:
                continue
            if is_pdf(fn, ct):
                parts.append(extract_pdf_text(b64) or "")
            elif is_image(fn, ct):
                parts.append(extract_image_text(b64) or "")
        text = "\n".join(p for p in parts if p and p.strip()).strip()[:3000]
        if not text:
            return {"type": "text", "content": "Couldn't read any text in that image."}

        tz = reminder_service.get_user_tzinfo(self.db, self.user.id)
        now = datetime.now(tz)
        try:
            raw = await self.chat_service.chat([
                {"role": "system", "content": (
                    "You pull a single reminder out of text taken off someone's screenshot. Reply with "
                    'ONE line of JSON and nothing else: {"what": str, "when": "YYYY-MM-DD HH:MM"}. '
                    "`what` is a SHORT description of the thing to be reminded about (3-8 words, no "
                    "date in it). `when` is when they should be reminded, in 24-hour local time. The "
                    f"user's local time right now is {now.strftime('%Y-%m-%d %H:%M')} ({now.strftime('%A')}), "
                    "so resolve weekdays and relative dates against that and never pick a time in the "
                    "past. If the text names no time or event at all, use null for both.")},
                {"role": "user", "content": text},
            ]) or ""
        except Exception as e:
            return {"type": "text", "content": f"Couldn't read that: {e}"}

        m = _re.search(r"\{.*\}", raw, _re.S)
        data = {}
        if m:
            try:
                data = _json.loads(m.group(0))
            except Exception:
                data = {}
        what = str(data.get("what") or "").strip()[:120]
        when = str(data.get("when") or "").strip()
        try:
            due = datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        except ValueError:
            due = None
        if not what or not due:
            # Say what was read rather than inventing a time — a reminder that fires at the wrong
            # moment is worse than one that was never set.
            return {"type": "text", "content":
                    "⏰ I read the image but couldn't find a clear event and time in it.\n"
                    "Try `remind <what> <when>` — e.g. `remind dentist thursday 3pm`."}
        if due <= now:
            return {"type": "text", "content":
                    f"⏰ That looks like it already passed ({due.strftime('%Y-%m-%d %H:%M')}) — "
                    "nothing set. Use `remind <what> <when>` if you meant a different time."}

        # Store NAIVE UTC — that's what parse_reminder produces and what the scheduler compares
        # against. `.astimezone()` with no argument converts to the SERVER's local zone instead, which
        # silently shifted a 3:00 PM appointment by the host's UTC offset.
        from datetime import timezone as _tzutc
        due_utc = due.astimezone(_tzutc.utc).replace(tzinfo=None)
        r = reminder_service.create_reminder(self.db, self.user, what, due_utc)
        human = reminder_service.humanize_due(r.due_at, tz=tz)
        return {"type": "text", "content": (
            f"⏰ Reminder set from the image: **{r.text}** — {human}.\n"
            f"_id {r.id} · `reminders` to view or cancel._")}

    async def _reminders_command(self) -> dict:
        """List pending reminders. Returns a `reminders` result the web UI renders with a Cancel
        button per item (Telegram builds an inline keyboard from the same list)."""
        from app.services import reminder_service

        if self.user is None:
            return {"type": "text", "content": "Sign in to view reminders."}
        items = reminder_service.list_reminders(self.db, self.user)
        if not items:
            return {"type": "text", "content": (
                "You have no pending reminders. Set one with `remind <what> <when>` — "
                "e.g. `remind open the oven in 10m`.")}
        tz = reminder_service.get_user_tzinfo(self.db, self.user.id)
        payload = [{
            "id": r.id, "text": r.text,
            "due_at": r.due_at.isoformat(),
            "human": reminder_service.humanize_due(r.due_at, tz=tz),
        } for r in items]
        lines = "\n".join(f"• **{r.text}** — {reminder_service.humanize_due(r.due_at, tz=tz)} _(id {r.id})_"
                          for r in items)
        return {
            "type": "reminders",
            "content": f"⏰ **Your reminders** ({len(items)})\n{lines}",
            "reminders": payload,
        }

    async def _pin_command(self, arg: str) -> dict:
        """Pin something you run often — a search (`pin ai news`) or any command
        (`pin screenshot https://google.com`). `pin delete <id>` removes one; `pin` with no
        arg shows the list. Running a pin re-runs it (bare text → search; a command word →
        that command verbatim)."""
        from app.services import saved_search_service

        if self.user is None:
            return {"type": "text", "content": "Sign in to save pins."}
        arg = (arg or "").strip()
        low = arg.lower()
        if not arg or low == "list":
            return await self._pins_command()
        if low.startswith("delete") or low.startswith("remove"):
            rest = arg.split(maxsplit=1)
            rid = rest[1].strip() if len(rest) > 1 else ""
            if rid.isdigit():
                ok = saved_search_service.delete_saved_search(self.db, self.user, int(rid))
                return {"type": "text", "content": ("🗑️ Pin deleted." if ok
                                                    else "No matching pin.")}
            return {"type": "text", "content": "Usage: `pin delete <id>` — see ids with `pins`."}

        s = saved_search_service.create_saved_search(self.db, self.user, arg)
        if not s:
            return {"type": "text", "content": (
                "Give me something to pin, e.g. `pin latest xrp news` or "
                "`pin screenshot https://google.com`.")}
        cmd, _ = self.parse_command(s.query)
        kind = "command" if cmd else "search"
        return {"type": "text", "content": (
            f"📌 Pinned {kind}: {s.query}\nSend 'pins' to run or delete it.")}

    async def _pins_command(self) -> dict:
        """List pins. Returns a `saved_searches` result the web UI renders with Run + Delete
        buttons per item (Telegram builds an inline keyboard from the same list). Each item
        carries a `run` string — the resolved command to execute (a bare query becomes
        `search <query>`, a command word is kept verbatim) — so the web Run button re-runs the
        right thing instead of always searching."""
        from app.services import saved_search_service

        if self.user is None:
            return {"type": "text", "content": "Sign in to view your pins."}
        items = saved_search_service.list_saved_searches(self.db, self.user)
        if not items:
            return {"type": "text", "content": (
                "You have no pins yet. Save one with `pin <query>` or `pin <command>` — "
                "e.g. `pin ai news` or `pin screenshot https://google.com`.")}
        payload = []
        for s in items:
            cmd, c_arg = self.parse_command(s.query)
            run = (f"{cmd} {c_arg}".strip() if cmd else f"search {s.query}".strip())
            payload.append({"id": s.id, "query": s.query, "run": run})
        # Plain text (no Markdown) — this `content` is what plain-text clients (and the
        # Telegram/web fallbacks) show verbatim; the web UI and Telegram each render their own
        # interactive list from `saved_searches`, so they don't reuse this body.
        lines = "\n".join(f"• {s.query}  (id {s.id})" for s in items)
        return {
            "type": "saved_searches",
            "content": f"📌 Your pins ({len(items)})\n{lines}",
            "saved_searches": payload,
        }

    async def _flashcards_command(self, arg: str, attachments: Optional[list]) -> dict:
        """Build an interactive multiple-choice study quiz from an attached PDF / image / slide
        deck / doc, OR from a URL's fetched text (`flashcards <url>` — clean text, no OCR, so proper
        nouns stay correct). Returns a `flashcards` result the web UI and Telegram render."""
        import asyncio
        from app.services import flashcards_service

        # URL path: use the page's real fetched text (same source as summarize / translate <url>),
        # never an OCR'd screenshot — accurate names/numbers and more cards.
        urls = self.search_service.extract_urls(arg or "")
        if urls and not attachments:
            try:
                fetched = await self.search_service.fetch_urls([urls[0]], max_urls=1)
            except Exception as e:
                return {"type": "text", "content": f"Couldn't fetch {urls[0]}: {e}"}
            if not fetched or fetched[0].get("error") or not fetched[0].get("content"):
                err = (fetched[0].get("error") if fetched else None) or "no readable text found"
                return {"type": "text", "content": f"Couldn't read that page to make flashcards: {err}"}
            title = fetched[0].get("title", "") or "Flashcards"
            body = (f"{title}\n\n" if title else "") + fetched[0]["content"]
            cards = await flashcards_service.generate_flashcards(body, self.chat_service)
            if not cards:
                return {"type": "text", "content": "Couldn't generate flashcards from that page."}
            return {"type": "flashcards", "title": title, "source": title, "cards": cards,
                    "note": "", "content": f"🎴 {len(cards)} flashcards from {title}"}

        if not attachments:
            return {
                "type": "text",
                "content": (
                    "Attach a PDF, image, or slide deck (PPTX/DOCX) — or send `flashcards <url>` — "
                    "to generate an interactive multiple-choice study quiz."
                ),
            }

        text, label, img_only = await asyncio.to_thread(
            flashcards_service.extract_source_text, attachments)
        if not text or not text.strip():
            if img_only:
                return {"type": "text", "content": (
                    "Couldn't read text from that image. If it's a screenshot sent as a Telegram "
                    "*photo*, Telegram compressed it — re-send it as a **file/document** "
                    "(uncompressed) for OCR, or use a text PDF/slide deck for best results.")}
            return {"type": "text", "content": "Couldn't read any text from that file to study from."}
        cards = await flashcards_service.generate_flashcards(text, self.chat_service)
        if not cards:
            return {"type": "text", "content": "Couldn't generate flashcards from that document — try a clearer or more text-rich file."}
        note = ("⚠️ This came from an image (OCR), so the text may be imperfect — a text PDF or "
                "slide deck gives better cards.") if img_only else ""
        return {
            "type": "flashcards",
            "title": label,
            "source": label,
            "cards": cards,
            "note": note,
            "content": f"🎴 {len(cards)} flashcards from {label}",
        }
