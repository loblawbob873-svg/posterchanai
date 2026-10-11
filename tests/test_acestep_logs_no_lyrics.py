"""ACE-Step must not write a song's caption or lyrics into the journal.

2026-10-10: investigating a music report meant reading members' song requests and full lyrics straight out of
`journalctl -u posterchanai` -- ACE-Step logs `text_prompt`, `lyrics_text`, `params.caption`/`params.lyrics` and
its LM's formatted prompt at INFO through loguru, in several modules. tests/test_no_prompt_logging.py greps OUR
code and cannot see a library's. music_local silences the library's loguru output before importing it."""
import ast
from pathlib import Path

from app.services import music_local

SRC = Path(music_local.__file__).read_text()


def _log_from(module_name, text):
    from loguru import logger
    got = []
    sink = logger.add(lambda m: got.append(str(m)), level="DEBUG")
    try:
        exec("from loguru import logger\nlogger.info(%r)" % text, {"__name__": module_name})
    finally:
        logger.remove(sink)
    return got


def test_acestep_cannot_log_a_songs_words():
    music_local._quiet_acestep_logs()
    for mod in ("acestep.core.generation.handler.conditioning_text", "acestep.inference", "acestep.llm_inference"):
        assert not _log_from(mod, "lyrics_text: my secret verse"), mod
    assert _log_from("app.services.something_else", "still logs"), "silenced more than ACE-Step"


def test_it_is_silenced_before_every_acestep_import():
    tree = ast.parse(SRC)
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        imports = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("acestep")]
        if not imports:
            continue
        quiet = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", "") == "_quiet_acestep_logs"]
        assert quiet and min(quiet) < min(imports), "%s imports acestep before silencing it" % fn.name
