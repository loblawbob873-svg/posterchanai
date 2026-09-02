#!/usr/bin/env python3
"""KEEP THE TIPPING WALLET ABLE TO TIP.

Monero spends whole OUTPUTS and locks the change for 10 blocks. A wallet whose balance sits in one
output can send once — a batch of up to 15 people, which is plenty — and then nothing at all for
~20 minutes. `sweep_all` to the wallet's own address with `outputs: N` turns the balance into N
independently spendable outputs, so N sends can follow each other before anything has to wait.

WHAT WAS WRONG WITH THE FIRST VERSION, AND IT IS THE WHOLE POINT OF THIS FILE.

It acted only when `num_unspent_outputs == 1`, which reads as "the wallet has collapsed to a single
output". It does not mean that. `num_unspent_outputs` counts UNSPENT outputs, and an output that is
unspent but still inside its 10-block lock is counted there while being unspendable. Measured on the
live wallet, minutes after the user reported having to wait again:

    outputs total   : 3          <- what the old rule looked at, and called healthy
    outputs UNLOCKED: 1          <- what could actually be spent
    0.049000000 XMR  unlocked=False   (a deposit that had just arrived)
    0.000330158 XMR  unlocked=False   (change from the previous tip)
    0.000052183 XMR  unlocked=True

and the timer logged `outputs=3: nothing to do`. So the maintainer stood down in precisely the state
it exists to repair: one spendable output, which the next tip consumes, whose change then locks —
leaving zero. The user hits "local wallet unlocks in ~14 min", the wallet is nominally full, and the
log says everything is fine. It could only ever have fired at `outs == 1`, a state that already
means the next tip is blocked, and never while there was still something to split.

So the count that matters is SPENDABLE outputs, and the threshold is a low-water mark rather than
the target: topping back up to N after every payment would re-split constantly and pay a fee each
time, which is what the original narrowness was guarding against. Splitting when the spendable count
falls BELOW HALF the target keeps that guard — roughly one split per four or five tips — while never
letting the wallet reach the blocked state.

`sweep_all` moves only unlocked funds, so this cannot disturb a locked deposit; it simply splits
whatever is spendable right now. The split's own outputs lock for 10 blocks, which is why this runs
on a timer and never in the tipping path: a person waiting to send must never wait on this.
"""
import json, os, subprocess, sys

URL = os.environ.get("XMR_RPC", "http://127.0.0.1:38083/json_rpc")
USER = os.environ.get("XMR_USER", "")
PASS = os.environ.get("XMR_PASS", "")
TARGET = int(os.environ.get("XMR_OUTPUTS", "8"))
#: Total unlocked floor. Below this a split is all fee and no benefit.
MIN_ATOMIC = int(os.environ.get("XMR_MIN_ATOMIC", str(2 * 10**9)))
#: Each resulting output has to be worth spending; otherwise fewer, larger ones are better.
MIN_OUT_ATOMIC = int(os.environ.get("XMR_MIN_OUT_ATOMIC", str(10**9)))
#: Act before the wallet is blocked, not once it already is.
LOW_WATER = int(os.environ.get("XMR_LOW_WATER", str(max(2, TARGET // 2))))


def decide(spendable_count, unlocked_atomic, target=TARGET, low_water=LOW_WATER,
           min_atomic=MIN_ATOMIC, min_out=MIN_OUT_ATOMIC):
    """(should_split, outputs, reason) — pure, so the rule can be tested without a wallet.

    Kept separate from the RPC on purpose: every bug this script has had was in the decision, and a
    decision buried in an I/O function can only be checked by running it against a real wallet."""
    if spendable_count >= low_water:
        return False, 0, (f"{spendable_count} spendable outputs (low-water {low_water}) — "
                          f"nothing to do")
    if unlocked_atomic < min_atomic:
        return False, 0, (f"{spendable_count} spendable but only "
                          f"{unlocked_atomic / 1e12:.12f} XMR unlocked — waiting")
    # Never mint dust: with little unlocked, fewer and larger outputs are worth more than N tiny
    # ones, each of which costs its own input in a later transaction.
    n = max(2, min(target, unlocked_atomic // min_out))
    return True, int(n), (f"{spendable_count} spendable (low-water {low_water}), "
                          f"{unlocked_atomic / 1e12:.12f} XMR unlocked — splitting into {n}")


def rpc(method, params=None):
    """curl --digest rather than a hand-rolled urllib digest handler.

    The first version used urllib's HTTPDigestAuthHandler and got a flat 401 against a wallet that
    answers curl perfectly — an authentication detail is not worth debugging when the machine
    already has a client that works."""
    body = json.dumps({"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}})
    out = subprocess.run(
        ["curl", "-s", "--max-time", "120", "--digest", "-u", f"{USER}:{PASS}",
         URL, "-H", "Content-Type: application/json", "-d", body],
        capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        raise SystemExit(f"wallet rpc unreachable: {out.stderr.strip()[:120]}")
    try:
        return json.loads(out.stdout)
    except Exception:
        raise SystemExit(f"wallet rpc gave no JSON: {out.stdout[:120]}")


def spendable(account_index=0):
    """Outputs that can be spent RIGHT NOW.

    `get_balance`'s `num_unspent_outputs` is not this number — it counts locked outputs too, which
    is the bug in the module docstring. `incoming_transfers` reports `unlocked` per output, so it
    is the only honest source. A wallet that does not report the flag at all must not be read as
    'everything is spendable': that would put the old bug back with more steps, so fall back to the
    unlocked BALANCE, which every version reports."""
    got = (rpc("incoming_transfers", {"transfer_type": "available",
                                      "account_index": account_index}).get("result") or {})
    transfers = got.get("transfers") or []
    if transfers and "unlocked" in transfers[0]:
        return sum(1 for t in transfers if t.get("unlocked")), True
    return 0, False


def main():
    bal = (rpc("get_balance", {"account_index": 0}).get("result") or {})
    unlocked = int(bal.get("unlocked_balance") or 0)
    count, measured = spendable()
    if not measured:
        # No per-output lock flag. One unlocked balance and nothing to say how it is divided; treat
        # a wallet with anything unlocked as having one usable output, which is the safe reading.
        count = 1 if unlocked > 0 else 0

    go, outputs, why = decide(count, unlocked)
    print(why)
    if not go:
        return 0

    own = (rpc("get_address", {"account_index": 0}).get("result") or {}).get("address") or ""
    if not own:
        print("no address from the wallet; refusing to sweep", file=sys.stderr)
        return 1

    got = rpc("sweep_all", {"address": own, "account_index": 0, "outputs": outputs,
                            "priority": 1, "get_tx_keys": False})
    if "error" in got:
        print("sweep refused:", got["error"].get("message"), file=sys.stderr)
        return 1
    res = got.get("result") or {}
    fee = sum(res.get("fee_list") or [])
    print(f"split into {outputs} outputs; fee {fee / 1e12:.12f} XMR; "
          f"tx {len(res.get('tx_hash_list') or [])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
