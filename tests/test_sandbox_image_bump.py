"""A CONTAINER OUTLIVES THE IMAGE IT WAS MADE FROM.

`ensure()` asked one question — does a container with this name exist? — so the first sandbox a user
ever got was handed back to them for the life of that user. A box created when the default image was
`debian:stable-slim` was still in service long after the default became a purpose-built image with
chromium, node and the app's dependencies in it. Bumping the tag changed nothing for anybody who had
already run a single command, which is everybody.

The symptom is not an error about images. It is whatever the new image ADDED being absent:
`git: command not found`, in a container whose configured image plainly contains git. Reported
exactly that way, and it cost a round of "the image is wrong" before the container was inspected.

These run against a fake docker so they can assert the ARGV — the bug lives in which commands are
issued, and every one of them succeeds.
"""
import asyncio
import unittest
from unittest import mock

from app.services import sandbox_service as sbx


class _FakeDocker:
    """Records argv and answers the four questions ensure() asks."""

    def __init__(self, existing_image="", running=False):
        self.calls = []
        self.existing_image = existing_image      # "" = no such container
        self.running = running

    async def __call__(self, *args, timeout=None):
        self.calls.append(list(args))
        a = list(args)
        if a[0] == "ps":
            if "-a" in a:                          # exists?
                return (0, "pcai-sbx-7\n" if self.existing_image else "")
            return (0, "pcai-sbx-7\n" if self.running else "")
        if a[0] == "inspect" and "{{.Config.Image}}" in a:
            return (0, self.existing_image + "\n") if self.existing_image else (1, "")
        if a[0] == "image":                        # image inspect → present
            return (0, "")
        if a[0] == "rm":
            self.existing_image = ""               # gone
            return (0, "")
        if a[0] == "run":
            return (0, "deadbeef\n")
        return (0, "")

    def ran(self, verb):
        return [c for c in self.calls if c and c[0] == verb]


def _ensure(fake, image="posterchanai-sandbox:3", active=0):
    sbx._active.clear()
    sbx._locks.clear()
    if active:
        sbx._active["7"] = active
    with mock.patch.object(sbx, "_docker", fake), \
         mock.patch.object(sbx, "_image", lambda: image), \
         mock.patch.object(sbx, "workspace_enabled", lambda: False):
        return asyncio.run(sbx.ensure(7))


class TestTheImageBumpReachesAnExistingContainer(unittest.TestCase):

    def test_a_container_from_an_older_image_is_replaced(self):
        """THE BUG. A box made from a previous default must not be handed back after a bump."""
        f = _FakeDocker(existing_image="debian:stable-slim")
        _ensure(f)
        self.assertTrue(f.ran("rm"),
                        "the stale container was reused — this is `git: command not found` in a "
                        "sandbox whose configured image contains git")
        run = f.ran("run")
        self.assertTrue(run, "nothing was recreated after the removal")
        self.assertIn("posterchanai-sandbox:3", run[0],
                      "recreated from the wrong image")

    def test_a_container_from_the_CURRENT_image_is_left_alone(self):
        """The other half, and the one that would make this destructive: matching is not stale.

        Without it every command would delete and rebuild the container it is about to use."""
        f = _FakeDocker(existing_image="posterchanai-sandbox:3", running=True)
        _ensure(f)
        self.assertFalse(f.ran("rm"), "a current container was destroyed for no reason")
        self.assertFalse(f.ran("run"), "a running container was needlessly recreated")

    def test_a_container_in_use_is_never_pulled_out_from_under_a_command(self):
        """A refcount above zero means somebody's command is mid-flight. One more run on the old
        image is far better than killing a job halfway; the next idle run replaces it."""
        f = _FakeDocker(existing_image="debian:stable-slim", running=True)
        _ensure(f, active=1)
        self.assertFalse(f.ran("rm"), "a container with a live command in it was removed")

    def test_an_unreadable_inspect_is_not_treated_as_stale(self):
        """Failing to ASK must never become a reason to destroy somebody's container — the same
        rule the relay's strict reads follow: 'I could not tell' is not 'it is wrong'."""
        f = _FakeDocker(existing_image="")            # inspect returns rc=1
        f.existing_image = ""
        with mock.patch.object(sbx, "_exists", mock.AsyncMock(return_value=True)):
            _ensure(f)
        self.assertFalse(f.ran("rm"), "an unreadable inspect destroyed the container")

    def test_a_registry_image_is_left_to_the_daemon(self):
        """A custom registry image can be re-pulled or re-tagged under the same name, so a string
        compare says nothing useful — and deleting a user's container on that basis is not ours."""
        f = _FakeDocker(existing_image="debian:stable-slim")
        _ensure(f, image="debian:bookworm-slim")
        self.assertFalse(f.ran("rm"), "a registry-image sandbox was recreated on a name compare")


if __name__ == "__main__":
    unittest.main()
