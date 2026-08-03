"""System health report — agentic edition.

Runs on a cron schedule (default 01:00, 12:00, 18:00) and on demand via the `/logs` command.
Instead of a hardcoded sequence of shell commands + regex parsing, it drives the existing
agentic node tooling (``node_service.run_agent``): for each configured node it hands the model a
fixed health-check goal and lets it run read-only diagnostic commands, then files the model's
report into the admin's "Logs" conversation and (optionally) Telegram.

All command execution, SSH, per-command timeouts, job logging and live streaming are delegated to
``node_service`` — this module just orchestrates and formats. The set of nodes is the same
Agentic Node Management config (Admin → Nodes); ``logs_nodes`` optionally narrows it.
"""
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.database import SessionLocal
from app.models import User, Conversation, Message
from app.services import node_service, settings_store
from app.services.chat_service import ChatService

logger = logging.getLogger(__name__)

# Global scheduler instance
logs_scheduler: Optional[AsyncIOScheduler] = None

# Name of the Logs conversation
LOGS_CHAT_TITLE = "Logs"

# The goal handed to the agent for each node. It pins down BOTH the emoji semantics and an exact
# line template so every node's report is formatted identically (the model otherwise drifts on
# layout and mislabels healthy-empty results as ⚪). The model still discovers specifics (drive
# names, whether RAID/btrfs/swap exist) for itself rather than relying on hardcoded device lists.
# What the AGENT does: gather accurate data and summarise it in plain text. It is deliberately NOT
# asked to format/emoji anything — this model reliably gathers but won't honour a strict layout at
# the finish step, so presentation is handled deterministically afterwards (see _to_status_board).
_HEALTH_GOAL = (
    "You are auditing this host. Use read-only commands only.\n\n"
    "Check each of these six subsystems: (1) disk usage of /, /boot and /raid; (2) SMART health of "
    "every physical drive; (3) RAID/mdstat; (4) failed systemd services; (5) swap; (6) WARN/ERROR "
    "lines in the last 6h of journalctl and dmesg (group repeats, ignore routine noise).\n\n"
    "SMART must be ACCURATE: run 'lsblk -d -o NAME,TRAN' to get the real drive names, then "
    "'sudo -n smartctl -H /dev/<name>' on EACH (retry with '-d sat' for USB drives or '-d nvme' "
    "for NVMe if it asks for a device type). Report the literal 'overall-health ... result' per "
    "drive (PASSED/FAILED) and NEVER say 'no data' when it printed PASSED/FAILED. Use 'sudo -n' for "
    "smartctl/dmesg/journalctl so they never block on a password prompt.\n\n"
    "When done, call the finish tool with a brief plain-text summary that, for EACH of the six "
    "subsystems, states the finding and whether it is healthy, a warning, critical, or not present "
    "on this host."
)

# The MEASURED half of the report, and the fallback for a node that can't run the LLM 'agent' loop
# (the lightweight standalone agent on router.lan etc. has no local model). It covers the SAME six
# subsystems as _HEALTH_GOAL in one read-only shell pass — the agent loop is only how a node gathers,
# so a shell-only node has no reason to report less. It now runs on EVERY node, because _parse_probe
# turns this output into the board's facts (see the fact-check block below) rather than trusting the
# model's retelling of them. Log/dmesg lines are normalised (timestamps, hex ids and digits blanked)
# then counted, so thousands of repeats of one nginx error collapse to a single row.
# Two deliberate widenings, both of which the model was papering over with invention:
#  - disk lists the host's REAL local mounts instead of the fixed `/ /boot /raid` triple, which
#    reported nothing for a NAS array mounted anywhere else (and left "/raid 63%" to be imagined for
#    a host with no such mount).
#  - raid reads `zpool status` as well as /proc/mdstat, so a ZFS host isn't declared array-less.
# The dmesg leg is 6h-windowed like the journal one (and like _ERROR_SAMPLE_SHELL): unbounded, it
# counts boot-time warnings forever, which pins the "Errors (6h)" row to a permanent warning and
# disagrees with the sample lines printed under it. `--since` needs util-linux >= 2.37, hence the
# fallback to the whole buffer.
# Every leg that can FAIL says so with an explicit `probe-error:` marker taken from its exit status,
# because the alternative is a silent false green: `sudo -n journalctl` without the sudoers rule
# exits nonzero having printed nothing, which is byte-identical to a host with no errors, and
# `${f:-none failed}` reports a healthy systemd when systemctl couldn't reach the bus at all. Sniffing
# the output for "permission denied" instead is not an option — that string is ordinary journal
# CONTENT (an nginx open() failure, a bad sudo password), so it would report a host with thousands of
# real errors as an unreadable journal. `head` limits are generous rather than tight for the same
# family of reasons: mdstat lists arrays newest-first, so a low cap drops the OLDEST array — usually
# the data array — and reports the remaining ones as clean. LC_ALL=C pins the labels the parsers
# match ('Swap:', 'Use%'), which a localised host would otherwise translate out of existence.
_HEALTH_SHELL = r"""
export LC_ALL=C
echo '== disk =='
df -hPl -x tmpfs -x devtmpfs -x squashfs -x overlay -x efivarfs 2>/dev/null | head -40
echo; echo '== smart =='
s=$(for d in $(lsblk -dn -o NAME 2>/dev/null | grep -Ev '^(loop|ram|zram|sr|dm-)'); do
  o=$(sudo -n smartctl -H /dev/$d 2>&1)
  case "$o" in *"device type"*|*"Unknown USB"*) o=$(sudo -n smartctl -H -d sat /dev/$d 2>&1);; esac
  echo "$d: $(echo "$o" | grep -Ei 'overall-health|SMART Health Status|Unavailable|not found|Permission denied|Operation not permitted' | head -1)"
done)
echo "${s:-no drives reported}"
echo; echo '== raid =='; cat /proc/mdstat 2>/dev/null | head -60
if command -v zpool >/dev/null 2>&1; then
  { sudo -n zpool status 2>/dev/null || zpool status 2>/dev/null; } |
    grep -Ei '^ *(pool|state|status):' | head -20
fi
echo; echo '== failed systemd units =='
f=$(systemctl --failed --no-legend 2>/dev/null); rc=$?
if [ $rc -ne 0 ]; then echo "probe-error: systemctl exit $rc"
else echo "${f:-none failed}" | head -20; fi
echo; echo '== swap =='; free -h 2>/dev/null | grep -iE '^ *total|swap'
echo; echo '== journal errors 6h =='
j=$(sudo -n journalctl --since -6h -p err --no-pager -q 2>/dev/null); rc=$?
[ $rc -ne 0 ] && echo "probe-error: journalctl exit $rc"
printf '%s\n' "$j" |
  sed -E 's/^[A-Z][a-z]{2} +[0-9]+ [0-9:]+ [^ ]+ //; s/[0-9a-f:]{4,}//g; s/[0-9]+/#/g' |
  grep -v '^ *$' | cut -c1-110 | sort | uniq -c | sort -rn | head -8
echo "total: $(printf '%s' "$j" | grep -c .) error lines"
echo; echo '== dmesg warn/err =='
d=$({ sudo -n dmesg -T --level=err,warn --since '6 hours ago' 2>/dev/null ||
      sudo -n dmesg -T --level=err,warn 2>/dev/null; }); rc=$?
[ $rc -ne 0 ] && echo "probe-error: dmesg exit $rc"
printf '%s\n' "$d" | grep -v 'IN=.*OUT=' |
  sed -E 's/^\[[^]]*\] //; s/[0-9]+/#/g' | grep -v '^ *$' |
  cut -c1-110 | sort | uniq -c | sort -rn | head -6
true
""".strip()

