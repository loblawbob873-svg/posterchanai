"""Long image prompts reach the model in full.

The text encoders read 77 tokens (75 words-ish plus start/end), and a longer prompt was cut twice:
first by our own 300-character trim, then by diffusers at 77 tokens ("The following part of your
input was truncated because CLIP can only handle sequences up to 77 tokens"). Whatever came last --
a background, a pose, "standing upright" -- simply did nothing (the dance frames found this out).

So a prompt over 75 tokens is encoded in 75-token chunks, each wrapped in its own start/end tokens and
padded to 77, run through every text encoder the pipeline has, and the chunks are joined along the
sequence. The negative prompt is encoded the same way and padded with empty chunks to the same length
(the two must match). SDXL's pooled embedding comes from the first chunk, as it would for a short
prompt. A prompt that fits is passed through as text, exactly as before -- this changes nothing for it.

Shared by the in-process generator (diffusers_service) and the subprocess one
(scripts/generate_image_subprocess.py), like image_pose.py, so the two cannot drift.
"""
from __future__ import annotations

CHUNK = 75


def _encoders(pipe):
    out = []
    for tk, te in (("tokenizer", "text_encoder"), ("tokenizer_2", "text_encoder_2")):
        tok, enc = getattr(pipe, tk, None), getattr(pipe, te, None)
        if tok is not None and enc is not None:
            out.append((tok, enc))
    return out


def _ids(tok, text):
    return tok(text or "", truncation=False, add_special_tokens=False).input_ids


def _chunks(tok, ids, n):
    """`n` chunks of [bos] + 75 ids + [eos], padded to 77."""
    bos, eos = tok.bos_token_id, tok.eos_token_id
    pad = tok.pad_token_id if tok.pad_token_id is not None else eos
    out = []
    for i in range(n):
        part = list(ids[i * CHUNK:(i + 1) * CHUNK])
        row = [bos] + part + [eos]
        out.append(row + [pad] * (CHUNK + 2 - len(row)))
    return out


def needs_chunks(pipe, prompt: str, negative: str = "") -> bool:
    return any(len(_ids(tok, t)) > CHUNK for tok, _e in _encoders(pipe) for t in (prompt, negative))


def _encode(pipe, text_chunks_by_encoder, device):
    import torch
    hidden, pooled = [], None
    sdxl = len(text_chunks_by_encoder) == 2
    for (tok, enc), rows in text_chunks_by_encoder:
        per = []
        for row in rows:
            ids = torch.tensor([row], device=device)
            out = enc(ids, output_hidden_states=True)
            if sdxl:
                per.append(out.hidden_states[-2])
                if enc is getattr(pipe, "text_encoder_2", None) and pooled is None:
                    pooled = out[0]                      # the projection of the FIRST chunk
            else:
                per.append(out[0])                       # SD1.5: the last hidden state
        hidden.append(torch.cat(per, dim=1))
    return torch.cat(hidden, dim=-1), pooled


def prompt_kwargs(pipe, prompt: str, negative: str, device=None) -> dict:
    """What to pass to `pipe(...)` for the prompt: the text itself when it fits, else full embeddings."""
    encs = _encoders(pipe)
    if not encs or not needs_chunks(pipe, prompt, negative):
        return {"prompt": prompt, "negative_prompt": negative}
    import torch
    device = device or getattr(pipe, "_execution_device", None) or getattr(pipe, "device", "cpu")
    n = max(1, max(-(-len(_ids(tok, t)) // CHUNK) for tok, _e in encs for t in (prompt, negative)))
    with torch.no_grad():
        pos, pos_pool = _encode(pipe, [((t, e), _chunks(t, _ids(t, prompt), n)) for t, e in encs], device)
        neg, neg_pool = _encode(pipe, [((t, e), _chunks(t, _ids(t, negative), n)) for t, e in encs], device)
    dtype = getattr(encs[-1][1], "dtype", None)
    if dtype is not None:
        pos, neg = pos.to(dtype), neg.to(dtype)
    out = {"prompt_embeds": pos, "negative_prompt_embeds": neg}
    if pos_pool is not None:
        out["pooled_prompt_embeds"] = pos_pool.to(pos.dtype)
        out["negative_pooled_prompt_embeds"] = neg_pool.to(pos.dtype)
    return out
