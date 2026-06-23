# Your GPU, Their Prompt, Nobody's Cloud: Distributed AI over Nostr

What if the GPU sitting idle in your closet could earn its keep answering a stranger's prompt — no AWS account, no API gateway, no SaaS dashboard, just an `npub` and a relay? And what if, when *your* rig is busy, your request quietly hopped over to a friend's machine across town, ran there, and streamed the answer back — all of it signed, encrypted, and routed over the same protocol you already use to shitpost? That's not a roadmap slide. It's running right now on the **PosterChanAI** stack, and this post shows you exactly how it works and how to wire it up yourself.

If you self-host, you already know the pitch for owning your inference: no token metering, no content filters you didn't write, no provider deciding your model is "deprecated" on a Tuesday. The catch has always been **scale**. One box, one GPU, one job at a time. PosterChanAI's answer is to make compute **federate the way Nostr notes already do** — by identity, not by IP, with trust you grant explicitly and revoke instantly.

---

## The stack in one breath

PosterChanAI is a single self-hosted FastAPI app: an OpenAI-compatible `/v1/` endpoint, image / music / video generation, TTS/STT, a file manager, Telegram + Matrix + fediverse bots — and a **built-in Nostr relay** that doubles as the app's own datastore. That last part is the keystone. Because every node already runs a relay (its identity, its web-of-trust, its event store), the network layer for distributed compute was *already there*. We didn't bolt on a message bus. We used the one you're already federating with.

Two distribution layers, cleanly separated:

- **Within your own machines** → a plain **IP load balancer**. Round-robin across `chat_server_urls`, shared GPU lock, busy-aware. Boring, battle-tested, fast.
- **Across owners** → **Nostr**. Your node addresses a *peer's npub*, the job rides the relay graph, the peer serves it on its own hardware and signs the result back.

The golden rule that keeps it simple: **Nostr is the boundary between owners; the IP LB distributes within one owner.** A provider with three GPUs of its own doesn't re-publish jobs to itself over Nostr — it hands the incoming job straight to its own load balancer. More on that below, because it's the trick that lets one entry node fan a single request across a whole cluster.

---

## How a job actually moves

It's a NIP-90-style Data Vending Machine, deliberately stripped to "listen for events from a trusted npub, run them, return." No new key to manage — **a node's compute identity *is* its relay identity**, the operator nsec it already signs its datastore with.

A request from npub **B** to provider **A** looks like this:

1. **B** builds a job event (a NIP-90 request kind), `p`-tagged to **A**'s npub, with the payload **NIP-44-encrypted to A**. Signs it with B's key.
2. **B** publishes it to **A's relay** — the rendezvous.
3. **A's relay** accepts the write (B is on A's allowlist — see below), so **A's worker**, subscribed for jobs addressed to it, picks it up.
4. **A** runs the job on its hardware and publishes an **encrypted result event** back, `p`-tagged to B.
5. **B** awaits the result, decrypts it, done.

Everything in flight is ciphertext to everyone but the two parties. The result is signature-verified before it's ever used. Job traffic is tagged `nofederate` so it never leaks out to public relays — it stays strictly between the cluster's relays.

---

## Sharing your machine: trust you grant, not trust you inherit

Here's the part that matters for a *network* of strangers' boxes: **sharing your GPU must be a deliberate grant, never a side effect of a social follow.** If "anyone in my web of trust" could spend my VRAM, the first viral thread would melt my card.

So PosterChanAI uses one explicit, mutual **peer list** — a set of connection cards, one per line:

```
npub1friend… wss://their-relay.example/relay
npub1other…  wss://other.example/relay
```

List a peer and the relationship is **symmetric**: they may send jobs to *your* machine **and** you may offload to *theirs*. One setting drives three things at once:

- **the trust gate** — your worker serves jobs *only* from npubs on this list;
- **the relay's write-gate** — your relay auto-accepts those npubs' job events even though they aren't in your social web of trust (a follow grants you nothing here);
- **the offload set** — the same peers are who *you* can round-robin your own work to.

Blank list = you share with nobody. No accidental exposure, ever. Add a friend's card, restart, and you're a two-way compute co-op. Remove it, and access is gone on the next restart.

---

## The load-balancer trick: one npub in, a whole cluster lit up

This is the bit engineers will appreciate. Say an outside node — call it the **edge** — wants to use your cluster. It doesn't need to know your topology. It sends *one* job to your **entry node** (think `ai.poster.place`) over Nostr. Your entry node doesn't pin that job to its own GPU. It treats it like any local request and hands it to **its own IP load balancer**, which spreads it across every box you run — *without* re-dispatching back over Nostr (that would loop). The peer that picks up the forwarded HTTP request just sees a normal request; it has no idea Nostr was ever involved.

The net effect, verified end to end with a fresh Docker node firing image jobs at a two-box cluster:

```
edge ──(Nostr/DVM)──► entry node ──(IP LB / HTTP)──► entry node  (ran locally)
                                  └────────────────► peer node   (POST /api/generate-image 200 OK)
```