# Sample lines behind the "Errors (6h)" row. A bare count ("11 journal + 8 dmesg errors", "10 SMART
# errors, 2 I/O timeouts") says something is wrong but never WHAT, which makes the row unactionable —
# you can't tell a dying disk from a chatty nginx. So the top repeat-groups are fetched
# DETERMINISTICALLY (like _node_uptime, NOT via the model, which paraphrases the specifics away) and
# printed verbatim underneath. Grouping keeps one REAL sample line per group: the counting pass in
# _HEALTH_SHELL blanks digits/hex, which is right for tallying repeats and unreadable as evidence.
# The dmesg leg keeps _HEALTH_SHELL's proven filters (err/warn only, minus firewall IN=/OUT= spam) —
# without --level the keyword-matched result was mostly boot-time chatter like 'ata1: SATA max UDMA'.
# `--since` needs util-linux >= 2.37; the fallback covers the whole ring buffer rather than nothing.
_ERROR_SAMPLE_SHELL = r"""
g() { awk '{ k=$0; gsub(/[0-9a-f]{8,}/,"",k); gsub(/[0-9]+/,"#",k);
             if (!(k in s)) s[k]=$0; c[k]++ }
       END { for (k in c) printf "%d\t%s\n", c[k], s[k] }' | sort -rn | head -"$1" | cut -c1-180; }
echo '== journal =='
sudo -n journalctl --since -6h -p err --no-pager -q 2>/dev/null |
  sed -E 's/^[A-Z][a-z]{2} +[0-9]+ [0-9:]+ [^ ]+ //' | g 5
echo '== dmesg =='
{ sudo -n dmesg -T --level=err,warn --since '6 hours ago' 2>/dev/null ||
  sudo -n dmesg -T --level=err,warn 2>/dev/null; } |
  grep -v 'IN=.*OUT=' | sed -E 's/^\[[^]]*\] //' | g 4
""".strip()

# Cap on how much evidence rides along: enough to identify the fault, not enough to bury the board.
_ERROR_SAMPLE_MAX = 8

# Deterministic status board — Python owns the emojis + layout so they're identical on every node.
# The model only supplies a status word + short detail per subsystem (an easy single-shot task);
# the icons and ordering below are never the model's job.
_BOARD_ICON = {"disk": "💾", "smart": "🔧", "raid": "💿", "services": "⚙️", "swap": "🔄", "errors": "📜"}
_BOARD_LABEL = {"disk": "Disk", "smart": "SMART", "raid": "RAID", "services": "Services",
                "swap": "Swap", "errors": "Errors (6h)"}
_BOARD_ORDER = ["disk", "smart", "raid", "services", "swap", "errors"]
_STATUS_EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴", "none": "⚪"}

_BOARD_SYS = (
    "You convert a server health summary into a status board. Output EXACTLY six lines, in this "
    "order, and NOTHING else. Each line is 'subsystem|status|detail':\n"
    "disk|<status>|<detail>\nsmart|<status>|<detail>\nraid|<status>|<detail>\n"
    "services|<status>|<detail>\nswap|<status>|<detail>\nerrors|<status>|<detail>\n"
    "<status> is exactly one of: green yellow red none. "
    "green = healthy / passed / none / zero-used; yellow = warning (e.g. disk 75-90%); "
    "red = critical / failed / degraded / errors present; none = subsystem not present on this host "
    "(e.g. no RAID array, no swap). A clean or empty result is green, never none. "
    "'services' is ONLY failed systemd units — green when none failed. Journal/dmesg/log errors "
    "belong to 'errors', NEVER to 'services'. "
    "<detail> is a terse phrase, e.g. '/ 33%, /raid 63%' or 'sda,sdb,nvme PASSED' or 'none failed'. "
    "For 'errors', NAME the sources rather than only counting them — 'ata3 I/O errors, nginx upstream "
    "timeouts' beats '10 SMART errors, 2 I/O timeouts', which identifies nothing. "
    "Use ONLY figures that literally appear in the input — never compute or invent a percentage."
)


