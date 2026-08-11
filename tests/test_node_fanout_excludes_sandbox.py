""""All nodes" means the fleet, and the sandbox is not one of them.

Run: venv-unified/bin/python -m pytest tests/test_node_fanout_excludes_sandbox.py

The per-user Debian sandbox is added to the node registry so it can be PICKED — it is a legitimate
target by name, and for a sandbox-only user it is the ONLY one. But `node all <cmd>` and
`node agent all <goal>` fanned out over that same registry, so every fleet-wide run also ran in a
throwaway container that has no relation to any host.

That is wrong twice. The answers are meaningless — ask every machine its uptime and one of them is a
container that started seconds ago — and it is not free: an agent run in a sandbox spins a container
up, archives its /workspace afterwards and reaps it, so every fan-out paid for a sandbox nobody asked
about, on the same GPU the real work wants.

`fleet_targets` is the one place that decides, shared by both fan-out paths so they cannot drift.
"""
from app.services.command_service.system import fleet_targets


def test_a_fanout_leaves_the_sandbox_out():
    reg = {"local": "local", "nas": "nostr:aabb", "sandbox": "sandbox:7"}
    assert fleet_targets(reg) == {"local": "local", "nas": "nostr:aabb"}


def test_a_sandbox_only_user_still_gets_their_sandbox():
    """They have no fleet, so "all" meaning nothing would be a dead command with a confusing message."""
    assert fleet_targets({"sandbox": "sandbox:7"}) == {"sandbox": "sandbox:7"}


def test_a_placed_sandbox_is_excluded_too():
    """A sandbox pinned to another node is still a sandbox — it is named `sandbox`, and the target
    shape (`sandboxnostr:…`) is about WHERE it runs, not about it being a host."""
    reg = {"local": "local", "sandbox": "sandboxnostr:aabb:ctrl-3"}
    assert fleet_targets(reg) == {"local": "local"}


def test_a_fleet_with_no_sandbox_is_unchanged():
    reg = {"local": "local", "nas": "nostr:aabb", "router": "nostr:ccdd"}
    assert fleet_targets(reg) == reg


def test_it_does_not_hand_back_the_caller_s_dict():
    """The fan-out narrows `nodes`/`_npub_nodes` from this result; returning the registry itself would
    let a later mutation reach back into the picker's list."""
    reg = {"sandbox": "sandbox:7"}
    out = fleet_targets(reg)
    out["local"] = "local"
    assert "local" not in reg


def test_nothing_configured_is_not_an_error():
    assert fleet_targets({}) == {}
    assert fleet_targets(None) == {}


def test_both_fanout_paths_go_through_it():
    """Two copies of this rule is how one of them keeps the sandbox. Asserted on the source: the agent
    fan-out and the shell fan-out must both resolve their targets from the shared helper."""
    import inspect
    from app.services.command_service import system

    src = inspect.getsource(system)
    assert src.count("_fanout()") >= 2, "a fan-out path stopped using the shared rule"
    assert "targets = list(_reg.items())" not in src, (
        "the agent fan-out is back to the whole registry, which includes the sandbox")
