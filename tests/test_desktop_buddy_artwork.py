"""The desktop dancers' frames: every one present, the same canvas, feet on one line, nothing cut off,
and shipped in both app bundles. 'make an alternative to posterchan that users can choose, a dancing
axolotl' -- a frame on a different canvas or baseline makes her jump while she dances."""
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("folder", ["dance", "axolotl"])
def test_eight_frames_one_canvas_one_baseline_nothing_cropped(folder):
    feet = []
    for i in range(1, 9):
        f = ROOT / "static/mascot" / folder / f"dance-{i}.webp"
        assert f.exists(), f
        im = Image.open(f)
        assert im.size == (406, 560) and im.mode == "RGBA", (f, im.size, im.mode)
        a = np.asarray(im)[:, :, 3]
        assert not (a[0] > 20).any() and not (a[:, 0] > 20).any() and not (a[:, -1] > 20).any(), f"{f} is cut off at an edge"
        assert a.max() == 255 and (a > 20).mean() > 0.15, f"{f} is empty or faint"
        feet.append(int(np.nonzero((a > 20).any(axis=1))[0].max()))
    assert max(feet) - min(feet) <= 6, ("feet are not on one line", folder, feet)


def test_both_dancers_ship_in_both_app_bundles():
    for script in ("desktop/build-www.sh", "mobile/build-www.sh"):
        src = (ROOT / script).read_text()
        for folder in ("static/mascot/dance", "static/mascot/axolotl"):
            assert folder in src, (script, "does not copy", folder)