# --- deterministic fact-check ---------------------------------------------------------------------
# The board's FACTS are parsed straight out of _HEALTH_SHELL's output here and OVERRIDE the model's
# row for every subsystem a command can answer outright. Formatting was already deterministic; the
# numbers were not, and two LLM hops (agent prose → board model) is two chances to invent one. This
# was not theoretical: a report claimed "RAID 🟢 degraded (disk 4 failed)" on a host whose md0 was
# `active raid5 [3/3] [UUU]` with no fourth disk, "2048M swap available" on a host with no swap, and
# "/raid 63%" for a mount that doesn't exist — while a second node reported "no RAID array" over a
# healthy one. Note the first of those also rendered a GREEN dot beside the word "degraded": the
# model picks status and detail independently, so they can contradict each other and nothing noticed.
# Only 'errors' still takes the model's wording (naming the sources is genuinely a language task);
# even there the probe owns the status and the zero case.

# A host with many ZFS datasets / btrfs subvolumes / LVM volumes has a df row each; listing them all
# would be an unreadable Telegram line, so the detail shows the fullest few (status still sees all).
_DISK_DETAIL_MAX = 6
_PROBE_ERROR = "probe-error:"


def _probe_sections(raw: str) -> dict:
    """Split _HEALTH_SHELL output on its '== name ==' markers into {name: [lines]}."""
    out: dict[str, list] = {}
    key = ""
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("==") and stripped.endswith("==") and len(stripped) > 4:
            key = stripped.strip("= ").strip().lower()
            out.setdefault(key, [])
            continue
        if key:
            out[key].append(line.rstrip())
    return out


def _to_bytes(size: str) -> Optional[float]:
    """'0B' / '7.6Gi' / '2048M' → bytes. None when it isn't a size at all."""
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*([KMGTPE]?)i?B?", (size or "").strip(), re.I)
    if not m:
        return None
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
            "T": 1024 ** 4, "P": 1024 ** 5, "E": 1024 ** 6}[m.group(2).upper()]
    return float(m.group(1).replace(",", ".")) * mult


def _probe_disk(lines: list) -> Optional[tuple]:
    """df -hP rows → worst use%. The header ends in 'on', so it can't match. The mount point is
    captured to END OF LINE, not as one \\S+ token: df prints '/media/USB Drive' unescaped, and a
    token-based capture yields 'Drive', fails the leading-'/' check and drops that filesystem — so a
    full disk with a space in its name would simply be missing from the row."""
    used = []
    for line in lines:
        m = re.search(r"\s(\d+)%\s+(/.*?)\s*$", line)
        if m:
            used.append((m.group(2), int(m.group(1))))
    if not used:
        return None
    worst = max(p for _, p in used)
    status = "red" if worst >= 90 else "yellow" if worst >= 75 else "green"
    # Status covers every mount; the DETAIL only has room for a few, so it shows the fullest (which
    # are the ones the status is about) rather than the first few in df order.
    shown = used if len(used) <= _DISK_DETAIL_MAX else sorted(used, key=lambda u: -u[1])[:_DISK_DETAIL_MAX]
    detail = ", ".join(f"{mount} {pct}%" for mount, pct in shown)
    if len(used) > len(shown):
        detail += f", +{len(used) - len(shown)} more"
    return status, detail


def _probe_smart(lines: list) -> Optional[tuple]:
    """'<dev>: SMART overall-health self-assessment test result: PASSED' (or 'SMART Health Status: OK',
    or a permission/unavailable message) per drive. A drive we couldn't read is a WARNING, never a
    silent omission — dropping the unreadable one is how a 4-drive host reported 3 drives PASSED."""
    ok, bad, unknown = [], [], []
    for line in lines:
        name, sep, rest = line.strip().partition(":")
        if not sep or not name or re.search(r"\s", name):
            continue
        text = rest.upper()
        if "FAILED" in text or "FAILING" in text:
            bad.append(name)
        elif "PASSED" in text or re.search(r"\bOK\b", text):
            ok.append(name)
        else:
            unknown.append(name)
    if not (ok or bad or unknown):
        return None
    parts = []
    if bad:
        parts.append(f"{','.join(bad)} FAILED")
    if unknown:
        parts.append(f"{','.join(unknown)} no result")
    if ok:
        parts.append(f"{','.join(ok)} PASSED")
    status = "red" if bad else "yellow" if unknown else "green"
    return status, "; ".join(parts)


