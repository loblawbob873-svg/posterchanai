"""The focused desktop window gets Hyprland's turning gradient border.

Asked for as "can you add the nice cyberpunk border to windows like Hyprland does?".

The interesting part is not the look, it is the four ways a CSS border like this quietly fails on
this particular desktop, each of which these tests pin:

  * `.osw` is `overflow:hidden`, so anything drawn at a NEGATIVE inset is clipped and simply never
    appears — the ring has to be inside the frame.
  * a gradient BORDER needs a masked pseudo-element; `border-color` takes one colour and
    `border-image` cannot be rounded.
  * the palette must be the theme's own tokens, or a light theme gets a hardcoded neon stripe.
  * `body.anim-off` (the client idles the GPU when backgrounded) sets `animation:none`, and a
    DISABLED animation resolves to the resting style. A `paused` one freezes mid-keyframe — the
    class of bug that once painted a whole timeline invisible.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def rule(selector):
    i = CSS.index(selector)
    return CSS[i:CSS.index("}", i) + 1]


class TheRing(unittest.TestCase):

    def test_it_is_drawn_inside_the_frame(self):
        """`.osw{overflow:hidden}` clips a negative inset to nothing."""
        self.assertIn("overflow:hidden", rule(".osw{position:absolute"))
        ring = rule(".os-root.os-fx .osw.focused:not(.osw-document)::after{")
        self.assertIn("inset:0", ring)
        self.assertNotRegex(ring, r"inset:-")

    def test_it_is_a_masked_gradient_not_a_border_colour(self):
        ring = rule(".os-root.os-fx .osw.focused:not(.osw-document)::after{")
        self.assertIn("conic-gradient", ring)
        self.assertIn("mask-composite:exclude", ring)
        self.assertIn("-webkit-mask-composite:xor", ring, "Safari/older WebKit spelling is missing")
        self.assertIn("border-radius:inherit", ring, "a square ring on a rounded window")

    def test_the_angle_is_declared_or_it_cannot_animate(self):
        """A bare custom property is a string to the animation engine and will not interpolate."""
        self.assertRegex(CSS, r"@property --osw-ang\{[^}]*syntax:'<angle>'")
        self.assertIn("@keyframes osw-ring{to{--osw-ang:360deg}}", CSS)

    def test_it_uses_the_theme_palette(self):
        """The GRADIENT only. The mask is deliberately `#000` — a mask needs an opaque colour and
        a theme token there would make the ring's own visibility depend on the palette."""
        ring = rule(".os-root.os-fx .osw.focused:not(.osw-document)::after{")
        grad = ring[ring.index("background:"):ring.index(";", ring.index("background:"))]
        self.assertIn("var(--neon)", grad)
        self.assertIn("var(--neon2)", grad)
        self.assertNotRegex(grad, r"#[0-9a-fA-F]{3,8}\b")

    def test_it_is_behind_the_existing_effects_switch(self):
        """`.os-fx` is the Desktop effects setting and is already off on touch. A continuously
        animating border on every focused window is exactly what that switch is for."""
        for i, block in enumerate(CSS.split("}")):
            if "animation:osw-ring" not in block:
                continue
            # The SELECTOR this declaration belongs to, not the line it sits on.
            sel = block[block.rindex("{", 0, block.index("animation:osw-ring")) :]
            sel = block[: block.index("{")].split("\n")[-1]
            self.assertIn("os-fx", sel, "the ring animates outside the effects switch: " + sel)

    def test_reduced_motion_keeps_the_ring_and_drops_the_rotation(self):
        i = CSS.index("@media(prefers-reduced-motion:reduce){\n  .os-root.os-fx .osw.focused")
        block = CSS[i:CSS.index("}}", i) + 2]
        self.assertIn("animation:none", block)
        self.assertNotIn("display:none", block)
        self.assertNotIn("content:none", block)

    def test_a_document_window_is_left_alone(self):
        """Office/PDF/mail windows are deliberately plain chrome — the same reason cyberpunk's
        scanline layer is excluded from them."""
        ring = rule(".os-root.os-fx .osw.focused:not(.osw-document)::after{")
        self.assertIn(":not(.osw-document)", ring)

    def test_one_shadow_rule_owns_the_focused_window(self):
        """Two rules for one selector is how a later edit silently loses to an earlier one."""
        hits = re.findall(r"\.os-root\.os-fx \.osw\.focused:not\(\.osw-document\)\{", CSS)
        self.assertEqual(len(hits), 1, "the focused-window shadow is declared twice")

    def test_it_cannot_strand_the_window_when_the_gpu_idles(self):
        """`anim-off` must DISABLE, never pause: a disabled animation resolves to the resting
        style, a paused one holds whatever keyframe it reached."""
        self.assertIn("body.anim-off *, body.anim-off *::before, body.anim-off *::after{ animation: none !important; }", CSS)
        ring = rule(".os-root.os-fx .osw.focused:not(.osw-document)::after{")
        self.assertNotIn("animation-play-state", ring)
        self.assertNotIn("opacity:0", ring, "a ring that starts transparent can be stranded invisible")


if __name__ == "__main__":
    unittest.main()
