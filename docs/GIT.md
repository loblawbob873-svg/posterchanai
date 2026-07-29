# The built-in Git server — a friendly guide

PosterChanAI ships its **own git server**. You can host repositories on your node, clone and push over
plain `https://`, and browse/edit them in the web client at **Discover → Git**.

The unusual part: **there are no accounts, passwords, SSH keys or access tokens.** Permission to write
is proved with your **Nostr key** — the same key you log into the client with. If you already know
GitHub or Gitea, everything below will look familiar; only the login part is different.

New to the internals? This page is the *how do I use it* guide. The design/security notes live in
[GIT_OVER_NOSTR.md](GIT_OVER_NOSTR.md).

---

## 1. The 60-second mental model

| Thing | What it is |
|---|---|
| A **repo** | A normal bare git repository on your node, under `<storage path>/git_repos/<owner-pubkey>/<name>.git`. Ordinary `git` reads and writes it — nothing proprietary. |
| Its **address** | `https://<your-domain>/git/<npub>/<name>.git` — the owner's npub is part of the URL. That's how the server knows who owns it. |
| Its **listing** | A Nostr event (**kind 30617**, "repo announcement") that says *this repo exists, here's its clone URL, here are its maintainers*. That event is what makes it appear in Discover → Git — here and in other Nostr git clients. |
| Its **state** | A Nostr event (**kind 30618**, "repo state") that says *branch `master` is currently commit `abc123…`*. |
| **Who may push** | Whoever the announcement lists as a maintainer — the owner, plus anyone in its `maintainers` tag. Nobody else, ever. |

So: **git stores the code, Nostr stores who-owns-what and what-the-branches-are.** Nothing about your
repo depends on a company's server staying up.

---

## 2. Turn it on (operator, once)

In **Admin → Git**:

1. **Run the git server on this node** → on (`git_server_enabled`).
2. **Public base URL** (`git_server_public_base`) → `https://your-domain/git` — this is what gets put
   into clone URLs, so set it before creating repos.
3. Make sure your nginx has a `location /git/` block pointing at the git host (port **3053**). See
   [NGINX.md](NGINX.md).
4. Restart the app (`sudo systemctl restart posterchanai`), then check **Admin → Git** shows the
   git host as running.

Nothing spawns and every git route 404s until step 1 — the feature ships dormant on purpose.

Docker users: set `POSTERCHANAI_GIT=1` (see [DOCKER.md](DOCKER.md)); port 3053 is already exposed in
`docker-compose.yml`. Bare-metal users can run `./install.sh --git-host` to check prerequisites.

**Multi-node:** run the server on ONE node and set `git_server_proxy_url = http://that-node:3053` on
the others; they'll forward git traffic to it. Details in [GIT_OVER_NOSTR.md](GIT_OVER_NOSTR.md).

---

## 3. Create a repository

Creating a repo is **admin/allowlisted** (it consumes disk), so it's an API call rather than a button.
Get an API key from **Settings → API keys**, then:

```bash
curl -s -X POST https://your-domain/api/git/host \
  -H "Authorization: Bearer sk-YOUR-KEY" \
  -H "Content-Type: application/json" \
  -d '{"repo_id":"my-app","name":"My App","description":"a thing I am building"}'
```

You get back the clone URL plus the tags for the announcement:

```json
{"ok": true, "repo_id": "my-app", "npub": "npub1…",
 "clone": "https://your-domain/git/npub1…/my-app.git",
 "announce_tags_30617": [["d","my-app"], ["name","My App"], ["clone","https://…"], ["maintainers","<hex>"]]}
```

Two ways to publish the announcement so the repo shows up in Discover → Git:

* **From the client (recommended):** open **Discover → Git → ＋ Announce a repo**, fill in the repo id
  (`my-app`), name, description and paste the clone URL. It's signed with your own key.
* **From the server**, when the repo is owned by the node's own operator key:
  `curl -X POST https://your-domain/api/git/announce -H "Authorization: Bearer sk-…" -d '{"repo_id":"my-app"}'`

To allow other people to push, add their pubkeys to the announcement's `maintainers` tag (re-announce
with the extra keys — announcements are replaceable, so the newest one wins).

**Private repos** (`"private": true` in the create call) are never announced and require a signed
header even to *clone*. Add readers with `"readers": ["npub1…"]`.

---

## 4. Push to it

Your first push has to prove you're a maintainer. There are two ways.

### The easy way — the repo's own mirror script

If you're pushing *this* project, `./sync.sh` already does everything (it mirrors every deploy to the
nostr repo via `scripts/grasp_mirror.py`). Skip ahead.

### By hand, for any repo

The server accepts a **NIP-98** signed header on the push. `scripts/grasp_mirror.py` is the reference
implementation, and it also publishes the kind-30618 state event for the new tip:

```bash
cd my-app
git remote add nostr https://your-domain/git/npub1…/my-app.git
# on the hosting node, with the owner's key in the keystore:
venv/bin/python scripts/grasp_mirror.py            # publishes 30618 + pushes
```

