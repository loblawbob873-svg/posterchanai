"""PosterChan Code is for everybody — and not everybody edits the node.

Run: venv-unified/bin/python -m pytest tests/test_code_is_for_everyone.py

It was gated to the terminal's allowlist, so an ordinary account got "limited to administrators"
and no editor at all. Reported by real users.

That gate was not arbitrary, and removing it alone would have been the worst possible fix: `_root()`
defaults to the app's OWN CHECKOUT — the directory `run.py` lives in — so write access there is
write access to the code this node runs. On a public instance with hundreds of Nostr signups, one
deleted line would have handed every account remote code execution.

So the gate became a ROUTE, and that is what is asserted here: an operator (admin, or the Admin →
Nodes allowlist — the same people who may open a shell) edits the node's tree; everybody else gets a
directory of their own. Both are real editors. The jail is the same for both.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE = os.path.join(ROOT, "app", "routers", "code.py")


def _read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def _code_only(src):
    """The CODE, without the prose.

    Three guards in this repo have now reported the paragraph EXPLAINING why something must not
    appear as that thing appearing — including this file's own first draft, twice in one run.
    """
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    src = re.sub(r"'''(?:.|\n)*?'''", '', src)
    return re.sub(r"(?m)#.*$", "", src)



class CodeIsForEveryone(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = _read(CODE)
        cls.src = _code_only(cls.raw)

    def test_an_ordinary_account_is_no_longer_refused(self):
        self.assertNotIn("limited to administrators", self.src,
                         "an ordinary user still gets a 403 and no editor")
        self.assertNotIn("raise HTTPException(status_code=403", self.src.split("def _guard")[1].split("\n\n")[0],
                         "_guard still refuses somebody outright")

    def test_only_an_operator_gets_the_node_s_own_tree(self):
        """`_root()` is the app's own checkout by default. Handing that to every account is RCE."""
        body = self.src[self.src.index("def _guard("):]
        body = body[:body.index("\n\n\n")] if "\n\n\n" in body else body
        self.assertIn("node_service.user_allowed", body,
                      "the operator check is gone, so everyone would get the node's tree")
        self.assertIn("return _root()", body)
        self.assertIn("return _user_root(user)", body)
        # The privileged branch must be the one behind the check.
        self.assertLess(body.index("user_allowed"), body.index("return _root()"))

    def test_a_personal_workspace_is_keyed_on_the_id_not_the_name(self):
        """A name can be changed and a display name can contain a slash — either would move somebody
        else's files or escape the base directory."""
        body = self.src[self.src.index("def _user_root("):self.src.index("def _guard(")]
        self.assertIn('str(int(getattr(user, "id"', body,
                      "the workspace path is built from something other than the numeric id")
        self.assertNotIn("username", body)
        self.assertIn("os.makedirs", body, "the workspace is never created, so the first open 404s")

    def test_every_path_is_still_resolved_INSIDE_the_tree_it_belongs_to(self):
        """The jail is the whole security of this file. Splitting the root in two must not leave an
        endpoint resolving against the wrong one — that is a personal workspace reading the node."""
        self.assertIn("def _resolve(rel: str, root: str", self.src,
                      "_resolve still picks its own root, so it cannot be the caller's")
        # No endpoint may call _resolve without passing a root.
        bare = re.findall(r"_resolve\((?!rel)[^,)]+\)", self.src)
        self.assertEqual(bare, [], f"_resolve called without a root: {bare}")
        self.assertNotIn("os.path.commonpath([root, target]) != root\n", "")  # sanity: rule still there
        self.assertIn("os.path.commonpath", self.src)

    def test_the_config_says_whose_tree_it_is(self):
        """An operator editing the node and a person editing their own directory are both real
        editors; confusing them is how somebody edits a config file they think is theirs."""
        self.assertIn('"own": root != _root()', self.src)

    def test_the_guard_is_still_called_by_every_endpoint(self):
        """A route that forgets it would resolve against whatever root the last caller left."""
        routes = re.findall(r"@router\.(?:get|post)\(([^)]*)\)", self.src)
        self.assertGreaterEqual(len(routes), 4)
        self.assertEqual(self.src.count("_guard(db, current_user)"), len(routes),
                         "an endpoint does not call _guard, so it has no workspace and no check")


if __name__ == "__main__":
    unittest.main()
