"""A stream that answers 0 has not ended, and this app treated it as though it had.

`while ((n = in.read(buf)) > 0)` is the shape, and it appeared three times in the sync package: the
file HASH, the whole-file READ, and the HTTP response copy. `InputStream.read` signals end of stream
with **-1**; a return of 0 means "no bytes this time", which is legal and ordinary on a pipe — and a
DocumentsProvider delivers a document over a pipe, which is how every cloud-backed and many custom
providers work.

Every one of the three failures is silent, and two of them are ruinous:

  * the HASH covers a PREFIX of the file and is returned as if it covered all of it. On the way out
    that certifies an upload with the checksum of its first few megabytes, so every other device
    downloads the file correctly, verifies it, and refuses it FOR EVER. On the way in it condemns a
    perfect download — "the copy in the store fails its checksum" about bytes whose every chunk
    already verified against its own content address on arrival.
  * the whole-file READ returns a SHORT file with no error and no short count.
  * the response COPY truncates a body: a chunk that is short but still hashes to something, or a
    JSON reply that ends mid-object.

The odds scale with the number of reads, which is why it is the biggest file in a folder that shows
it — reported as a >2 GB `.jex` that "keeps failing on both android devices, can't be fetched: the
copy in the store fails its checksum".

This RUNS the real SafFs digest loop against a stream that answers 0 in the middle, because a test
that greps for the operator would pass the day somebody writes the same mistake a different way.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYNC = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java",
                    "place", "poster", "app", "sync")
JAVAC = shutil.which("javac")
JAVA = shutil.which("java")

# The digest loop, lifted VERBATIM from SafFs.sha256 by the test itself (see _loop) so it cannot
# drift from the shipped one.
HARNESS = r"""
import java.io.*;
import java.security.MessageDigest;

public class StreamProbe {
  /** A stream that answers 0 once, in the middle, without having ended — what a pipe does. */
  static class Stuttering extends InputStream {
    private final byte[] data; private int at = 0; private boolean stuttered = false;
    Stuttering(byte[] d){ data = d; }
    public int read() { return at < data.length ? (data[at++] & 0xff) : -1; }
    public int read(byte[] b, int off, int len) {
      if (at >= data.length) return -1;
      if (!stuttered && at > 0) { stuttered = true; return 0; }   // legal, and not the end
      int n = Math.min(len, data.length - at);
      System.arraycopy(data, at, b, off, n); at += n; return n;
    }
  }

  static String digest(InputStream in) throws Exception {
    MessageDigest md = MessageDigest.getInstance("SHA-256");
    byte[] buf = new byte[16];
    int n;
    __LOOP__
    StringBuilder sb = new StringBuilder();
    for (byte b : md.digest()) sb.append(Character.forDigit((b >> 4) & 0xf, 16))
                                 .append(Character.forDigit(b & 0xf, 16));
    return sb.toString();
  }

  public static void main(String[] a) throws Exception {
    byte[] data = new byte[200];
    for (int i = 0; i < data.length; i++) data[i] = (byte) i;
    String whole = digest(new ByteArrayInputStream(data));
    String piped = digest(new Stuttering(data));
    System.out.println(whole.equals(piped) ? "SAME" : ("DIFFER " + whole + " " + piped));
  }
}
"""


@unittest.skipIf(not JAVAC or not JAVA, "no JDK on this node")
@unittest.skipIf(not os.path.isdir(SYNC), "no android sources here")
class StreamReadsRunToTheEnd(unittest.TestCase):
    def _loop(self):
        """The digest loop as SHIPPED, so this test is about the real code and not a copy of it."""
        src = open(os.path.join(SYNC, "SafFs.java"), encoding="utf-8").read()
        m = re.search(r"while \(\(n = in\.read\(buf\)\)[^\n]*md\.update\(buf, 0, n\);", src)
        self.assertTrue(m, "the digest loop moved — re-point this test rather than deleting it")
        return m.group(0)

    def _run(self, loop):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "StreamProbe.java"), "w") as fh:
                fh.write(HARNESS.replace("__LOOP__", loop))
            r = subprocess.run([JAVAC, "-nowarn", "-d", d, os.path.join(d, "StreamProbe.java")],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr[-800:])
            r = subprocess.run([JAVA, "-cp", d, "StreamProbe"], capture_output=True, text=True,
                               timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr[-800:])
            return r.stdout.strip()

    def test_the_shipped_hash_covers_the_whole_file_over_a_stuttering_stream(self):
        self.assertEqual(self._run(self._loop()), "SAME",
                         "the digest stopped at a zero-length read and hashed a PREFIX of the file "
                         "— on the way out that certifies an upload nobody can ever verify, and on "
                         "the way in it condemns a download that is perfectly fine")

    def test_and_the_old_loop_really_did_get_it_wrong(self):
        """The check can fail. Without this the test above passes against any loop that happens to
        work on a ByteArrayInputStream, which is every loop."""
        self.assertTrue(self._run("while ((n = in.read(buf)) > 0) md.update(buf, 0, n);")
                        .startswith("DIFFER"),
                        "the probe's stream does not actually stutter — it proves nothing")

    def test_no_read_loop_in_the_package_stops_on_zero(self):
        """The other two: the whole-file read and the HTTP response copy. Same operator, same
        silence — a short file and a truncated response body."""
        bad = []
        for name in sorted(os.listdir(SYNC)):
            if not name.endswith(".java"):
                continue
            src = open(os.path.join(SYNC, name), encoding="utf-8").read()
            for m in re.finditer(r"while \(\(\w+ = \w+\.read\(\w+\)\) *> *0\)", src):
                bad.append(f"{name}: {m.group(0)}")
        self.assertEqual(bad, [], "a read loop treats 0 as end of stream; -1 is end of stream")


if __name__ == "__main__":
    unittest.main()