What a push checks, in plain terms:

1. Do you present a **fresh signature** from a key that the announcement lists as a maintainer? → allowed.
2. Otherwise, is there a **maintainer-signed kind-30618** on the relay that says this branch should now
   point at exactly the commit you're pushing? → allowed.
3. Anything else → rejected, and nothing is written. Errors always fail *closed*.

> **Big first push?** Run `git config http.postBuffer 524288000` if you're on an older node. Modern
> versions of the server handle chunked uploads natively, so this is only a fallback.

### Cloning

Public repos clone anonymously, like any other git server:

```bash
git clone https://your-domain/git/npub1…/my-app.git
```

Nothing to log into. A **private** repo needs a signed header — see
[GIT_OVER_NOSTR.md](GIT_OVER_NOSTR.md#private-repos--read-gate-security-critical).

---

## 5. The web UI (Discover → Git)

Open the client, go to **Discover → Git**, and tap a repo. Repos hosted on this node get the full
browser; repos announced from GitHub/Gitea show their README and the Nostr issues/patches only.

**Header** — the clone URL with a ⧉ copy button, and a **⎇ branch button**. Tap it to switch branch or
tag; the file list and commit list both follow your choice.

**📖 README** — rendered markdown.

**📁 Files** — click through directories; each row shows the last commit that touched it and when.
Click a file to open it. In the file header you get:

| Button | What it does |
|---|---|
| **⬇ Download** | Saves the file to your device (works for binaries too). |
| **🕘 History** | Just the commits that touched this file. |
| **✏️ Edit** | Opens the editor (maintainers only). |
| **🗑 Delete** | Removes the file in a commit (maintainers only). |

Plus **＋ New file** above the listing to create one.

**🕘 Commits** — the history. **Tap any commit to see exactly what changed**: per-file patches with
line numbers, added lines in green, removed in red, and a `+n / −n` summary per file. Big files start
collapsed; tap the header to expand.

**🐛 Issues / 🩹 Patches** — NIP-34 collaboration. Anyone with a Nostr key can open an issue; it's a
signed event, not a row in our database, so other Nostr git clients see it too.

Everything is built for phones: one-column rows, a single line-number gutter on narrow screens, and
diffs that scroll sideways inside their own box instead of stretching the page.

### Editing in the browser

Tap **✏️ Edit**, change the text, write a commit message, and hit **✓ Commit** (or Ctrl/⌘+Enter).

What happens under the hood — worth knowing, because it's what makes web editing safe:

1. The client asks you to **sign a NIP-98 event** bound to this exact repo's write route. (If you use
   Amber or a browser extension, it prompts; a local key signs silently.)
2. The git host verifies that signature against the repo's **maintainer list from Nostr** — the very
   same check a `git push` goes through. A web edit therefore has *exactly* the authority of a push and
   not one bit more.
3. It writes a real git commit (authored as your npub), fast-forwarding the branch.
4. It hands back the new **kind-30618** state, which your client signs and publishes — so the repo's
   signed state on Nostr keeps matching reality, and your next `git push` still validates.

If someone else pushed while your editor was open, the commit is **refused** rather than silently
overwriting them ("The branch moved — reopen the file and re-apply your change").

You can only edit on a **branch**, not on a tag, and only text files (open a binary and you'll be
offered a download instead).

---

## 6. Which Nostr bits are used

| Kind | Name | Used for |
|---|---|---|
| 30617 | Repository announcement (NIP-34) | Listing a repo, and the **maintainer list** that decides who may write |
| 30618 | Repository state (NIP-34) | What each branch points at; a push is authorized against it |
| 1621 / 1617 | Issue / Patch (NIP-34) | Collaboration, from any Nostr client |
| 27235 | HTTP Auth (NIP-98) | Signed proof on a push, a private clone, or a web commit |

All of these are ordinary signed Nostr events on the built-in relay, so a second client (gitworkshop,
ngit, …) can see your repos and issues without any extra work on your part.

---

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Everything git 404s | `git_server_enabled` is off, or you're on a **proxy** node — manage repos on the hosting node. |
| Repo doesn't appear in Discover → Git | No 30617 announcement published (or the repo is private, which is never announced). |
| `git push` rejected | Your key isn't in the announcement's `maintainers`, or no matching kind-30618 exists. Publishing the 30618 is what `scripts/grasp_mirror.py` does for you. |
| `400 Bad request syntax` on a big push | An old node without chunked-upload support. Update it, or set `git config http.postBuffer 524288000`. |
| Clone URL is empty in the API response | `git_server_public_base` isn't set. |
| No ✏️ Edit button | You're not a maintainer of that repo, you're viewing a tag rather than a branch, the file is binary, or you're browsing as a guest. |
| "the git host did not answer" | The subprocess isn't running (Admin → Git) or `git_server_proxy_url` points somewhere unreachable. |
| Files/Commits tabs missing entirely | That repo isn't hosted here — its clone URL has a username instead of an npub, so there's no file API to read. |

Logs: `journalctl -u posterchanai.service | grep git-host`.
