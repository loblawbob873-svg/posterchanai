"""The terminal's failure sentence names what actually went wrong (2026-10-23).

paramiko's RejectPolicy says "Server '[127.0.0.1]:45037' not found in known_hosts"; the "not found" rule for a missing
key file matched it first, so an unknown host key was reported as "the key file configured for this host is not on
the server" -- sending people to look for a file that was fine.
"""
from app.routers.ssh_term import _why_kind


def test_an_unknown_host_key_is_not_reported_as_a_missing_key_file():
    said = _why_kind('SSHException', "Server '[127.0.0.1]:45037' not found in known_hosts")
    assert 'known_hosts' in said and 'key file' not in said, said


def test_a_missing_key_file_is_still_said():
    said = _why_kind('FileNotFoundError', "[Errno 2] No such file or directory: '/srv/keys/id_ed25519'")
    assert said == 'the key file configured for this host is not on the server', said
