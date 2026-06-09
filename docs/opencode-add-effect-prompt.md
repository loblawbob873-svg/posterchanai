# Prompt: add a new "audio clip over image" effect

Copy the block below into opencode, filling in the three placeholders at the top.
These effects turn an attached image into a short MP4 with a sound clip over it
(like `curb`, `fbi`, `smell`). The effect name becomes a command usable in the
web UI, Telegram, Matrix, and the Pleroma/Misskey/Matrix bots, and it
automatically supports the `<name> zoom` Ken Burns pan-out (no extra wiring).

---

You are adding a new media effect to PosterChanAI. Be exact and consistent with
the existing effects — copy an existing one verbatim and rename.

NAME = `smellz`              # the command word, lowercase, must NOT collide with an
                            # existing command (check `help`, `node`, `budget`, etc.)
EMOJI = `👃`                 # one emoji for summaries/buttons
YOUTUBE_URL = `https://www.youtube.com/watch?v=XXXXXXXXXXX`
CLIP = full                 # `full`, or a range like `5-10` (seconds)

## 0. Download the audio asset
Run (use the repo's yt-dlp at `venv-ipex/bin/yt-dlp`):
- full clip:  `venv-ipex/bin/yt-dlp -x --audio-format mp3 --audio-quality 5 -o "assets/NAME.%(ext)s" "YOUTUBE_URL"`
- range 5-10: add `--download-sections "*5-10" --force-keyframes-at-cuts`
Then check length: `ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 assets/NAME.mp3`
Set DURATION to a value slightly ABOVE the clip length (e.g. clip 4.2s → 5.0); the
render uses `-shortest` so it ends at the audio. **`git add assets/NAME.mp3`** (sync.sh's
`git commit -a` will NOT pick up the new file otherwise).

## 1. app/services/effects_service.py
Copy the entire `# Smell (...)` section (the `_SMELL_AUDIO_CANDIDATES`,
`_SMELL_DURATION`, `_smell_audio_path`, `add_smell`, `smell_attachments`) to the
end of the file and rename SMELL→NAME, smell→name, 👃→EMOJI, duration→DURATION.

## 2. app/services/command_service.py
- Add to the `COMMANDS` dict: `"NAME": "Turn an attached image into a short MP4 set to the ... clip: NAME",`
- Add to `execute_command`: `elif command == "NAME": return await self._NAME_command(attachments)`
- Add the `_NAME_command` method (copy `_smell_command`, rename, import `NAME_attachments`).
- Add `"NAME"` to the `ZOOMABLE_EFFECTS` set (this is what gives you `NAME zoom`).

## 3. app/routers/media_api.py
- Add `"NAME"` to the `if command not in (...)` allowlist tuple.
- Add `elif command == "NAME": outputs, summary = await asyncio.to_thread(effects_service.NAME_attachments, attachments)`

## 4. app/routers/chat.py
- Add `"NAME"` to the `build_media_attachments` command allowlist tuple (the one
  with `compress, clip, convert, translate, meme, ...`).

## 5. app/routers/matrix.py
- Add `"NAME"` to the media-command allowlist tuple.
- Add a help line: `"• \`NAME\` — turn an image into a ... video.\n"`

## 6. app/routers/telegram.py  (the most edit sites — do ALL of them)
- BOTH hardcoded `commands = [...]` lists (there are two identical ones).
- BOTH media allowlist tuples: the `has_images and ... command not in (...)` gate
  and the `oversized_attachment and command in (..., None)` gate.
- The "send the file with `...`" caption help line (append `, \`NAME\``).
- The "✨ Effects → EMOJI Name — ..." help line.
- `_media_effects_keyboard()`: add a button `{"text": "EMOJI Name", "callback_data": "media:zq:NAME"}`
  (the `zq:` prefix routes it through the zoom Yes/No prompt automatically).
- Add a callback render block (copy the `elif _action == "smell":` block, rename).
  This is the "No zoom" path the prompt falls back to.

## 7. botframework/pleromaListener.py AND botframework/misskeyListener.py (identical edits)
- Help line: `"• NAME — turn an attached image into a ... video\n"`
- The `text = "" if command in (...)` suppress tuple: add `"NAME"`.
- The `for _c in (...)` media-command loop tuple: add `"NAME"`.

## 8. botframework/matrixListener.py
- The `or lower_prompt == "..." or lower_prompt.startswith("... ")` chain: add a line for NAME.
- `_MEDIA_REPLY_CMDS` tuple: add `"NAME"`.
- The `lower_prompt.startswith((...))` suppress tuple: add `"NAME"`.
- The help `_opts.append("• \`NAME\` — turn the image into a ... video")` line.

## 9. Verify
- `venv-ipex/bin/python -m py_compile app/services/effects_service.py app/services/command_service.py app/routers/media_api.py app/routers/matrix.py app/routers/telegram.py app/routers/chat.py botframework/pleromaListener.py botframework/misskeyListener.py botframework/matrixListener.py`
- Smoke test:
  ```python
  venv-ipex/bin/python -c "
  from PIL import Image; import io
  b=io.BytesIO(); Image.new('RGB',(640,480)).save(b,'JPEG'); d=b.getvalue()
  from app.services.effects_service import NAME_attachments
  outs,s=NAME_attachments([('t.jpg',d,'image/jpeg')]); print(s, len(outs[0]['data']))
  "
  ```

## 10. Deploy
- `git add assets/NAME.mp3`  (REQUIRED — new files aren't staged by `commit -a`)
- `./sync.sh`  (commits + pushes to origin/production, restarts nodes)
- Public mirror (only if asked): `git push github master:main`
