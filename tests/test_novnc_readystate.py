"""The VM console (noVNC) black-screened on the PosterChan desktop app because it wraps global
WebSocket for tor/relay routing and that wrapper's static WebSocket.OPEN/CONNECTING/... are undefined.
noVNC's Websock maps readyState by testing membership in ReadyStates arrays built from those statics,
so they became [undefined, undefined] and readyState returned "unknown" — neither _socketOpen nor
flush ran and the RFB handshake stalled after ProtocolVersion. The arrays must also carry the
standard numeric readyState values (1 = OPEN) and the RTCDataChannel string states, which can never
be undefined."""
import re
from pathlib import Path

WS = (Path(__file__).resolve().parents[1] / "static/vendor/novnc/core/websock.js").read_text()


def _arr(name):
    m = re.search(name + r":\s*\[([^\]]*)\]", WS)
    assert m, f"ReadyStates.{name} not found"
    return m.group(1)


def test_readystate_arrays_carry_numeric_and_string_literals():
    for name, num, word in (("CONNECTING", "0", "connecting"), ("OPEN", "1", "open"),
                            ("CLOSING", "2", "closing"), ("CLOSED", "3", "closed")):
        body = _arr(name)
        assert re.search(r"(^|[,\s])" + num + r"($|[,\s])", body), \
            f"ReadyStates.{name} must include the numeric literal {num} (WebSocket.* statics can be undefined)"
        assert f'"{word}"' in body, f"ReadyStates.{name} must include the string state \"{word}\""
