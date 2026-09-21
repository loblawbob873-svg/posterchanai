"""THE SAME PUBLIC NOTE, SIGNED TWICE, IS HOW AMETHYST DECIDES SOMEBODY IS A SPAMMER.

Amethyst's AntiSpamFilter hashes content+tags (not the author) of every note it sees; a second event
with the same hash and a different id counts as spam, and a few of them hide the author. Measured on
server1's relay (2026-09-21), from PosterChan clients:

  * "🤖 start #chess against the bot — …" with identical tags, 7 times in 131 s from one account
    (and the tictactoe start twice from another) — clicks while the first publish was in flight;
  * NIP-88 poll responses (kind 1018): 8 identical in 10 s from one account, 4 in 4 s from another —
    `votePoll` only records "already voted" after the relay answers.

These run the SHIPPED functions under node with a stub `publish` that takes a while to answer, and
fail on the pre-fix tree.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "static" / "js" / "client"


def _slice(src, start, end):
    a = src.index(start)
    return src[a:src.index(end, a + 1)]


def _node(script):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


HARNESS = r"""
const published=[];const toasts=[];
const toast=m=>toasts.push(m);
const publish=async(kind,content,tags)=>{published.push({kind,content,tags});await new Promise(r=>setTimeout(r,50));return {ok:true};};
const safePk=v=>v;const PC={CFG:{chess_bot_npub:'b'.repeat(64),ttt_bot_npub:'b'.repeat(64),connect4_bot_npub:'b'.repeat(64)},VIEW:'x'};
const $=()=>null,renderChess=()=>{},render=()=>{};
const _myPollVotes={};
"""


@pytest.mark.parametrize("fname,start,end,fn", [
    ("chess.js", "    let _botStartAt=0;", "    function _bindChessInvite(", "startBotGame"),
    ("ttt.js", "    let _botStartAt=0;", "    async function startGame(", "startBot"),
    ("connect4.js", "    let _botStartAt=0;", "    async function startGame(", "startBot"),
])
def test_a_game_start_is_one_note_and_two_games_are_two_different_notes(fname, start, end, fn):
    body = _slice((JS / fname).read_text(encoding="utf-8"), start, end)
    out = _node(HARNESS + body + f"""
(async()=>{{
  await Promise.all([{fn}(),{fn}(),{fn}(),{fn}()]);      // an impatient person clicking
  const burst=published.length;
  _botStartAt=0;                                          // a later, real second game
  await {fn}();
  console.log(JSON.stringify({{burst,notes:published.map(p=>p.content+JSON.stringify(p.tags))}}));
}})();""")
    assert out["burst"] == 1, "clicking Play vs bot four times signed %d identical public notes" % out["burst"]
    assert len(out["notes"]) == 2 and out["notes"][0] != out["notes"][1], \
        "two separate games published byte-identical content+tags (the Amethyst spam hash)"


def test_a_poll_vote_in_flight_is_not_sent_again():
    src = (JS / "app.js").read_text(encoding="utf-8")
    body = _slice(src, "  const _pollVoting = new Set();", "  // Pull media URLs OUT of the text")
    out = _node(HARNESS + "const ME={pubkey:'c'.repeat(64)};const $$=()=>[];" + body + """
(async()=>{
  await Promise.all([votePoll('p','a'),votePoll('p','a'),votePoll('p','a')]);
  await votePoll('p','a');                                 // after it landed: "already voted"
  console.log(JSON.stringify({n:published.length}));
})();""")
    assert out["n"] == 1, "three quick clicks on one poll option signed %d identical 1018s" % out["n"]
