"""`posterchan_clients_only` must not lock out the signer people log in WITH.

Run: venv-unified/bin/python -m unittest tests.test_a_remote_signer_can_still_reach_the_relay

The switch exists so this node is not a free general-purpose relay for other people's clients. It
decides at the HANDSHAKE, from the Origin header, the User-Agent and the peer address — and a
remote signer has none of the three: `nostrconnect://` names OUR relay (`_ncRelays()` returns
`CFG.relay_url`) and AMBER dials it, as a native Android app, from a phone on a public address.

So the flow the sign-in screen tells people to use — scan this QR with Amber — was refused at the
handshake with a 403 nobody sees. Measured on this node: the switch is ON and
`nostr_relay_posterchan_origins` is unset, so the only ways in are the derived origin list (browser,
APK, desktop) and the PosterChan User-Agent. A phone signer matches neither.

NIP-55 (Amber signing locally for our own app) and `bunker://` (where the SIGNER names its relay)
were never affected. This is specifically the QR direction.

The fix is not "allow everyone": the signer is let onto the socket and confined to kind 24133, which
is ephemeral — stored nowhere, fanned out only to whoever subscribed — so it grants no storage and
no read access to anything the relay holds.
"""
import unittest

from app.services.nostr_relay import server as S


class _Conn:
    # NOT 203.0.113.x: TEST-NET-3 is IANA special-purpose and Python's `ipaddress` reports it as
    # PRIVATE, so a fixture using it takes the LAN exemption and proves nothing about a phone on
    # the internet. 93.184.216.34 (example.com) is genuinely public.
    def __init__(self, peer="93.184.216.34"):
        self.remote_address = (peer, 54321)


def _verdict(ua="", origin="", peer="93.184.216.34", on=True):
    headers = {}
    if ua:
        headers["User-Agent"] = ua
    if origin:
        headers["Origin"] = origin
    cfg = {"posterchan_clients_only": on,
           "posterchan_origins": ["https://poster.place", "app://posterchan"]}
    return S._posterchan_client_allowed(cfg, headers, _Conn(peer))


class TheHandshakeGate(unittest.TestCase):
    def test_a_posterchan_client_is_admitted_outright(self):
        self.assertIs(_verdict(ua="PosterChan/1.0"), True)
        self.assertIs(_verdict(origin="https://poster.place"), True)

    def test_the_lan_is_admitted_outright(self):
        self.assertIs(_verdict(peer="192.168.0.42"), True)

    def test_the_switch_off_admits_everything(self):
        self.assertIs(_verdict(ua="Amber/1.0", on=False), True)

    def test_a_remote_signer_is_admitted_as_a_signer_not_refused(self):
        """The regression this file exists for: this used to be False, i.e. a 403 at the handshake."""
        v = _verdict(ua="okhttp/4.12.0")          # what a native Android signer looks like
        self.assertEqual(v, "signer",
                         "a remote signer is refused at the handshake — Amber's QR login cannot work")
        self.assertTrue(v, "a signer must still be allowed onto the socket")


class WhatAConfinedSocketMayDo(unittest.TestCase):
    def test_nip46_both_ways_is_allowed(self):
        self.assertTrue(S._restricted_traffic("EVENT", ["EVENT", {"kind": 24133}]))
        self.assertTrue(S._restricted_traffic("REQ", ["REQ", "s", {"kinds": [24133], "#p": ["ab"]}]))

    def test_housekeeping_is_allowed(self):
        for verb in ("CLOSE", "AUTH", "PING", "PONG"):
            self.assertTrue(S._restricted_traffic(verb, [verb, "x"]), verb)

    def test_an_inbound_dm_is_allowed_from_any_client(self):
        """IF THIS RELAY IS SOMEBODY'S DM INBOX, A STRANGER MUST BE ABLE TO DELIVER TO IT.

        A NIP-65/10050 inbox list naming this relay is an instruction to every other client on the
        network: put this person's mail here. With the switch on, a sender on Damus or Amethyst was
        refused at the handshake, so the message was never delivered and NEITHER END WAS TOLD — the
        sender sees a published event, the recipient sees nothing.

        This grants no new policy: kinds 4/13/1059 already bypass the WoT gate (`_DM_KINDS`) because
        a gift wrap's author is a throwaway key, so the relay accepted them from anyone who could
        open a socket. Only the socket was in the way.
        """
        for kind in (1059, 13, 4):
            self.assertTrue(S._restricted_traffic("EVENT", ["EVENT", {"kind": kind}]),
                            f"kind {kind} cannot be delivered — DMs from other clients are lost")

    def test_reading_is_still_confined_to_the_signer_channel(self):
        """Delivering a DM needs no subscription. Reading other people's mail off this relay with a
        third-party client is exactly what the switch excludes, so the write allowance must not
        quietly become a read allowance."""
        self.assertFalse(S._restricted_traffic("REQ", ["REQ", "s", {"kinds": [1059]}]),
                         "a confined socket can subscribe to gift wraps")
        self.assertFalse(S._restricted_traffic("REQ", ["REQ", "s", {"kinds": [4]}]))

    def test_everything_else_is_refused(self):
        self.assertFalse(S._restricted_traffic("EVENT", ["EVENT", {"kind": 1}]),
                         "a confined socket could publish ordinary notes")
        self.assertFalse(S._restricted_traffic("REQ", ["REQ", "s", {"kinds": [1]}]))
        self.assertFalse(S._restricted_traffic("REQ", ["REQ", "s", {"kinds": [24133]}, {"kinds": [1]}]),
                         "one NIP-46 filter must not carry a second filter for everything else")
        self.assertFalse(S._restricted_traffic("REQ", ["REQ", "s", {"authors": ["ab"]}]),
                         "a filter with no `kinds` asks for EVERYTHING and must not pass")
        self.assertFalse(S._restricted_traffic("REQ", ["REQ", "s"]), "a REQ with no filter at all")
        self.assertFalse(S._restricted_traffic("COUNT", ["COUNT", "s", {"kinds": [24133]}]))
        self.assertFalse(S._restricted_traffic("NEG-OPEN", ["NEG-OPEN", "s", {}, ""]))


class TheConfinementIsWired(unittest.TestCase):
    def test_the_handshake_marks_the_socket_and_the_dispatch_checks_it(self):
        import inspect
        src = inspect.getsource(S)
        self.assertIn('setattr(connection, "_pcai_signer_only"', src,
                      "the handshake no longer records that a socket is a confined signer")
        self.assertIn('getattr(conn, "_pcai_signer_only", False) and not _restricted_traffic', src,
                      "nothing enforces the confinement, so a signer socket is a full relay client")


if __name__ == "__main__":
    unittest.main()
