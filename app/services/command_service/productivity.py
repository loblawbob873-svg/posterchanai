"""Auto-split from the original command_service.py monolith (mixin pattern). No behavior change."""
from ._common import Optional


class _ProductivityMixin:
    async def _todo_command(self, arg: str) -> dict:
        """Todo command - DISABLED (CalDAV removed)"""
        return {"type": "text", "content": "⚠️ The todo feature is temporarily unavailable."}

    async def _remind_command(self, arg: str) -> dict:
        """Set/cancel a reminder. `remind <what> <when>` parses natural language via the LLM and
        stores it; `remind list` shows them; `remind cancel <id>` cancels one. Delivered later by
        the reminder scheduler to the web UI (always) and Telegram (if configured)."""
        from app.services import reminder_service

        if self.user is None:
            return {"type": "text", "content": "Sign in to set reminders."}
        arg = (arg or "").strip()
        low = arg.lower()

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
        # Plain text (no Markdown) — this `content` is what plain-text clients (Matrix, and the
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