def _probe_raid(lines: list) -> Optional[tuple]:
    """/proc/mdstat + `zpool status`. Degraded is read off the array itself — '[n/m]' with m < n, a
    '_' in the [UU_] map, an (F)aulty member or a non-active state — never off prose."""
    arrays, cur = [], None
    for line in lines:
        m = re.match(r"^(md\d+)\s*:\s*(\S+)\s*(.*)$", line.strip())
        if m:
            level = re.search(r"\braid\d+\b|\blinear\b|\bmultipath\b", m.group(3))
            cur = {"name": m.group(1), "state": m.group(2).lower(), "faulty": "(F)" in m.group(3),
                   "level": level.group(0) if level else "", "want": None, "have": None,
                   "map": "", "rebuild": False}
            arrays.append(cur)
            continue
        if cur is None:
            continue
        u = re.search(r"\[(\d+)/(\d+)\]\s*\[([U_]+)\]", line)
        if u:
            cur["want"], cur["have"], cur["map"] = int(u.group(1)), int(u.group(2)), u.group(3)
        if re.search(r"\b(recovery|resync|reshape|check)\s*=", line):
            cur["rebuild"] = True

    pools, pool = [], None
    for line in lines:
        m = re.match(r"^\s*(pool|state|status)\s*:\s*(.+)$", line.strip(), re.I)
        if not m:
            continue
        field, value = m.group(1).lower(), m.group(2).strip()
        if field == "pool":
            pool = {"name": value, "state": "", "note": ""}
            pools.append(pool)
        elif pool is None:
            continue
        elif field == "state":
            pool["state"] = value.upper()
        else:
            # `status:` is printed ONLY when something is wrong, and ZFS reports unrecoverable
            # errors there while `state:` stays ONLINE — reading state alone calls that pool healthy.
            pool["note"] = value

    if not arrays and not pools:
        return "none", "no RAID array"

    status, details = "green", []

    def _raise(level):
        nonlocal status
        if level == "red" or (level == "yellow" and status == "green"):
            status = level

    for a in arrays:
        degraded = (a["faulty"] or "_" in a["map"]
                    or (a["want"] is not None and a["have"] is not None and a["have"] < a["want"]))
        size = f" [{a['want']}/{a['have']}]" if a["want"] is not None else ""
        label = f"{a['name']} {a['level']}".strip()
        if degraded:
            _raise("red")
            details.append(f"{label} DEGRADED{size}")
        elif a["state"] != "active":
            # An 'inactive' md device is usually a stale/foreign superblock left by a removed disk —
            # it isn't carrying data and isn't at risk, so it's a warning. A DEGRADED array (checked
            # first, above) is the one that is live and one failure from loss.
            _raise("yellow")
            details.append(f"{label} {a['state']}{size}")
        elif a["rebuild"]:
            _raise("yellow")
            details.append(f"{label} rebuilding{size}")
        else:
            details.append(f"{label} clean{size}")
    for p in pools:
        state = p["state"] or "unknown"
        if state != "ONLINE":
            _raise("red")
        elif p["note"]:
            _raise("yellow")
        note = f" ({p['note'][:60]})" if p["note"] else ""
        details.append(f"{p['name']} {state}{note}")
    return status, ", ".join(details)


def _probe_services(lines: list) -> Optional[tuple]:
    """`systemctl --failed --no-legend`, or the literal 'none failed' the probe substitutes."""
    body = [ln.strip() for ln in lines if ln.strip()]
    if not body:
        return None
    if any(ln.startswith(_PROBE_ERROR) for ln in body):
        return "yellow", "could not query systemd"      # never green off a command that didn't run
    if len(body) == 1 and body[0].lower() == "none failed":
        return "green", "none failed"
    # systemctl prefixes each row with a '●' status bullet — taking split()[0] blind names every
    # failed unit "●" and counts them all as the same thing.
    names = [ln.lstrip("●*○◍ \t").split()[0] for ln in body if ln.lstrip("●*○◍ \t")]
    shown = ", ".join(names[:4]) + ("…" if len(names) > 4 else "")
    return "red", f"{len(names)} failed: {shown}"


def _probe_swap(lines: list) -> Optional[tuple]:
    """The `free -h` Swap row. Zero total means NO SWAP (⚪), which is a different thing from
    'swap is fine' — reporting a green figure for a host that has none is pure invention."""
    for line in lines:
        m = re.match(r"^\s*swap:\s+(\S+)\s+(\S+)\s+(\S+)", line, re.I)
        if not m:
            continue
        total, used = _to_bytes(m.group(1)), _to_bytes(m.group(2))
        if total is None or used is None:
            return None
        if total <= 0:
            return "none", "no swap configured"
        pct = used * 100 / total
        status = "red" if pct >= 90 else "yellow" if pct >= 50 else "green"
        return status, f"{m.group(2)} of {m.group(1)} used"
    return None


def _probe_errors(journal: list, dmesg: list) -> Optional[tuple]:
    """Counts only — the model still gets to NAME the sources when there are any (see _render_board),
    and the verbatim samples land underneath via _with_error_samples."""
    def _tally(rows):
        return sum(int(m.group(1)) for m in
                   (re.match(r"^\s*(\d+)\s+\S", ln) for ln in rows) if m)

    # A leg that couldn't RUN (no sudoers rule, dmesg_restrict) exits nonzero having printed nothing,
    # which is indistinguishable from a clean host — so the probe marks it and we refuse to call it
    # green. Detecting this by searching the output for "permission denied" instead would misfire on
    # ordinary journal CONTENT: an nginx open() failure and a bad `sudo` password both say exactly
    # that, and a host with thousands of real errors would be reported as an unreadable journal.
    unreadable = [ln.split(_PROBE_ERROR, 1)[1].strip()
                  for ln in journal + dmesg if _PROBE_ERROR in ln]
    if unreadable:
        return "yellow", f"logs unreadable ({unreadable[0]})"

    total = None
    for line in journal:
        m = re.search(r"total:\s*(\d+)\s+error lines", line)
        if m:
            total = int(m.group(1))
    if total is None:
        if not journal and not dmesg:
            return None
        total = _tally(journal)
    dm = _tally(dmesg)
    if total == 0 and dm == 0:
        return "green", "no errors"
    return "yellow", f"{total} journal, {dm} dmesg"


def _parse_probe(raw: str) -> dict:
    """_HEALTH_SHELL output → {subsystem: (status, detail)} for everything a command can answer.
    Rows the probe couldn't read are simply absent, so the model's row survives for those."""
    s = _probe_sections(raw)
    # A MISSING section is not a finding. Only 'raid' can legitimately parse to a row from no content
    # ("no RAID array"), so it — like the rest — is asked only when the probe actually reported it;
    # otherwise a truncated or failed probe would announce that a host has no array.
    rows = {}
    if "raid" in s:
        rows["raid"] = _probe_raid(s["raid"])
    if "disk" in s:
        rows["disk"] = _probe_disk(s["disk"])
    if "smart" in s:
        rows["smart"] = _probe_smart(s["smart"])
    if "failed systemd units" in s:
        rows["services"] = _probe_services(s["failed systemd units"])
    if "swap" in s:
        rows["swap"] = _probe_swap(s["swap"])
    if "journal errors 6h" in s or "dmesg warn/err" in s:
        rows["errors"] = _probe_errors(s.get("journal errors 6h", []), s.get("dmesg warn/err", []))
    return {k: v for k, v in rows.items() if v}


