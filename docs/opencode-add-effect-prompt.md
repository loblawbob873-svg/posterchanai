# Prompt: add a new "audio clip over image" effect

Copy the block below into opencode, filling in the four placeholders at the top.
These effects turn an attached image into a short MP4 with a sound clip over it
(like `curb`, `fbi`, `hood`). The effect name becomes a command usable in the
web UI, Telegram, Matrix, and the Pleroma/Misskey/Matrix bots, and it
automatically supports the `<name> zoom` Ken Burns pan-out (no extra wiring).

Existing effects to copy from: `meme dildo poo cum blood bullethole fire gay
blacked kosher barked hava indian yakety yamete curb depressing fuu helpme gong
fbi redeem gigity beavis smell hood`.

---

You are adding a new "audio clip over image" media effect to PosterChanAI (turns an
attached image into a short MP4 with a sound clip, like `curb`/`hood`). Copy an
existing effect (e.g. `hood`) verbatim and rename. Be exact and consistent.

NAME = newfx                # command word, lowercase; must NOT collide with an existing
                            # command (check COMMANDS dict, e.g. help/node/budget)
EMOJI = 🎵
YOUTUBE_URL = https://www.youtube.com/watch?v=XXXXXXXXXXX
CLIP = full                 # `full`, or a range like 0-10 (seconds)

## 0. Download the audio asset
Use the repo's yt-dlp at `venv-ipex/bin/yt-dlp`:
- full clip:  `venv-ipex/bin/yt-dlp -x --audio-format mp3 --audio-quality 5 -o "assets/NAME.%(ext)s" "YOUTUBE_URL"`
- range 0-10: add `--download-sections "*0-10" --force-keyframes-at-cuts`
Check length: `ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 assets/NAME.mp3`
Set DURATION = clip length rounded UP (render uses `-shortest`, so it ends at the audio).
**`git add assets/NAME.mp3`** — sync.sh's `git commit -a` will NOT pick up new files.

## 1. app/services/effects_service.py
Copy the whole `# Hood (...)` section (`_HOOD_AUDIO_CANDIDATES`, `_HOOD_DURATION`,
`_hood_audio_path`, `add_hood`, `hood_attachments`) and rename HOOD→NAME, hood→name,
🏚️→EMOJI, 10.0→DURATION.

## 2. app/services/command_service.py
- Add a `COMMANDS` dict entry for NAME.
- `execute_command`: `elif command == "NAME": return await self._NAME_command(attachments)`
- Add the `_NAME_command` method (copy `_hood_command`, rename, import `NAME_attachments`).
- Add `"NAME"` to the `ZOOMABLE_EFFECTS` set (this is what gives you `NAME zoom`).

## 3. app/routers/media_api.py
- Add `"NAME"` to the `if command not in (...)` allowlist tuple.
- Add `elif command == "NAME": outputs, summary = await asyncio.to_thread(effects_service.NAME_attachments, attachments)`

## 4. app/routers/chat.py
- Add `"NAME"` to the `build_media_attachments` command allowlist tuple.

## 5. app/routers/matrix.py
- Add `"NAME"` to the media-command allowlist tuple.
- Add a help line: `"• \`NAME\` — turn an image into a ... video.\n"`

## 6. app/routers/telegram.py  (most edit sites — do ALL)
- BOTH hardcoded `commands = [...]` lists (two identical ones).
- BOTH media allowlist tuples: the `has_images ... command not in (...)` gate and the
  `oversized_attachment and command in (..., None)` gate (the latter ends with `None`).
- The "send the file with `...`" caption help line.
- The "✨ Effects → EMOJI Name — ..." help line.
- `_media_effects_keyboard()`: add `{"text": "EMOJI Name", "callback_data": "media:zq:NAME"}`
  (the `zq:` prefix auto-routes through the zoom Yes/No prompt).
- A callback render block: copy the `elif _action == "hood":` block, rename. This is the
  "No zoom" path the prompt falls back to.

## 7. botframework/pleromaListener.py AND botframework/misskeyListener.py (identical edits)
- Help line: `"• NAME — turn an attached image into a ... video\n"`
- The `text = "" if command in (...)` suppress tuple: add `"NAME"`.
- The `for _c in (...)` media-command loop tuple: add `"NAME"`.

## 8. botframework/matrixListener.py
- The `or lower_prompt == "..." or lower_prompt.startswith("... ")` chain: add a NAME line.
- `_MEDIA_REPLY_CMDS` tuple: add `"NAME"`.
- The `lower_prompt.startswith((...))` suppress tuple: add `"NAME"`.
- The help `_opts.append("• \`NAME\` — turn the image into a ... video")` line.

## 9. Verify
- `venv-ipex/bin/python -m py_compile app/services/effects_service.py app/services/command_service.py app/routers/media_api.py app/routers/matrix.py app/routers/telegram.py app/routers/chat.py botframework/pleromaListener.py botframework/misskeyListener.py botframework/matrixListener.py`
- Smoke test:
  ```
  venv-ipex/bin/python -c "from PIL import Image; import io; b=io.BytesIO(); Image.new('RGB',(640,480)).save(b,'JPEG'); from app.services.effects_service import NAME_attachments; o,s=NAME_attachments([('t.jpg',b.getvalue(),'image/jpeg')]); print(s, len(o[0]['data']))"
  ```

## 10. Deploy
- `git add assets/NAME.mp3`  (REQUIRED — new files aren't staged by `commit -a`)
- `./sync.sh`  (commits + pushes to origin/production, restarts both nodes)
- Public mirror (only if asked): `git push github master:main`
