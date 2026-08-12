"""
Furi OS — The pairing link: a real address, and a QR of it (2026-08-04)

*(Tier 2, item 5 — the half that makes the remote surface usable by a person)*

The listener, the manifest, the device tokens and the phone page all shipped on
2026-08-03 and were correct. What was missing was the walk from "the feature
exists" to "a human can reach it": `POST /api/remote/pair` returned the literal
string `http://<this-machine>:8765`, nothing in the repo resolved the machine's
LAN address, and `remote_page.py` told an unpaired user to *"scan the QR code in
Settings"* — a QR that did not exist. Pairing meant curl, a copied token, and
hand-typing an IP the user had to go and find.

⚠️ THE TOKEN GOES IN THE FRAGMENT, AND THAT IS NOT COSMETIC. `#t=<token>` is
never sent to a server, never appears in an access log, and never reaches a
proxy — the browser keeps it client-side and `remote_page.py` reads it from
`location.hash`. Putting it in the query string instead would write a working
credential into every log between here and the phone.

⚠️ THREE PURE FUNCTIONS THAT NEVER RAISE. This module is called from a request
handler on the pairing path; a machine with an unusual network stack, or no
network at all, must still be able to pair (over localhost, if that is all there
is). Every failure degrades to something honest — a loopback address, or no QR —
and never to a 500 on the one call the user needs to work.
"""
from __future__ import annotations

import socket
from typing import Optional

from loguru import logger

# RFC 5737 TEST-NET-3, deliberately NOT 8.8.8.8. No packet is ever sent (see
# lan_address), but if this code is ever changed such that one is, a
# documentation-reserved address goes nowhere and tells nobody anything. The
# address only has to be routable enough for the OS to pick an interface.
_ROUTE_PROBE = ("203.0.113.1", 9)

# What we fall back to when there is no usable non-loopback address. It is a
# true statement about a machine with no network, and the phone page served on
# it still works — from that machine.
_LOOPBACK = "127.0.0.1"

# A bind wildcard means "every interface", which is not an address anyone can
# type. Seeing one of these is what sends us to lan_address().
_WILDCARDS = {"", "0.0.0.0", "::", "*"}


def lan_address() -> str:
    """This machine's address on the local network, as a phone would reach it.

    Uses the standard UDP routing probe: open a datagram socket and `connect()`
    it. ⚠️ **NO PACKET IS SENT** — `connect()` on a UDP socket is a purely local
    operation that fixes the peer, and its side effect is that the kernel
    consults the routing table and binds a source address. Reading that back
    with `getsockname()` is how you ask "which of my interfaces would reach the
    outside world?" without any traffic, any DNS, and any dependency.

    Falls back to loopback on any failure — an offline machine, a stack with no
    default route, or a host with only loopback configured.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.connect(_ROUTE_PROBE)
        address = sock.getsockname()[0]
        # A wildcard here means the kernel declined to pick one — treat it as
        # "no usable address" rather than handing back something untypeable.
        if not address or address in _WILDCARDS:
            return _LOOPBACK
        return str(address)
    except Exception as e:  # noqa: BLE001 — see the module docstring: never raise
        logger.debug(f"LAN address could not be resolved, using loopback: {e}")
        return _LOOPBACK
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass


def listen_address(configured_host: str) -> str:
    """The address to PUT IN A LINK, given what the listener was told to bind.

    `REMOTE_HOST` defaults to `0.0.0.0` — correct for binding (every interface,
    so the phone can arrive on any of them) and useless in a URL. Only when the
    user has pinned a specific interface do we echo it back; otherwise we go and
    find the one a phone would actually use.
    """
    host = (configured_host or "").strip()
    if host and host not in _WILDCARDS:
        return host
    return lan_address()


def pairing_link(token: str, *, host: str, port: int) -> str:
    """The complete URL to hand a phone: page, plus its credential.

    The token rides in the FRAGMENT (see the module docstring) because that is
    where `remote_page.py` reads it from and because a fragment never leaves the
    browser.
    """
    return f"http://{listen_address(host)}:{port}/#t={token}"


def qr_data_uri(url: str) -> Optional[str]:
    """An `<img src=…>`-ready SVG QR of `url`, or None if we cannot make one.

    ⚠️ A DATA URI, NOT SVG MARKUP, on purpose: the Settings card renders it with
    a plain `<img>` and therefore never needs `dangerouslySetInnerHTML`. There
    is no path by which this string becomes live DOM.

    `segno` is imported LAZILY and is optional — the `rapidocr-onnxruntime`
    convention. Without it the pairing card still shows a working, copyable
    link; only the convenience of scanning is lost, and the card says so rather
    than rendering a broken image.
    """
    if not url:
        return None
    try:
        import segno  # noqa: PLC0415 — optional dependency, imported at use
    except ImportError:
        logger.info(
            "segno is not installed — pairing shows a copyable link with no QR. "
            "`pip install segno==1.6.6` to enable it."
        )
        return None
    try:
        # scale/border are the printed size; `dark` matches the card's slate UI
        # rather than pure black, which reads as a hole punched in the panel.
        return segno.make(url, error="m").svg_data_uri(scale=4, border=2, dark="#0f172a")
    except Exception as e:  # noqa: BLE001 — a missing QR must never fail pairing
        logger.warning(f"QR could not be generated (non-critical): {e}")
        return None