def _render_board(raw: str, probe: Optional[dict] = None) -> Optional[str]:
    """Parse the model's 'subsystem|status|detail' lines into the fixed emoji board, with `probe`'s
    measured rows overriding the model's. Returns None if too few rows in total (caller falls back to
    the plain summary)."""
    rows: dict[str, tuple[str, str]] = {}
    for line in (raw or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        key = parts[0].lower()
        key = next((k for k in _BOARD_ICON if k in key), None)
        if not key:
            continue
        status = parts[1].lower()
        status = next((s for s in _STATUS_EMOJI if s in status), "green")
        rows[key] = (status, parts[2])
    for key, (status, detail) in (probe or {}).items():
        prior_status, prior_detail = rows.get(key, ("", ""))
        if key == "errors" and status != "green":
            # 'errors' is the one row where the model earns its keep: it names WHICH source is noisy.
            # Keep that wording — but append the MEASURED counts rather than trusting the model's
            # (invented figures are the whole point of this override), and let a 'red' from the model
            # stand. The probe caps the row at yellow because it only counts lines; it can't tell a
            # dying disk from a chatty proxy, so it is a floor on severity, never a ceiling.
            if prior_detail:
                detail = f"{prior_detail} ({detail})"
            if prior_status == "red":
                status = "red"
        elif key == "raid" and status == "none" and prior_status and prior_status != "none":
            # mdstat/zpool found nothing, but the agent reported an array: hardware RAID (megaraid),
            # btrfs and dm-integrity are invisible to both, so "none" here means "no evidence", not
            # "no array". Overriding would turn a real DEGRADED controller into a ⚪.
            continue
        rows[key] = (status, detail)
    # The <4 floor is about the MODEL — a half-parsed board is worse than the plain summary. Measured
    # rows are known-good however few there are, so they're never thrown away on that count.
    if not rows or (len(rows) < 4 and not probe):
        return None
    lines = []
    for key in _BOARD_ORDER:
        if key in rows:
            status, detail = rows[key]
            lines.append(f"{_BOARD_ICON[key]} {_BOARD_LABEL[key]}: {_STATUS_EMOJI[status]} {detail}")
    return "\n".join(lines)


def _clean_sample(line: str) -> str:
    """Drop the characters that would unbalance Telegram MarkdownV1 in an arbitrary log line. `_` is
    deliberately left alone — it's everywhere in unit and device names, and telegram_service already
    retries a failed parse as plain text, so mangling every name is the worse trade."""
    return re.sub(r"\s{2,}", " ", re.sub(r"[*`]", "", line)).strip()


def _parse_error_samples(raw: str) -> list:
    """'<count>\\t<line>' rows under the '== journal ==' / '== dmesg ==' markers → display lines."""
    out, source = [], ""
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("=="):
            source = stripped.strip("= ").strip().lower()
            continue
        m = re.match(r"^\s*(\d+)\t(.+)$", line.rstrip())
        if not m:
            continue
        text = _clean_sample(m.group(2))
        if not text:
            continue
        # journal lines carry their own unit prefix ("kernel:", "nginx:"); dmesg lines don't, so say
        # where they came from — otherwise the two sources are indistinguishable in the list.
        prefix = "dmesg: " if source == "dmesg" else ""
        out.append(f"↳ ×{m.group(1)} {prefix}{text}")
    return out[:_ERROR_SAMPLE_MAX]


async def _run_probe(db, admin, name: str, target: str, script: str, timeout: int = 45) -> str:
    """Run a read-only shell script on a node over whichever transport it uses, and return its raw
    output. The three deterministic legs (uptime, error samples, the health probe) all need exactly
    this and had each grown their own copy of the nostr-vs-local branch."""
    if target.startswith("nostr:"):
        return await node_service.run_agent_over_nostr(target[6:], script, mode="shell") or ""
    job = await node_service.run_to_completion(db, name, target, script,
                                               user_id=admin.id, timeout=timeout)
    return job.output or ""


async def _health_probe(db, admin, name: str, target: str) -> tuple:
    """Measured board rows for a node — the fact-check the agent's summary is corrected against —
    plus the raw probe output, which the shell-only node path reuses instead of running the same
    script a second time.

    Best-effort like _error_samples: on any failure it returns ({}, "") and the board is exactly what
    the model said, which is the old behaviour rather than a broken node."""
    try:
        raw = await _run_probe(db, admin, name, target, _HEALTH_SHELL, timeout=90)
        return _parse_probe(raw), raw
    except Exception as e:
        logger.warning(f"health probe failed for {name}: {e}")
        return {}, ""


async def _error_samples(db, admin, name: str, target: str) -> list:
    """Top journal/dmesg error groups for a node, each with one verbatim sample line.

    Best-effort and independent of the agent: it returns [] on any failure (so the board is never
    blocked by it) and it runs even when the agent leg errored, which is exactly when raw evidence
    is worth the most."""
    try:
        return _parse_error_samples(await _run_probe(db, admin, name, target, _ERROR_SAMPLE_SHELL))
    except Exception as e:
        logger.warning(f"error-sample fetch failed for {name}: {e}")
        return []


def _with_error_samples(board: str, samples: list) -> str:
    """Attach the sample lines directly under the 'Errors (6h)' row, so the evidence sits with the
    count it explains. Appended at the end when there's no such row (e.g. a raw-output fallback body
    or an agent error), rather than dropped."""
    if not samples:
        return board
    block = "\n".join(samples)
    lines = (board or "").splitlines()
    head = f"{_BOARD_ICON['errors']} {_BOARD_LABEL['errors']}"
    for i, line in enumerate(lines):
        if line.startswith(head):
            lines.insert(i + 1, block)
            return "\n".join(lines)
    return f"{(board or '').rstrip()}\n{block}".strip()


async def _node_uptime(db, admin, name: str, target: str) -> str:
    """Best-effort one-line system uptime for a node's report header (e.g. 'up 3 days, 4 hours').
    Read-only + DETERMINISTIC (run directly, not via the agent) so it's always present and accurate.
    Returns '' on any failure so the header just omits it."""
    try:
        cmd = "uptime -p 2>/dev/null || uptime"
        out = (await _run_probe(db, admin, name, target, cmd, timeout=20)).strip().splitlines()
        line = out[0].strip() if out else ""
        # "⚠️ …" (transport failure) and a bare "exit N" (command produced no output) are status, not an
        # uptime — showing either as the header's ⏱️ line is worse than omitting it.
        if not line or line.startswith("⚠️") or re.fullmatch(r"exit -?\d+", line):
            return ""
        return line[:80]
    except Exception as e:
        logger.warning(f"uptime fetch failed for {name}: {e}")
        return ""


async def _to_status_board(chat_service, summary: str, fallback: Optional[str] = None,
                           probe: Optional[dict] = None) -> str:
    """Turn the agent's plain-text summary (or a shell probe's raw output) into the deterministic emoji
    board, with `probe`'s measured rows overriding the model's. On any failure, fall back to
    `fallback` — or the input itself — so a node is never blank. The model still runs even when the
    probe answered every row: its wording on 'errors' is the one thing the probe can't produce, and
    the probe's rows are applied on top of whatever it says."""
    fb = (fallback if fallback is not None else summary).strip()
    try:
        raw = await chat_service.chat([
            {"role": "system", "content": _BOARD_SYS},
            {"role": "user", "content": summary},
        ])
        return _render_board(raw, probe) or fb
    except Exception as e:
        logger.warning(f"status-board formatting failed: {e}")
        return _render_board("", probe) or fb


def get_logs_settings(db=None) -> dict:
    """Read scheduler settings (schedule + node selection) from the DB, with defaults."""
    settings = {"schedule": "1,12,18", "nodes": []}

    schedule_value = settings_store.get("logs_schedule", "")
    if schedule_value:
        settings["schedule"] = schedule_value

    nodes_value = settings_store.get("logs_nodes", "")
    if nodes_value:
        settings["nodes"] = [n.strip() for n in nodes_value.split(",") if n.strip()]

    return settings


# Per-node run state for the report: {"ok": <unix ts of last SUCCESSFUL report>, "attempt": <unix ts of
# last attempt>}. The `_last_runs` suffix deliberately makes this key node-LOCAL (settings_store
# _RUNTIME_SUFFIXES): each node keeps its own schedule position, and hydrating a peer's copy off the
# relay would make one node think another's report was its own.
_RUN_STATE_KEY = "logs_report_last_runs"
_CATCHUP_MAX_AGE = 24 * 3600   # don't resurrect a report older than a day — it'd describe a stale system
_CATCHUP_RETRY_GAP = 1800      # min seconds between attempts, so a crash-restart loop can't hammer it
_CATCHUP_DELAY = 120           # let the relay/LLM finish coming up before a catch-up run


def _run_state() -> dict:
    import json as _json
    try:
        return _json.loads(settings_store.get(_RUN_STATE_KEY, "") or "{}")
    except Exception:
        return {}


def _mark_run(field: str) -> None:
    """Stamp 'attempt' (run started) or 'ok' (report delivered). Best-effort: losing a stamp costs at
    worst one duplicate report, whereas raising here would abort a report that otherwise succeeded."""
    import json as _json
    try:
        st = _run_state()
        st[field] = int(datetime.now().timestamp())
        settings_store.put(_RUN_STATE_KEY, _json.dumps(st))
    except Exception as e:
        logger.debug("could not stamp report run state (%s): %s", field, e)


def _missed_slot(schedule: str) -> Optional[datetime]:
    """The most recent scheduled slot that has already passed, or None if the schedule is unusable.

    Exists because the report takes ~5 minutes to build and a service restart inside that window
    killed it silently, with no retry — an 18:00 report was lost exactly this way, and nothing noticed
    until a human asked where it went. Comparing this against the last SUCCESSFUL run tells us at
    startup whether we owe one."""
    try:
        hours = sorted({int(h) for h in str(schedule).split(",") if h.strip().isdigit()})
    except Exception:
        return None
    if not hours:
        return None
    now = datetime.now()
    today = [now.replace(hour=h, minute=0, second=0, microsecond=0) for h in hours]
    passed = [t for t in today if t <= now]
    if passed:
        return passed[-1]
    # Nothing today yet (e.g. 00:30 with slots 1,12,18) → the last slot of YESTERDAY is the one due.
    return today[-1] - timedelta(days=1)


def _owed_report(schedule: str) -> bool:
    """True when a scheduled report was missed and is still worth running now."""
    slot = _missed_slot(schedule)
    if slot is None:
        return False
    slot_ts = int(slot.timestamp())
    now_ts = int(datetime.now().timestamp())
    if now_ts - slot_ts > _CATCHUP_MAX_AGE:
        return False                                  # too old to be a useful picture of the system
    st = _run_state()
    if int(st.get("ok") or 0) >= slot_ts:
        return False                                  # that slot already produced a report
    if now_ts - int(st.get("attempt") or 0) < _CATCHUP_RETRY_GAP:
        return False                                  # just tried; don't hammer on a restart loop
    return True


async def _catchup_run():
    """One-shot catch-up for a scheduled report that a restart interrupted."""
    if not settings_store.get_bool("logs_scheduler_enabled"):
        return
    logger.info("Health report: running CATCH-UP for a missed/interrupted scheduled run")
    await run_logs_for_admin()


def selected_nodes(db) -> dict:
    """Return {name: target} for the nodes to include in the report. Nostr-only: the shared
    `node_service.all_nodes` registry — synthetic ``local`` (this host, direct) + the npub workers
    (`node_exec_node_npubs`) as ``nostr:<pkhex>``; a self-mapped npub collapses to ``local`` so the
    host is never reported twice. The loop dispatches ``nostr:`` targets over the encrypted channel and
    ``local`` directly. ``logs_nodes`` (if set) narrows by name; empty = all."""
    available = node_service.all_nodes(db)
    chosen = get_logs_settings(db)["nodes"]
    if not chosen:
        return available
    return {name: available[name] for name in chosen if name in available}


def get_or_create_logs_chat(db, user_id: int) -> Conversation:
    """Get the Logs chat for a user, creating it if it doesn't exist."""
    logs_chat = db.query(Conversation).filter(
        Conversation.user_id == user_id,
        Conversation.title == LOGS_CHAT_TITLE,
    ).first()
    if logs_chat:
        return logs_chat

    logs_chat = Conversation(user_id=user_id, title=LOGS_CHAT_TITLE)
    db.add(logs_chat)
    db.commit()
    db.refresh(logs_chat)
    logger.info(f"Created Logs chat for user {user_id}")
    return logs_chat


def _to_telegram_markdown(text: str) -> str:
    """Convert standard markdown to Telegram Markdown v1 format."""
    import re
    # ## Heading → *Heading* (Telegram MarkdownV1 bold). No backslash before the * — MarkdownV1
    # doesn't honour escapes, so a literal "\*" would show up verbatim instead of bolding.
    text = re.sub(r'^#+ (.+)$', r'*\1*', text, flags=re.MULTILINE)
    # **bold** → *bold*
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    # Remove leading spaces from indented lines (Telegram ignores indentation)
    text = re.sub(r'^  +', '', text, flags=re.MULTILINE)
    return text


async def build_health_report(db, admin: User, notify=None) -> str:
    """Run the agentic health check across the selected nodes and return the formatted report.

    `notify`, when given, is an async callback used to stream live progress to the originating
    channel (the interactive `/logs` command passes one; the scheduler does not)."""
    nodes = selected_nodes(db)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    if not node_service.is_enabled(db):
        return (f"## 🩺 System Health Report\n🕒 {timestamp}\n\n"
                "⚠️ Agentic Node Management is disabled — enable it in **Admin → Nodes** to run "
                "the agentic health report.")
    if not nodes:
        return (f"## 🩺 System Health Report\n🕒 {timestamp}\n\n"
                "⚠️ No nodes selected. Configure nodes in **Admin → Nodes → Agentic Node "
                "Management**.")

    chat_service = ChatService(db, admin)
    sections = []
    for name, target in nodes.items():
        _nostr = target.startswith("nostr:")
        where = "this host" if target == "local" else (f"🛰️ nostr:{target[6:18]}…" if _nostr else target)
        if notify:
            try:
                await notify(f"🔍 Checking *{name}* ({where})…")
            except Exception:
                pass
        fallback = None
        # Measured first, and independently of the agent: these are the rows the board is corrected
        # against, and they're also the only rows a node whose agent leg dies still gets.
        probe, probe_raw = await _health_probe(db, admin, name, target)
        try:
            if _nostr:
                summary = await node_service.run_agent_over_nostr(target[6:], _HEALTH_GOAL, mode="agent", report=True)
                # A lightweight standalone worker (no local LLM) can't run agent mode — the same six
                # subsystems come from the read-only shell probe instead. That probe has ALREADY run
                # (it's the fact-check), so reuse its output rather than paying for the whole script
                # a second time; only the raw text (in a code block) is used if distillation fails.
                if summary and "no local LLM" in summary:
                    # …but if the probe came back empty (transient transport failure), retry it here
                    # rather than reporting the node as "(no output)" on the strength of one attempt.
                    raw = (probe_raw or "").strip()
                    if not raw:
                        raw = (await node_service.run_agent_over_nostr(
                            target[6:], _HEALTH_SHELL, mode="shell") or "").strip()
                        probe = probe or _parse_probe(raw)
                    summary = raw[:4000]
                    fallback = f"```\n{raw[:2500] or '(no output)'}\n```"
            else:
                summary = await node_service.run_agent(
                    db, admin, name, target, _HEALTH_GOAL, chat_service,
                    notify=notify, report_mode=True,
                )
            # Presentation is deterministic (Python owns emojis/layout) so the agent model's
            # formatting drift never reaches the report.
            body = await _to_status_board(chat_service, summary or "", fallback=fallback, probe=probe)
        except Exception as e:
            logger.error(f"Health check failed for node {name}: {e}")
            # The probe is a separate leg, so a dead agent costs the prose, not the whole node: show
            # the measured board and say the agent failed, rather than an error line by itself.
            measured = _render_board("", probe)
            body = f"{measured}\n⚠️ agent error: {e}" if measured else f"⚠️ agent error: {e}"
        # The counts on the errors row are a summary, not evidence — attach the actual top log lines
        # so the row can be acted on without opening a shell on the node.
        body = _with_error_samples(body, await _error_samples(db, admin, name, target))
        uptime = await _node_uptime(db, admin, name, target)
        header = f"━━━━━━━━━━━━━━\n🖥️ *{name}*  ·  `{where}`"
        if uptime:
            header += f"\n⏱️ {uptime}"
        sections.append(f"{header}\n\n{(body or '').strip()}")

    body = "\n\n".join(sections)
    return f"## 🩺 System Health Report\n🕒 {timestamp}\n\n{body}"


async def run_logs_for_admin(return_text: bool = False, notify=None,
                             deliver_telegram: bool = True) -> Optional[str]:
    """Build the health report and store it in the admin's Logs conversation + Telegram.

    Returns the report text when `return_text` is True (used by the interactive `/logs` command),
    otherwise None (the scheduler / admin trigger ignore the return).

    `deliver_telegram` pushes the report to the admin's Telegram. The scheduler and admin trigger
    want this; the interactive `/logs` command sets it False because its return value is already
    posted back to whatever channel invoked it (so it'd otherwise arrive twice on Telegram)."""
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.id == 1).first()
        if not admin:
            logger.warning("Admin user not found")
            return None

        logger.info("Building agentic system health report...")
        # Stamp the ATTEMPT before the multi-minute build: if a restart (or a crash) kills us partway,
        # the startup catch-up sees an attempt with no matching success and knows a report is owed —
        # while the gap check stops a restart loop from rebuilding it over and over.
        _mark_run("attempt")
        message_text = await build_health_report(db, admin, notify=notify)

        # build_health_report's per-node agent diagnostics can run for minutes, idling THIS transaction
        # past Postgres' idle_in_transaction_session_timeout (60s). The connection is then dead and the
        # very next query (the "Logs" conversation lookup) throws "server closed the connection
        # unexpectedly". Do the SAVE on a guaranteed-fresh session so the write can't hit a dead conn.
        try:
            db.close()
        except Exception:
            pass
        db = SessionLocal()
        admin = db.query(User).filter(User.id == 1).first()
        if not admin:
            logger.warning("Admin user not found after report build")
            return message_text if return_text else None

        # Store in the admin's Logs conversation
        logs_chat = get_or_create_logs_chat(db, admin.id)
        from app.services import chat_history
        await chat_history.append(db, admin, logs_chat.id, "assistant", message_text)   # encrypted event
        logs_chat.updated_at = datetime.utcnow()
        db.commit()
        logger.info("Added health report to Logs chat for admin")
        # The Logs conversation is created directly here (not via the API that normally mirrors its
        # index doc), so mirror it so the relay is consistent for a fresh-node rebuild. The report
        # MESSAGE is mirrored by the Message after_commit hook (this runs in the scheduler's async
        # loop, so the hook fires); the client shows it from PG via the API regardless.
        try:
            from app.services import chat_store
            await chat_store.mirror_conversation(db, admin, logs_chat)
        except Exception as e:
            logger.warning(f"Logs conversation relay mirror failed: {e}")

        # Send to Telegram if the admin has it enabled (suppressed for the interactive command,
        # whose return value is already delivered to the invoking channel).
        if deliver_telegram and admin.telegram_enabled and admin.telegram_chat_id:
            from app.services.telegram_service import telegram_service, configure_from_settings
            try:
                token = settings_store.get("telegram_bot_token", "")
                if token:
                    telegram_service.set_token(token)
                configure_from_settings(db)
                await telegram_service.send_message(
                    admin.telegram_chat_id,
                    _to_telegram_markdown(message_text),
                )
                logger.info(f"Sent health report to Telegram for admin user {admin.id}")
            except Exception as tg_err:
                logger.error(f"Failed to send health report to Telegram: {tg_err}")

        # Delivered (it's in the Logs conversation; a failed Telegram push is caught above and doesn't
        # un-deliver it). Stamping success here is what stops the next startup from filing a duplicate.
        _mark_run("ok")
        return message_text if return_text else None

    except Exception as e:
        logger.error(f"Error in health report: {e}")
        # The error may itself be a dead connection (idle-timeout during the long build) — a rollback
        # on it would raise again and mask the real error, so guard it.
        try:
            db.rollback()
        except Exception:
            pass
        return f"⚠️ Error generating health report: {e}" if return_text else None
    finally:
        try:
            db.close()
        except Exception:
            pass


