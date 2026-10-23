"""A TEST-OWNED shell keeper: app/services/ssh_keeper.py's real server, in its own process, on a temp socket.

Run by tests/test_ssh_terminal_far_end.py as `python tests/ssh_keeper_runner.py`. The keeper is the
production code unchanged; only what a node's settings and known_hosts would supply comes from the test
(PC_TEST_KEEPER_CONF = the settings as JSON, PC_TEST_KNOWN_HOSTS = the server account's known_hosts), and the
socket from POSTERCHANAI_SSH_SOCK -- so it can never be the node's own keeper at <checkout>/.run/ssh-keeper.sock.
"""
import asyncio
import json
import os
import sys



def main():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if not os.environ.get('POSTERCHANAI_SSH_SOCK'):
        sys.exit('refusing to start without POSTERCHANAI_SSH_SOCK: that would be the production keeper socket')
    import paramiko

    from app.services import ssh_keeper, ssh_service
    conf = json.loads(os.environ.get('PC_TEST_KEEPER_CONF') or '{}')
    known = os.environ.get('PC_TEST_KNOWN_HOSTS') or ''
    ssh_service._get = lambda k, d='': conf.get(k) or d
    paramiko.SSHClient.load_system_host_keys = lambda cli, filename=None: cli.load_host_keys(known) if known else None
    asyncio.run(ssh_keeper._serve())


if __name__ == '__main__':
    main()
