# botframework/

The autonomous bot framework, **merged into PosterChanAI**. These are the fediverse listeners
and daemons (Pleroma reply bots, nitter relays, blockbot/welcome/report/
hashtag/unfollow) that PosterChanAI's bot manager spawns as child processes.

**You don't run anything in here directly.** Bots are added, configured, and toggled from the
web UI:

> **Admin → Bots**

and supervised by `app/services/bot_manager_service.py`. Configuration lives in the database
(the `Bot` model), not in a `bots_config.py` file.

## Setup

Handled by PosterChanAI's installer — `./install.sh` (option 6 to refresh deps) installs this
package's requirements (`botframework/requirements.txt`) into the app's venv and creates the
`bots` table. Nothing bot-specific to install separately.

## Docs

See **[../docs/BOTS.md](../docs/BOTS.md)** for the full picture: the bot manager, the single
PosterChanAI server URL, per-bot config, per-node cutover, and troubleshooting.

## main.py modes (reference)

Each bot is `main.py <modes>`; the manager builds the modes from the bot's **Features** in the
UI. For reference:

| Mode | Bot |
|------|-----|
| `--pleroma` | reply to mentions on that platform |
| `--nitter` | relay Nitter (X/Twitter) RSS into the room/feed |
| `--welcome` | welcome new local users |
| `--blockbot` | announce block events |
| `--report` | announce moderation reports |
| `--hashtagbot` | scheduled hashtag posts |
| `--unfollowbot` | prune/announce unfollows |
| `--image` | scheduled image poster |

## Note on Python

The code is kept compatible with the app's runtime Python (3.11+); it also runs natively on
3.13. Adding 3.12+ syntax will fail `py_compile` under the service venv — keep it 3.11-safe.