async def check_and_run_logs():
    """Scheduler entry point: run the report only if the scheduler is enabled."""
    if not settings_store.get_bool("logs_scheduler_enabled"):
        logger.debug("Logs scheduler disabled")
        return

    await run_logs_for_admin()


def start_logs_scheduler():
    """Start the logs scheduler."""
    global logs_scheduler

    if logs_scheduler is not None:
        logger.warning("Logs scheduler already running")
        return

    if not settings_store.get_bool("logs_scheduler_enabled"):
        logger.info("Logs scheduler disabled")
        return
    schedule = get_logs_settings()["schedule"]

    logs_scheduler = AsyncIOScheduler()
    logs_scheduler.add_job(
        check_and_run_logs,
        CronTrigger(hour=schedule, minute="0"),
        id="logs_scheduler",
        name="System Health Report",
        replace_existing=True,
    )
    # Catch-up: a report that a restart interrupted (or that this node was down for) is re-run shortly
    # after startup instead of being silently skipped until the next slot — up to ~11 hours away on the
    # default 1,12,18 schedule. Delayed rather than inline so the relay/LLM are up first, and gated by
    # _owed_report so it can't duplicate a report that already landed or spin on a restart loop.
    if _owed_report(schedule):
        logs_scheduler.add_job(
            _catchup_run,
            "date",
            run_date=datetime.now() + timedelta(seconds=_CATCHUP_DELAY),
            id="logs_scheduler_catchup",
            name="System Health Report (catch-up)",
            replace_existing=True,
        )
        logger.info("Health report: a scheduled run was missed — catch-up queued in %ds", _CATCHUP_DELAY)

    logs_scheduler.start()
    logger.info(f"Logs scheduler started - running at hours: {schedule}")


def stop_logs_scheduler():
    """Stop the logs scheduler."""
    global logs_scheduler

    if logs_scheduler is not None:
        logs_scheduler.shutdown()
        logs_scheduler = None
        logger.info("Logs scheduler stopped")
