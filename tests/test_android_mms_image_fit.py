"""A CAMERA PHOTO MUST FIT IN THE MMS THE PLATFORM WILL CARRY — the size is ours to fix, not mmslib's.

Reported, again: "Unable to send attachments like pictures still in android Texts". Two earlier fixes
(e3d5b293, c12b1940) chased the subscription id because the failure arrived as
`MMS_ERROR_IO_ERROR` ("Send status unconfirmed"). The real cause was upstream of the radio:

  * `MmsLink.required()` keeps every photo <= 8 MB on the MMS path, on the stated belief that
    "mmslib resizes an image for the carrier".
  * It does not. Decompiled org.fossify:mmslib:1.0.0 — `new Message(body, to, Bitmap)` ends in
    `Message.bitmapToByteArray`, i.e. `image.compress(JPEG, 90, stream)` of the FULL-RESOLUTION
    bitmap, with no scaling anywhere on the send path.
  * `Transaction.sendMmsThroughSystem` then passes the platform `maxMessageSize =
    MmsConfig.getMaxMessageSize()` (819200) as a config override. The platform's MmsService reads at
    most that many PDU bytes, answers null for more, and the request ends with MMS_ERROR_IO_ERROR.

So: every ordinary camera picture was refused before it reached the carrier, and the receiver files
code 5 as "status unconfirmed". This file RUNS the Android-free decision half (`MmsImageFit`) on a
JVM, MEASURES with a real JPEG encoder (javax.imageio) that the pre-fix encoding of a 12 MP photo is
over the transport cap while the ladder lands under a 300 KB carrier budget, and pins that
MmsSender actually routes photos through it. The on-device half (Android's own Bitmap/JPEG and the
library's real PDU composer) is `MmsImageFitDeviceTest`, which runs in the emulator CI.

Run: venv-unified/bin/python -m pytest tests/test_android_mms_image_fit.py -q
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SMS = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java", "place", "poster", "app", "sms")
SENDER = os.path.join(SMS, "MmsSender.java")
PLUGIN = os.path.join(SMS, "SmsPlugin.java")
LINK = os.path.join(SMS, "MmsLink.java")
JAVAC = shutil.which("javac")
JAVARUN = shutil.which("java")

KB = 1024

HARNESS = r'''
package place.poster.app.sms;

import java.awt.image.BufferedImage;
import java.io.ByteArrayOutputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.Random;
import javax.imageio.IIOImage;
import javax.imageio.ImageIO;
import javax.imageio.ImageWriteParam;
import javax.imageio.ImageWriter;
import javax.imageio.stream.MemoryCacheImageOutputStream;

public class FitProbe {
    static byte[] jpeg(BufferedImage img, int quality) throws Exception {
        ImageWriter w = ImageIO.getImageWritersByFormatName("jpeg").next();
        ImageWriteParam p = w.getDefaultWriteParam();
        p.setCompressionMode(ImageWriteParam.MODE_EXPLICIT);
        p.setCompressionQuality(quality / 100f);
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try (MemoryCacheImageOutputStream s = new MemoryCacheImageOutputStream(out)) {
            w.setOutput(s);
            w.write(null, new IIOImage(img, null, null), p);
        }
        w.dispose();
        return out.toByteArray();
    }

    /** A camera-like frame: smooth gradients plus sensor-style noise and some edges. */
    static BufferedImage photo(int w, int h) {
        BufferedImage img = new BufferedImage(w, h, BufferedImage.TYPE_INT_RGB);
        Random r = new Random(7);
        for (int y = 0; y < h; y++) for (int x = 0; x < w; x++) {
            int n = r.nextInt(25) - 12;
            int edge = ((x / 97) + (y / 61)) % 2 == 0 ? 30 : 0;
            int rr = clamp(x * 255 / w + n + edge), gg = clamp(y * 255 / h + n), bb = clamp(128 + n - edge);
            img.setRGB(x, y, (rr << 16) | (gg << 8) | bb);
        }
        return img;
    }
    static int clamp(int v) { return Math.max(0, Math.min(255, v)); }

    static BufferedImage scale(BufferedImage src, int edge) {
        int[] s = MmsImageFit.scaled(src.getWidth(), src.getHeight(), edge);
        BufferedImage out = new BufferedImage(s[0], s[1], BufferedImage.TYPE_INT_RGB);
        java.awt.Graphics2D g = out.createGraphics();
        g.setRenderingHint(java.awt.RenderingHints.KEY_INTERPOLATION,
                java.awt.RenderingHints.VALUE_INTERPOLATION_BILINEAR);
        g.drawImage(src, 0, 0, s[0], s[1], null);
        g.dispose();
        return out;
    }

    public static void main(String[] a) throws Exception {
        String mode = a[0];
        if (mode.equals("rules")) {
            System.out.println("budget300=" + MmsImageFit.budget(300 * 1024, 819200, 0));
            System.out.println("budget1200=" + MmsImageFit.budget(1200 * 1024, 819200, 0));
            System.out.println("budgetBody=" + MmsImageFit.budget(300 * 1024, 819200, 1000));
            System.out.println("budgetUnknown=" + MmsImageFit.budget(0, 819200, 0));
            System.out.println("budgetTiny=" + MmsImageFit.budget(20 * 1024, 819200, 0));
            System.out.println("gifFits=" + MmsImageFit.sendAsIs("image/gif", 1000, 2000));
            System.out.println("gifBig=" + MmsImageFit.sendAsIs("image/gif", 3000, 2000));
            System.out.println("jpegFits=" + MmsImageFit.sendAsIs("image/jpeg", 1000, 2000));
            System.out.println("sample=" + MmsImageFit.sampleSize(4000, 3000, 1600));
            System.out.println("sample480=" + MmsImageFit.sampleSize(4000, 3000, 480));
            int[] s = MmsImageFit.scaled(4000, 3000, 1600);
            System.out.println("scaled=" + s[0] + "x" + s[1]);
            int[] p = MmsImageFit.scaled(3000, 4000, 1600);
            System.out.println("portrait=" + p[0] + "x" + p[1]);
            int[] small = MmsImageFit.scaled(500, 400, 1600);
            System.out.println("small=" + small[0] + "x" + small[1]);
            return;
        }
        if (mode.equals("ladder")) {
            // A size model: bytes ~ edge^2 * quality / K. Records every rung asked for.
            final List<String> asked = new ArrayList<>();
            byte[] got = MmsImageFit.fit((edge, q) -> {
                asked.add(edge + "@" + q);
                return new byte[(int) ((long) edge * edge * q / 1000)];
            }, 4000, 3000, 60_000);
            System.out.println("got=" + (got == null ? -1 : got.length));
            System.out.println("asked=" + String.join(",", asked));
            asked.clear();
            byte[] none = MmsImageFit.fit((edge, q) -> { asked.add(edge + "@" + q); return new byte[10_000_000]; },
                    4000, 3000, 60_000);
            System.out.println("none=" + (none == null));
            System.out.println("tried=" + asked.size());
            asked.clear();
            MmsImageFit.fit((edge, q) -> { asked.add(edge + "@" + q); return new byte[10_000_000]; },
                    500, 400, 60_000);
            System.out.println("smallEdges=" + String.join(",", asked));
            return;
        }
        if (mode.equals("measure")) {
            BufferedImage cam = photo(4000, 3000);
            // THE PRE-FIX ENCODING: the full-resolution bitmap at quality 90, exactly what
            // Message.bitmapToByteArray does with the Bitmap the old MmsSender handed it.
            System.out.println("prefix=" + jpeg(cam, 90).length);
            int budget = MmsImageFit.budget(300 * 1024, 819200, 0);
            final BufferedImage[] held = {null};
            final int[] heldEdge = {-1};
            final int[] lastEdge = {0};
            byte[] fitted = MmsImageFit.fit((edge, q) -> {
                if (heldEdge[0] != edge) { held[0] = scale(cam, edge); heldEdge[0] = edge; }
                lastEdge[0] = edge;
                return jpeg(held[0], q);
            }, cam.getWidth(), cam.getHeight(), budget);
            System.out.println("budget=" + budget);
            System.out.println("fitted=" + (fitted == null ? -1 : fitted.length));
            System.out.println("edge=" + lastEdge[0]);
            BufferedImage back = ImageIO.read(new java.io.ByteArrayInputStream(fitted));
            System.out.println("decoded=" + back.getWidth() + "x" + back.getHeight());
        }
    }
}
'''


def _parse(out):
    return dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)


@unittest.skipIf(not JAVAC or not JAVARUN, "no JDK on this node")
class FitRunsOnAJvm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        pkg = os.path.join(cls.tmp, "src", "place", "poster", "app", "sms")
        os.makedirs(pkg)
        for name in ("MmsImageFit.java", "MmsAttachment.java"):
            shutil.copy(os.path.join(SMS, name), pkg)
        with open(os.path.join(pkg, "FitProbe.java"), "w") as f:
            f.write(HARNESS)
        out = os.path.join(cls.tmp, "out")
        r = subprocess.run([JAVAC, "-d", out] + [os.path.join(pkg, n) for n in os.listdir(pkg)],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        cls.cp = out

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_probe(self, mode):
        r = subprocess.run([JAVARUN, "-Djava.awt.headless=true", "-cp", self.cp,
                            "place.poster.app.sms.FitProbe", mode],
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        return _parse(r.stdout)

    def test_the_budget_is_under_both_the_carrier_and_the_transport_cap(self):
        got = self.run_probe("rules")
        self.assertEqual(int(got["budget300"]), 300 * KB - 16 * KB)
        # A carrier publishing 1.2 MB is still capped by mmslib's 800 KB maxMessageSize override —
        # the platform would refuse a 1 MB PDU with MMS_ERROR_IO_ERROR.
        self.assertEqual(int(got["budget1200"]), 819200 - 16 * KB)
        self.assertEqual(int(got["budgetBody"]), 300 * KB - 16 * KB - 1000, "the text shares the PDU")
        self.assertEqual(int(got["budgetUnknown"]), 300 * KB - 16 * KB, "an unread config is 300 KB")
        self.assertEqual(int(got["budgetTiny"]), 48 * KB, "a nonsense ceiling is floored")

    def test_only_a_gif_that_already_fits_goes_untouched(self):
        got = self.run_probe("rules")
        self.assertEqual(got["gifFits"], "true", "re-encoding a GIF flattens its animation")
        self.assertEqual(got["gifBig"], "false")
        self.assertEqual(got["jpegFits"], "false",
                         "a camera JPEG is re-encoded even when it fits: that is what strips its GPS EXIF")

    def test_decode_and_scale_geometry(self):
        got = self.run_probe("rules")
        self.assertEqual(got["sample"], "2")
        self.assertEqual(got["sample480"], "8")
        self.assertEqual(got["scaled"], "1600x1200")
        self.assertEqual(got["portrait"], "1200x1600")
        self.assertEqual(got["small"], "500x400", "a small picture is never upscaled")

    def test_the_ladder_takes_the_first_rung_that_fits_and_admits_when_none_does(self):
        got = self.run_probe("ladder")
        self.assertEqual(int(got["got"]), 1024 * 1024 * 48 // 1000)
        self.assertTrue(got["asked"].startswith("1600@85,1600@72,1600@60,1600@48,1280@85"), got["asked"])
        self.assertTrue(got["asked"].endswith("1024@60,1024@48"), got["asked"])
        self.assertEqual(got["none"], "true", "nothing fitting must be a refusal, never an oversized PDU")
        self.assertEqual(int(got["tried"]), 6 * 4)
        # 500 px: every larger rung is the same 500 px encode, so it is tried once, then 480.
        self.assertTrue(got["smallEdges"].startswith("500@85,500@72,500@60,500@48,480@85"),
                        got["smallEdges"])

    def test_measured_a_camera_photo_did_not_fit_before_and_fits_now(self):
        got = self.run_probe("measure")
        prefix = int(got["prefix"])
        self.assertGreater(prefix, 819200,
                           "the pre-fix encoding (full resolution, quality 90) must be over the "
                           "transport's 800 KB cap — that is the MMS_ERROR_IO_ERROR")
        self.assertGreater(int(got["fitted"]), 0)
        self.assertLessEqual(int(got["fitted"]), int(got["budget"]))
        w, h = (int(x) for x in got["decoded"].split("x"))
        self.assertLessEqual(max(w, h), 1600)
        self.assertEqual(w * 3, h * 4, "aspect ratio kept")


class SenderUsesTheFit(unittest.TestCase):
    """The rule is worthless if the transport still hands mmslib a Bitmap to encode itself."""

    def setUp(self):
        self.sender = open(SENDER, encoding="utf-8").read()

    def test_photos_never_reach_the_librarys_bitmap_encoder(self):
        self.assertIsNone(re.search(r"new Message\([^)]*,\s*to,\s*image\)", self.sender),
                          "the Bitmap constructor re-encodes the full-resolution photo at q90")
        self.assertNotIn("android.graphics.Bitmap image", self.sender)
        self.assertIn("MmsImageFit.budget(carrierLimit(ctx), transportLimit(), bodyBytes)", self.sender)
        self.assertIn("prepareImage(raw, type, budget)", self.sender)
        self.assertIn('message.addMedia(fitted, asIs ? type : "image/jpeg", partName, partName)',
                      self.sender)

    def test_every_ceiling_is_capped_by_the_transport(self):
        self.assertIn("com.android.mms.MmsConfig.getMaxMessageSize()", self.sender)
        self.assertIn("static int ceiling(int carrier) { return Math.min(carrier, transportLimit()); }",
                      self.sender)
        body = self.sender[self.sender.index("static int carrierLimit() {"):]
        body = body[:body.index("\n    }\n")]
        self.assertIn("ceiling(", body, "videoLimit() reads carrierLimit(); it must be transport-capped too")
        plugin = open(PLUGIN, encoding="utf-8").read()
        self.assertIn("MmsSender.transportLimit()", plugin,
                      "the WebView's link threshold (SmsPlugin.mmsLimit) must know the transport cap")

    def test_the_link_rule_no_longer_claims_the_library_resizes(self):
        link = open(LINK, encoding="utf-8").read()
        self.assertNotIn("mmslib resizes an image for the carrier", link)
        self.assertIn("MmsImageFit", link)


if __name__ == "__main__":
    unittest.main()
