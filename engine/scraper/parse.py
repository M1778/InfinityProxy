"""URI parsing into NodeCandidate, and feed-level parsing per SourceManifest."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import replace
from urllib.parse import unquote

from engine.models import PROXY_PROTOCOLS, NodeCandidate, SourceManifest

SUPPORTED_SCHEMES = frozenset(PROXY_PROTOCOLS)

# hy2 is the alias hysteria2 feeds actually emit.
_SCHEME_ALIASES = {"hy2": "hysteria2"}

# Longest-first so ssr/ vs ss/ and hy2 vs http disambiguate. Excludes https,
# socks4, wireguard and friends, which are unsupported and fall through to
# plain-host parsing (which rejects them).
_SCHEMES = sorted(set(PROXY_PROTOCOLS) | {"hy2"}, key=len, reverse=True)
_SCHEME_RE = re.compile(r"(?<![\w])(?:" + "|".join(_SCHEMES) + r")://[^\s]+")
_TRAILING_JUNK = ",;!<>'\"`}])"


def parse_uri(uri: str) -> NodeCandidate | None:
    """Parse a single node URI into a candidate with source="", or None."""
    token = uri.strip()
    if not token or token.startswith("#"):
        return None

    scheme, sep, rest = token.partition("://")
    if not sep:
        hp = _parse_host_port(token)
        if hp is None:
            return None
        server, port = hp
        return NodeCandidate(token, "http", server, port, None, "")

    scheme = _SCHEME_ALIASES.get(scheme.lower(), scheme.lower())
    if scheme not in SUPPORTED_SCHEMES:
        return None

    authority = _strip_query_fragment(rest)

    if scheme == "vmess":
        return _parse_vmess(token, authority)
    if scheme == "ssr":
        return _parse_ssr(token, authority)
    if scheme == "ss":
        return _parse_ss(token, authority)

    userinfo, sep, hostpart = authority.partition("@")
    if scheme in ("vless", "trojan", "hysteria2") and (not sep or not userinfo):
        return None

    if scheme == "tuic":
        if not sep or not userinfo:
            return None
        user = _leading_user(userinfo)
    elif scheme in ("vless", "trojan") or scheme == "hysteria2":
        user = unquote(userinfo)
    elif scheme in ("http", "socks5"):
        user = _leading_user(userinfo) if sep else None
    else:
        user = None

    default_port = 443 if scheme in ("vless", "vmess", "trojan") else None
    hp = _parse_host_port(hostpart if sep else authority, default_port=default_port)
    if hp is None:
        return None
    server, port = hp
    return NodeCandidate(token, scheme, server, port, user, "")


def parse_feed(text: str, source: SourceManifest) -> list[NodeCandidate]:
    """Parse every node in a feed body, tagging candidates with the source name."""
    if source.encoding == "base64":
        if source.line_separated:
            lines = (
                decoded_line
                for raw in text.splitlines()
                if (decoded_line := _b64_to_text(raw.strip()))
            )
        else:
            decoded = _b64_to_text(text)
            lines = decoded.splitlines() if decoded is not None else ()
    else:
        lines = (raw.strip() for raw in text.splitlines())

    candidates: list[NodeCandidate] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for token in _scheme_tokens(line):
            candidate = parse_uri(token)
            if candidate is not None:
                candidates.append(replace(candidate, source=source.name))
    return candidates


def _parse_vmess(token: str, authority: str) -> NodeCandidate | None:
    payload = _b64_to_text(authority)
    if payload is None:
        return None
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    server = data.get("add")
    port_raw = data.get("port")
    user = data.get("id")
    if not isinstance(server, str) or not server:
        return None
    if isinstance(port_raw, str):
        port = _coerce_port(port_raw)
    elif isinstance(port_raw, int):
        port = port_raw if 1 <= port_raw <= 65535 else None
    else:
        port = None
    if port is None or not isinstance(user, str) or not user:
        return None
    return NodeCandidate(token, "vmess", server, port, user, "")


def _parse_ssr(token: str, authority: str) -> NodeCandidate | None:
    payload = _b64_to_text(authority)
    if payload is None:
        return None
    fields = payload.split(":", 5)
    if len(fields) < 2:
        return None
    server = fields[0]
    port = _coerce_port(fields[1])
    if not server or port is None:
        return None
    # ssr encodes its port in the payload, so no default applied.
    return NodeCandidate(token, "ssr", server, port, None, "")


def _parse_ss(token: str, authority: str) -> NodeCandidate | None:
    # Legacy form: ss://base64(method:password@host:port) with no '@'.
    if "@" not in authority:
        payload = _b64_to_text(authority)
        if payload is None or payload == authority:
            return None
        return _parse_ss(token, payload)

    userinfo, _, hostpart = authority.partition("@")
    if not userinfo or not hostpart:
        return None
    if ":" in userinfo:
        password = userinfo.split(":", 1)[1]
    else:
        plain = _b64_to_text(userinfo)
        password = (
            plain.split(":", 1)[1] if plain is not None and ":" in plain else None
        )
        if password is None:
            password = userinfo
    hp = _parse_host_port(hostpart)
    if hp is None:
        return None
    server, port = hp
    return NodeCandidate(token, "ss", server, port, password, "")


def _scheme_tokens(line: str) -> list[str]:
    matches = list(_SCHEME_RE.finditer(line))
    if not matches:
        return [line]
    return [m.group(0).rstrip(_TRAILING_JUNK) for m in matches]


def _strip_query_fragment(authority: str) -> str:
    return authority.split("?", 1)[0].split("#", 1)[0]


def _parse_host_port(
    authority: str, default_port: int | None = None
) -> tuple[str, int] | None:
    if not authority:
        return None
    host, sep, port_s = authority.rpartition(":")
    if not sep:
        return (authority, default_port) if default_port is not None else None
    if not host:
        return None
    port = _coerce_port(port_s)
    if port is not None:
        return host, port
    return None


def _coerce_port(value: str) -> int | None:
    if not value.isdigit():
        return None
    port = int(value)
    return port if 1 <= port <= 65535 else None


def _leading_user(userinfo: str) -> str | None:
    user = userinfo.split(":", 1)[0]
    return user or None


def _b64_to_text(value: str) -> str | None:
    padded = value + "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except (ValueError, binascii.Error):
        try:
            raw = base64.b64decode(padded)
        except (ValueError, binascii.Error):
            return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