Three image requests, one entry npub, fanned across two GPUs, results Blossom-transferred home. Chat, image, music, and video all ride the identical path — music even forwarded to a second node's ACE-Step backend and came back as a branded MP4. **One identity to address; an entire cluster to absorb the load.**

For self-hosters this means you can scale horizontally by *adding boxes and listing each other* — no orchestrator, no service mesh, no shared filesystem. For a small group of friends, it means pooling GPUs so nobody has to own the biggest card.

---

## Blossom: beating Nostr's 256 KB ceiling for real context

Relays cap event size (256 KB here). Modern LLM work laughs at that — a near-full agentic context with accumulated tool outputs and pasted files is easily **300 KB+**. If your distributed-AI layer can only move tiny payloads, it's a toy.

PosterChanAI's fix is **transparent Blossom spillover**. Small payloads ride inline as NIP-44 ciphertext, exactly as you'd expect. But the moment a request *or* a result crosses the size threshold, the node:

1. AES-256-GCM-encrypts the payload,
2. uploads the ciphertext to a **Blossom** server,
3. sends only the tiny hash-reference over Nostr.

The receiver fetches the blob, verifies the hash, decrypts, and runs the job as if it had arrived inline. The per-blob key rides *inside* the NIP-44-encrypted event, so a Blossom URL alone is useless to anyone watching. And the ref is **self-describing** — it carries the uploader's Blossom URL — so a peer connection card needs nothing more than `npub relay`; media discovery is automatic and works across owners.

The test that matters: a **286 KB chat context** — which the relay *would reject inline as "message too large"* — round-tripped over the cluster and came back with the model's answer. The only way that succeeds is the spill. Large context over Nostr isn't a caveat in the docs; it's a passing test.

---

## Plug it into your existing LB

You don't have to choose between "IP load balancing" and "Nostr sharing." They compose:

- Keep your local cluster exactly as-is: `chat_server_urls` lists your own boxes, the IP LB round-robins them with a shared GPU lock.
- Turn on shared compute and add a peer card or two.
- Now your nodes have **two tiers of capacity**: your own machines (HTTP, low-latency) *and* your peers' machines (Nostr, cross-owner). Your own users are unaffected — they're still served locally or via your IP LB; sharing only *adds* peer capacity, in both directions.

A node that's purely a worker never even needs to know Nostr exists — it just answers HTTP. A node that's the front door speaks DVM on the way in and IP LB on the way out. Mix and match per box.

---

## Now make it pay: Nostr-native, Lightning-settled inference

Here's where this stops being a hobbyist curiosity and starts being a **business model you fully own**. NIP-90 DVMs and Lightning were practically designed to meet here:

- A consumer publishes a job. Your provider node replies with a **payment-required** event quoting a price (per token, per image, per second of audio — your call) and a **Lightning invoice** (BOLT11, or a Lightning Address / LNURL).
- The consumer pays — a zap-like flow Nostr clients already understand — and your node, on seeing the payment, runs the job and returns the (encrypted) result.
- No Stripe, no chargebacks, no merchant account, no KYC funnel. **A keypair and a Lightning node.** Settlement is instant and final; the unit of account is sats, the unit of trust is an npub.

Picture the shapes this unlocks: a **GPU co-op** where members meter each other and net out in sats monthly; a **public image/chat DVM** that anyone on Nostr can hit and pay per call; a **burst-capacity marketplace** where your idle overnight GPU rents itself to whoever's awake. Because trust is an explicit allowlist and billing is a Lightning invoice, you decide exactly who gets free access (friends), who pays (the public), and what it costs — and you change your mind by editing one list.

The plumbing that makes paid service safe is already in place: encrypted payloads, signature-verified results, per-npub trust, Blossom for big artifacts, and a fast-failover design so a dead or non-paying peer just drops you back to local instead of hanging. Bolting an invoice onto the request/response handshake is the natural next turn of the crank — not a rewrite.

---

## Why this is the right shape

- **Identity over IP.** You address compute by `npub`. Machines move, IPs churn, the identity is stable and yours.
- **Trust you control.** An explicit, mutual, instantly-revocable allowlist — separate from your social graph.
- **No new infrastructure.** The relay you already run *is* the message bus. The Blossom you already use for media *is* the large-payload channel. The Lightning node you might already run *is* the payment rail.
- **It composes with what you have.** Drop it next to your IP load balancer and your cluster grows a second, cross-owner tier.
- **It's verified, not vapor.** Chat, image, music, the 286 KB Blossom spill, the fan-out across a real two-node cluster from a fresh outside box — all tested end to end.

Self-hosting AI was always about *sovereignty*. Distributed AI over Nostr is about **sovereignty that scales** — your keys, your GPUs, your prices, your peers, federating the only way that's ever actually worked for Nostr: by identity, over relays, with money that settles in seconds.

Spin up a node. Trade a peer card with a friend. Watch a prompt you didn't write light up a GPU you'll never touch — and, if you like, drop an invoice on the way out.

*Built on the PosterChanAI stack — self-hosted FastAPI, a built-in web-of-trust Nostr relay, Blossom media, and an OpenAI-compatible API. `npub` in, inference out.*
