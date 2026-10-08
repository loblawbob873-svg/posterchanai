"""`woodchipper`: the machine stands bottom-centre under the image and says "Get in!".

Asked for: "new effect: woodchipper add that to bottom center, with the text 'Get in!'". Rendered for
real (the pointing-meme renderer, like `would`), and reachable from every surface an effect has to be
on -- the chat command, Telegram's effects menu and the Meme Builder's layer catalogue.
"""
import asyncio
import io

from PIL import Image

from app.services.effects_service import add_woodchipper, _character_path


def _photo(w=1024, h=768):
    b = io.BytesIO()
    Image.new("RGB", (w, h), (70, 110, 160)).save(b, "JPEG")
    return b.getvalue()


def test_the_art_is_a_transparent_cutout_that_resolves():
    p = _character_path("woodchipper")
    assert p and p.endswith("woodchipper.png")
    with Image.open(p) as im:
        assert im.mode == "RGBA" and im.getpixel((0, 0))[3] == 0, "not a cut-out: it would paste a box"
    assert _character_path("chipper") == p


def test_it_stands_bottom_centre_and_says_get_in():
    with Image.open(io.BytesIO(add_woodchipper(_photo()))) as out:
        out = out.convert("RGB")
        W, H = out.size
        bg = (70, 110, 160)
        changed = lambda x, y: sum(abs(a - b) for a, b in zip(out.getpixel((x, y)), bg)) > 60
        # The machine: the bottom-centre is covered, the top corners are not.
        box = [(x, y) for x in range(int(W * .36), int(W * .64), 6) for y in range(int(H * .55), H, 6)]
        covered = sum(1 for x, y in box if changed(x, y)) / len(box)
        assert covered > .6, ("no woodchipper at the bottom centre", covered)
        side = [(x, y) for x in range(0, int(W * .2), 6) for y in range(int(H * .55), H, 6)]
        assert sum(1 for x, y in side if changed(x, y)) / len(side) < .1, "it is not centred"
        assert not changed(10, 10) and not changed(W - 10, 10), "the art spilled over the top of the image"
        # The caption bubble: white ink somewhere beside the machine, below the middle.
        white = sum(1 for x in range(0, W, 4) for y in range(H // 2, H, 4) if min(out.getpixel((x, y))) > 235)
        assert white > 200, "no caption bubble"


def test_every_surface_offers_it():
    from app.services.command_service import CommandService
    from app.services.meme_builder_service import _ALPHA_CHARACTERS
    assert "woodchipper" in CommandService.COMMANDS and "Get in!" in CommandService.COMMANDS["woodchipper"]
    assert "woodchipper" in CommandService.MOTION_EFFECTS
    assert "woodchipper" in dict(_ALPHA_CHARACTERS), "not in the Meme Builder"


def test_the_command_returns_the_picture_and_asks_for_one_when_missing():
    from app.services.command_service import CommandService
    cs = CommandService.__new__(CommandService)
    out = asyncio.run(cs._woodchipper_command([("pic.jpg", _photo(), "image/jpeg")]))
    assert out["type"] == "files" and out["files"], out
    none = asyncio.run(cs._woodchipper_command([]))
    assert none["type"] == "text" and "woodchipper" in none["content"]
