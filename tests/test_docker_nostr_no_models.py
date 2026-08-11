"""A Nostr-only node downloads NO model weights — the shipped entrypoint logic, actually run.

Run: venv-unified/bin/python -m unittest tests.test_docker_nostr_no_models

WHAT WAS WRONG. `DOWNLOAD_MODEL=1`, `DOWNLOAD_DEPTH_MODEL=1` and `DOWNLOAD_U2NET_MODEL=1` are ENV in
the SHARED final stage of the Dockerfile, so they are on in every image — including the nostr-only
one, which is built from requirements-nostr.txt and has no llama-cpp, no torch, no onnxruntime and
no rembg. `docker compose --profile nostr up -d` therefore began pulling ~5.9 GB of weights
(5.6 GB GGUF + 94 MB depth + 176 MB u2net) that nothing in the container can load, in the
background, onto the data volume of the deployment least likely to want them.

It is tested by EXECUTING the entrypoint's own download section under bash with `curl` stubbed,
rather than by reading it: the conditions are shell, the failure is "a background subshell ran", and
a test that greps for a variable name would pass against a gate that was never wired into the third
block. The Dockerfile assertions below are the other half — they pin the defaults this exists to
neutralise, so the day one of them changes, this file explains why it mattered.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "docker-entrypoint.sh"
DOCKERFILE = ROOT / "Dockerfile"


def _download_section() -> str:
    """The shipped gate + the three pre-fetch blocks, lifted out of the entrypoint verbatim."""
    src = ENTRY.read_text()
    start = src.index("PC_WANT_MODELS=1")
    end = src.index("# Provision the relay's instance (operator) key")
    return src[start:end]


def run_entrypoint_downloads(env: dict):
    """Run that section for real, with a stub `curl`, and report what it tried to fetch.

    The blocks background themselves with `( … ) &`, so the harness waits for them — otherwise the
    script exits before the stub has written anything and EVERY case looks like "downloaded
    nothing", which is the answer this test is looking for.
    """
    with tempfile.TemporaryDirectory() as td:
        bindir = Path(td) / "bin"
        bindir.mkdir()
        log = Path(td) / "fetched.log"
        # A stub curl: record the URL, then create the -o file so the block's `mv` succeeds.
        (bindir / "curl").write_text(
            "#!/usr/bin/env bash\n"
            "out=''; url=''\n"
            "while [ $# -gt 0 ]; do case \"$1\" in -o) out=\"$2\"; shift 2;; "
            "http*) url=\"$1\"; shift;; *) shift;; esac; done\n"
            f"echo \"$url\" >> {log}\n"
            "[ -n \"$out\" ] && : > \"$out\"\n"
            "exit 0\n")
        (bindir / "curl").chmod(0o755)
        base = {
            "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
            "POSTERCHANAI_LLM_MODEL_PATH": str(Path(td) / "models" / "chat.gguf"),
            "POSTERCHANAI_MODEL_URL": "https://example.invalid/chat.gguf",
            "DEPTH_MODEL_PATH": str(Path(td) / "assets" / "depth.onnx"),
            "DEPTH_MODEL_URL": "https://example.invalid/depth.onnx",
            "U2NET_HOME": str(Path(td) / "u2net"),
            "U2NET_MODEL_URL": "https://example.invalid/u2net.onnx",
            "DOWNLOAD_MODEL": "1", "DOWNLOAD_DEPTH_MODEL": "1", "DOWNLOAD_U2NET_MODEL": "1",
        }
        for k in ("POSTERCHANAI_LLM_MODEL_PATH", "DEPTH_MODEL_PATH"):
            Path(base[k]).parent.mkdir(parents=True, exist_ok=True)
        base.update(env)
        script = _download_section() + "\nwait\n"
        out = subprocess.run(["bash", "-c", script], env=base, capture_output=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError(out.stderr.decode()[-2000:])
        urls = log.read_text().split() if log.exists() else []
        return urls, out.stdout.decode()


@unittest.skipIf(shutil.which("bash") is None, "bash not installed")
class EntrypointTests(unittest.TestCase):
    def test_the_nostr_image_downloads_nothing(self):
        """PC_ACCEL is baked into the image at build time, so this holds under a bare `docker run`
        with none of the compose environment — which is exactly how somebody would try the lean
        image out."""
        urls, log = run_entrypoint_downloads({"PC_ACCEL": "nostr"})
        self.assertEqual(urls, [], f"a nostr-only build fetched {urls}")
        self.assertIn("Nostr-only", log, "it skipped them SILENTLY — say why, or a missing model "
                                         "and a failed download read the same in the log")

    def test_nostr_only_mode_downloads_nothing_on_any_image(self):
        """An AI-capable image run as a Nostr-only node: the AI surfaces are hidden, so 5.6 GB of
        chat model would reach nothing."""
        for val in ("1", "true", "TRUE", "yes", "on"):
            urls, _ = run_entrypoint_downloads({"PC_ACCEL": "cpu", "POSTERCHANAI_NOSTR_ONLY": val})
            self.assertEqual(urls, [], f"POSTERCHANAI_NOSTR_ONLY={val} still fetched {urls}")

    def test_an_ai_build_still_prefetches(self):
        """The other half: this must not have turned the turnkey pull off for everybody. That is the
        way a guard like this 'passes' while quietly breaking the default install."""
        urls, _ = run_entrypoint_downloads({"PC_ACCEL": "cuda"})
        self.assertEqual(len(urls), 3, f"an AI build fetched {urls} — want chat + depth + u2net")
        self.assertTrue(any("chat.gguf" in u for u in urls), urls)
        self.assertTrue(any("depth" in u for u in urls), urls)
        self.assertTrue(any("u2net" in u for u in urls), urls)

    def test_off_is_still_off(self):
        urls, _ = run_entrypoint_downloads({"PC_ACCEL": "cuda", "DOWNLOAD_MODEL": "0",
                                            "DOWNLOAD_DEPTH_MODEL": "0", "DOWNLOAD_U2NET_MODEL": "0"})
        self.assertEqual(urls, [])

    def test_every_prefetch_is_behind_the_gate(self):
        """A block added later that forgets the gate is the whole risk: the other three would still
        pass their tests while the new one pulls a model into the lean image."""
        for line in _download_section().split("\n"):
            if re.search(r'\$\{DOWNLOAD_\w+:-0\}"? = "1"', line):
                self.assertIn('"$PC_WANT_MODELS" = "1"', line,
                              f"this pre-fetch is not gated on PC_WANT_MODELS:\n  {line.strip()}")


class DockerfileTests(unittest.TestCase):
    """The defaults the gate exists to neutralise, pinned so a change to them is a decision."""

    SRC = DOCKERFILE.read_text()

    def test_the_image_still_ships_the_flags_on(self):
        for flag in ("DOWNLOAD_MODEL=1", "DOWNLOAD_DEPTH_MODEL=1", "DOWNLOAD_U2NET_MODEL=1"):
            self.assertIn(flag, self.SRC,
                          f"{flag} is gone — if the turnkey pre-fetch was removed, the entrypoint "
                          "gate and this test can go with it")

    def test_the_build_accelerator_is_baked_in(self):
        """PC_WANT_MODELS reads PC_ACCEL, which is the only thing in a bare `docker run` that knows
        the image has no AI stack."""
        self.assertIn("ENV PC_ACCEL=${GPU}", self.SRC)

    def test_the_nostr_build_installs_no_ai_stack(self):
        """Why the pre-fetch is pointless there, stated as a test: no llama-cpp/torch/diffusers, so
        nothing in that image can open a GGUF or an ONNX file."""
        self.assertRegex(self.SRC, r'nostr\)\s*\\?\s*\n\s*echo "Nostr-only build: skipping torch')
        self.assertIn('if [ "$GPU" = "nostr" ]; then \\\n        pip install -r /tmp/requirements-nostr.txt',
                      self.SRC)


class AdminButtonTests(unittest.TestCase):
    """The admin panel is NOT gated by nostr-only mode, so its "Download chat model" button sits
    there on a build that cannot use one. The service refuses, with a sentence."""

    SRC = (ROOT / "app" / "services" / "model_download_service.py").read_text()

    def test_the_service_refuses_on_a_no_ai_build(self):
        self.assertIn("def _no_ai_build()", self.SRC)
        self.assertIn('_blocked = _no_ai_build()', self.SRC)
        self.assertIn('_set(kind, "error", _blocked)', self.SRC)

    def test_it_reads_both_signals(self):
        self.assertIn('os.getenv("PC_ACCEL"', self.SRC)
        self.assertIn('os.getenv("POSTERCHANAI_NOSTR_ONLY"', self.SRC)

    def test_it_actually_blocks(self):
        import importlib
        mds = importlib.import_module("app.services.model_download_service")
        prev = os.environ.get("PC_ACCEL")
        os.environ["PC_ACCEL"] = "nostr"
        try:
            started = mds.start("chat", lambda: None)
            self.assertFalse(started)
            st = mds.status("chat")
            self.assertEqual(st["state"], "error")
            self.assertIn("Nostr-only", st["message"])
        finally:
            if prev is None:
                os.environ.pop("PC_ACCEL", None)
            else:
                os.environ["PC_ACCEL"] = prev


if __name__ == "__main__":
    unittest.main()
