"""Long image prompts reach the model in full (app/services/long_prompt.py).

A prompt was cut at 300 characters by us and again at 77 tokens by the text encoder, so whatever came
last -- the background, the pose, "standing upright" -- did nothing (measured today in the log:
"truncated because CLIP can only handle sequences up to 77 tokens: [', standing upright']"). Runs the
shipped helper against a fake SDXL pipeline whose encoders record which token ids they were shown.
"""
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
from app.services import long_prompt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BOS, EOS = 49406, 49407


class Tok:
    bos_token_id, eos_token_id, pad_token_id = BOS, EOS, EOS

    def __call__(self, text, truncation=False, add_special_tokens=False):
        class R:
            pass
        r = R()
        r.input_ids = [100 + i for i, _w in enumerate(text.split())]   # one id per word, in order
        return r


class Out(tuple):
    pass


class Enc:
    def __init__(self, width, pooled):
        self.width, self.pooled, self.seen, self.dtype = width, pooled, [], torch.float32

    def __call__(self, ids, output_hidden_states=True):
        self.seen.append(ids[0].tolist())
        h = ids.float().unsqueeze(-1).repeat(1, 1, self.width)          # each position carries its id
        out = Out((torch.full((1, 4), float(ids[0, 1])) if self.pooled else h,))
        out.hidden_states = [h, h, h]
        return out


class Pipe:
    def __init__(self):
        self.tokenizer, self.tokenizer_2 = Tok(), Tok()
        self.text_encoder, self.text_encoder_2 = Enc(3, False), Enc(5, True)
        self._execution_device = "cpu"


def test_a_prompt_that_fits_is_passed_as_text_exactly_as_before():
    p = Pipe()
    assert long_prompt.prompt_kwargs(p, "a cat on a mat", "blurry") == {"prompt": "a cat on a mat", "negative_prompt": "blurry"}


def test_every_word_of_a_long_prompt_reaches_both_encoders():
    p = Pipe()
    prompt = " ".join(f"w{i}" for i in range(160))          # 160 tokens: three chunks
    kw = long_prompt.prompt_kwargs(p, prompt, "blurry")
    assert kw["prompt_embeds"].shape == (1, 77 * 3, 3 + 5), kw["prompt_embeds"].shape
    assert kw["negative_prompt_embeds"].shape == kw["prompt_embeds"].shape, "negative must match the prompt's length"
    seen = [i for row in p.text_encoder.seen[:3] for i in row if i not in (BOS, EOS)]
    assert seen == list(range(100, 260)), "a word was dropped or reordered"
    assert p.text_encoder_2.seen[:3] == p.text_encoder.seen[:3], "the second encoder did not see the whole prompt"
    # The tail ("standing upright" in real life) is in the last chunk the model sees.
    assert 259 in p.text_encoder.seen[2]
    # SDXL's pooled vector is the first chunk's, as for a short prompt.
    assert float(kw["pooled_prompt_embeds"][0, 0]) == 100.0


def test_no_generator_cuts_the_prompt_any_more():
    svc = (ROOT / "app/services/diffusers_service.py").read_text()
    sub = (ROOT / "scripts/generate_image_subprocess.py").read_text()
    assert "_truncate_prompt" not in svc, "the 300-character cut is back"
    for src in (svc, sub):
        assert "long_prompt.prompt_kwargs(pipe, prompt, negative_prompt)" in src
