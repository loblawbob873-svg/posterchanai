# Per-user Monero Send review

Integrated the other agent's committed Send button (0c6422015), which uses the
existing authenticated `/api/wallet/xmr/me/pay` route. Review found three money
handling bugs before this release: changing the acknowledgement checkbox could
re-enable a pending/unknown payment; a fast disconnect or gateway error invited
a retry; and a malformed successful response was called a completed payment.

The confirmation now has explicit ready/sending/unknown/sent states. Checkbox
changes cannot bypass the sending or terminal states. Once `/me/pay` is
dispatched, transport failures and HTTP 5xx responses are reported as uncertain,
with instructions to check history. Malformed successful responses and missing
transaction hashes are also uncertain. Success requires a nonempty list of
64-character hexadecimal transaction hashes. Known validation refusals remain
retryable. No automatic payment retries were added.

Amounts remain exact decimal strings in the request, with at most 12 decimal
places. The confirmation explains that up to the configured service percentage
may be deducted and that the miner fee is additional. Backend validation remains
authoritative for balance, address and fee applicability.

Validation: 15 driven runtime cases using the actual entire wallet module and
stubbed network passed, including pending/unknown checkbox changes, disconnects,
502/504, malformed/missing receipts, explicit 400 refusal, minimum atomic value,
exact decimal transmission, invalid/over-balance inputs and fee disclosure.
Four ambiguity cases failed against the original committed module. The existing
39 per-user source and wallet send-flow checks also passed. No real payments
were sent during these tests. Independent review confirmed the blockers closed.
