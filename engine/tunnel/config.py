"""Render a tunnel's sing-box config from its assigned nodes.

Vocabulary per CONTEXT.md and architecture.md "Tunnels": each node's URI is
parsed back into an outbound, all outbounds are grouped under one `urltest`
outbound for latency-weighted rotation (ADR-0005).
"""

from __future__ import annotations

import base64
import binascii
import json as _json
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from engine.models import Node, Tunnel

ROTATOR_TAG = "rotator"
INBOUND_TAG = "mixed-in"
BLOCK_TAG = "blocker"

# Proxy protocol as scraped (models.PROXY_PROTOCOLS) -> sing-box outbound type.
_PROTOCOL_OUTBOUND_TYPE = {
    "vless": "vless",
    "vmess": "vmess",
    "ss": "shadowsocks",
    "trojan": "trojan",
    "tuic": "tuic",
    "hysteria2": "hysteria2",
    "http": "http",
    "socks5": "socks",
}

_URI_DEFAULT_PORT = {
    "vless": 443,
    "vmess": 443,
    "ss": 8388,
    "trojan": 443,
    "tuic": 443,
    "hysteria2": 443,
}

# Ciphers sing-box v1.x accepts for shadowsocks outbounds. Corrupted feed data
# often decodes to a "method" that is not on this list; rendering such a node
# makes sing-box refuse to start (empirically: "unknown method"), so the
# renderer must reject the node up front instead of poisoning the whole config.
_SS_METHODS = frozenset(
    {
        "2022-blake3-aes-128-gcm",
        "2022-blake3-aes-256-gcm",
        "2022-blake3-chacha20-poly1305",
        "aes-128-gcm",
        "aes-192-gcm",
        "aes-256-gcm",
        "chacha20-ietf-poly1305",
        "xchacha20-ietf-poly1305",
        "aes-128-cfb",
        "aes-192-cfb",
        "aes-256-cfb",
        "aes-128-ctr",
        "aes-192-ctr",
        "aes-256-ctr",
        "camellia-128-cfb",
        "camellia-192-cfb",
        "camellia-256-cfb",
        "chacha20-ietf",
        "xchacha20",
        "salsa20",
        "rc4",
        "rc4-md5",
        "none",
    }
)


def node_tag(node: Node) -> str:
    """Tag naming scheme: `n_` + the node's stable pool id."""
    return f"n_{node.node_id}"


def render_config(
    tunnel: Tunnel,
    nodes: list[Node],
    urltest_interval_s: int | None = None,
) -> dict[str, Any]:
    """Build the sing-box config dict for a tunnel over its assigned nodes."""
    tags = [node_tag(n) for n in nodes]
    outbounds = [_outbound_from_node(n) for n in nodes]
    if not outbounds:
        # A degraded tunnel with zero assigned nodes must still start: sing-box
        # rejects an empty urltest, so fall back to a block placeoutbound until
        # the renewal loop tops the tunnel up.
        tags = [BLOCK_TAG]
        outbounds.append({"type": "block", "tag": BLOCK_TAG})
    rotator: dict[str, Any] = {"type": "urltest", "tag": ROTATOR_TAG, "outbounds": tags}
    # urltest's stock health-check interval is 3m; over churny free nodes a dead
    # peer stays selectable far longer than the engine's 30s probe loop. A short
    # interval makes the rotator self-heal: sing-box excludes the failed node
    # from selection on the next cycle, without waiting for a config rewrite.
    if urltest_interval_s is not None:
        rotator["interval"] = f"{urltest_interval_s}s"
    outbounds.append(rotator)
    return {
        "log": {"level": "warn"},
        # One `mixed` inbound serves both SOCKS5 and HTTP on the same port:
        # sing-box rejects two inbounds sharing a listen_port.
        "inbounds": [_mixed_inbound(tunnel)],
        "outbounds": outbounds,
        "route": {"final": ROTATOR_TAG},
    }


def _mixed_inbound(tunnel: Tunnel) -> dict[str, Any]:
    return {
        "type": "mixed",
        "tag": INBOUND_TAG,
        "listen": "0.0.0.0",
        "listen_port": tunnel.port,
        "users": [{"username": tunnel.username, "password": tunnel.password}],
    }


def _outbound_from_node(node: Node) -> dict[str, Any]:
    builder = _BUILDERS.get(node.protocol)
    if builder is None:
        supported = ", ".join(sorted(_PROTOCOL_OUTBOUND_TYPE))
        raise ValueError(
            f"node {node.node_id!r} has unsupported proxy protocol "
            f"{node.protocol!r}; sing-box supports: {supported}; "
            "note ssr was removed from sing-box 1.6.0"
        )
    return builder(node)


def _query(query: str) -> dict[str, list[str]]:
    return parse_qs(query, keep_blank_values=True)


def _first(q: dict[str, list[str]], key: str) -> str | None:
    values = q.get(key)
    return values[0] if values else None


def _b64decode(data: str) -> str:
    # Some feeds percent-encode the userinfo (which is plain base64), e.g.
    # %56%50... instead of VPNCU.... Base64 never contains '%', so unquoting
    # when present is safe and leaves '+' untouched.
    if "%" in data:
        data = unquote(data)
    padded = data + "=" * (-len(data) % 4)
    for alt in (None, b"-_"):
        try:
            raw = base64.b64decode(padded, altchars=alt, validate=False)
            return raw.decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            continue
    raise ValueError(f"invalid base64 payload {data[:16]!r}")


def _host_port(netloc: str, default_port: int | None = None) -> tuple[str, int]:
    netloc = netloc.rpartition("@")[2]
    if netloc.startswith("["):
        host, sep, rest = netloc.partition("]")
        if not sep:
            raise ValueError(f"malformed IPv6 authority {netloc!r}")
        rest = rest[1:] if rest.startswith(":") else ""
        port = rest or (str(default_port) if default_port else "")
    else:
        host, sep, port = netloc.rpartition(":")
        if not sep:
            host = netloc
            port = ""
    if not host:
        raise ValueError(f"malformed node authority {netloc!r}")
    if not port:
        if default_port is None:
            raise ValueError(f"node authority {netloc!r} has no port")
        port = str(default_port)
    return unquote(host), int(port)


def _required_tls(q: dict[str, list[str]], node: Node, label: str) -> dict[str, Any]:
    """TLS for a protocol sing-box cannot run in the clear (trojan/tuic/h2).

    A missing server_name makes sing-box refuse the outbound ("TLS required"),
    which would crash the whole tunnel, so such a node is rejected up front and
    the pool's admission gate demotes it.
    """
    tls = _tls_from_query(q, require_tls=True)
    if not tls or "server_name" not in tls:
        raise ValueError(
            f"node {node.node_id!r}: {label} needs a TLS server_name "
            "(sni=... in the URI) to configure a sing-box outbound"
        )
    return tls


def _tls_from_query(
    q: dict[str, list[str]], require_tls: bool
) -> dict[str, Any] | None:
    security = (_first(q, "security") or "").lower()
    if not require_tls and security not in ("tls", "reality", "xtls"):
        return None
    # sing-box TLS options need enabled=true for any outbound that must run
    # over TLS; without it a hysteria2/tuic/trojan outbound aborts at startup
    # with "TLS required" even when a server_name is present.
    tls: dict[str, Any] = {"enabled": True}
    sni = _first(q, "sni") or _first(q, "serverName") or _first(q, "server_name")
    if sni:
        tls["server_name"] = unquote(sni)
    insecure = _first(q, "allowInsecure") or _first(q, "insecure") or "0"
    if insecure.lower() in ("1", "true", "yes"):
        tls["insecure"] = True
    alpn = _first(q, "alpn")
    if alpn:
        tls["alpn"] = [p.strip() for p in alpn.split(",") if p.strip()]
    fingerprint = _first(q, "fp") or _first(q, "fingerprint")
    if fingerprint:
        tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
    if security == "reality":
        public_key = _first(q, "pbk") or _first(q, "publicKey")
        if public_key:
            reality: dict[str, Any] = {"enabled": True, "public_key": public_key}
            short_id = _first(q, "sid") or _first(q, "shortId")
            if short_id:
                reality["short_id"] = short_id
            tls["reality"] = reality
    return tls


def _transport_from_query(
    q: dict[str, list[str]], key: str = "type"
) -> dict[str, Any] | None:
    transport = (_first(q, key) or "tcp").lower()
    if transport in ("tcp", "none", "raw", ""):
        return None
    path = _first(q, "path") or _first(q, "serviceName")
    host = _first(q, "host")
    if transport in ("ws", "websocket"):
        built: dict[str, Any] = {"type": "ws"}
        if path:
            built["path"] = path
        if host:
            built["headers"] = {"Host": host}
        return built
    if transport in ("grpc", "gun"):
        built = {"type": "grpc"}
        if path:
            built["service_name"] = path
        return built
    if transport == "httpupgrade":
        built = {"type": "httpupgrade"}
        if host:
            built["host"] = host
        if path:
            built["path"] = path
        return built
    if transport in ("h2", "http"):
        built = {"type": "http"}
        if host:
            built["host"] = host
        if path:
            built["path"] = path
        return built
    # mkcp / quic have no sing-box transport; keep the plain TCP dial rather
    # than emit a transport sing-box would reject.
    return None


def _build_vless(node: Node) -> dict[str, Any]:
    u = urlparse(node.uri)
    q = _query(u.query)
    host, port = _host_port(u.netloc, _URI_DEFAULT_PORT[node.protocol])
    out: dict[str, Any] = {
        "type": _PROTOCOL_OUTBOUND_TYPE[node.protocol],
        "tag": node_tag(node),
        "server": host,
        "server_port": port,
        "uuid": unquote(u.username or ""),
    }
    flow = _first(q, "flow")
    if flow:
        out["flow"] = flow
    tls = _tls_from_query(q, require_tls=False)
    if tls:
        out["tls"] = tls
    transport = _transport_from_query(q)
    if transport:
        out["transport"] = transport
    return out


def _build_vmess(node: Node) -> dict[str, Any]:
    # Base64 JSON payloads may contain '/', which urlparse would treat as a
    # path separator, so strip the scheme and fragment by hand.
    body = node.uri.removeprefix("vmess://")
    body = body.partition("#")[0]
    if "@" in body:
        return _build_vmess_uri(node, body)
    return _build_vmess_json(node, body)


def _build_vmess_uri(node: Node, body: str) -> dict[str, Any]:
    userinfo, _, authority = body.partition("@")
    authority, _, query_str = authority.partition("?")
    q = _query(query_str)
    host, port = _host_port(authority, _URI_DEFAULT_PORT[node.protocol])
    out: dict[str, Any] = {
        "type": _PROTOCOL_OUTBOUND_TYPE[node.protocol],
        "tag": node_tag(node),
        "server": host,
        "server_port": port,
        "uuid": unquote(userinfo),
    }
    security = _first(q, "security") or _first(q, "encryption") or "auto"
    if security:
        out["security"] = security
    alter_id = _first(q, "alterId") or _first(q, "aid")
    if alter_id:
        out["alter_id"] = int(alter_id)
    tls = _tls_from_query(q, require_tls=False)
    if tls:
        out["tls"] = tls
    transport = _transport_from_query(q, key="net")
    if transport:
        out["transport"] = transport
    return out


def _build_vmess_json(node: Node, body: str) -> dict[str, Any]:
    try:
        data = _json.loads(_b64decode(body))
    except ValueError as e:
        raise ValueError(f"node {node.node_id!r}: cannot parse vmess payload") from e
    if not isinstance(data, dict):
        raise TypeError(f"node {node.node_id!r}: vmess payload is not a JSON object")

    def field(*keys: str) -> Any:
        for key in keys:
            value = data.get(key)
            if value not in (None, ""):
                return value
        return None

    host = field("add", "host")
    port = field("port")
    uuid = field("id")
    if not host or port is None or not uuid:
        raise ValueError(f"node {node.node_id!r}: vmess payload lacks add/port/id")
    out: dict[str, Any] = {
        "type": _PROTOCOL_OUTBOUND_TYPE[node.protocol],
        "tag": node_tag(node),
        "server": host,
        "server_port": int(port),
        "uuid": uuid,
    }
    security = field("scy", "security") or "auto"
    if security:
        out["security"] = security
    alter_id = field("aid", "alterId")
    if alter_id is not None:
        out["alter_id"] = int(alter_id)
    if (field("tls") or "").lower() in ("tls", "1", "true", "reality", "xtls"):
        q = {key: [value] for key, value in data.items() if value}
        tls = _tls_from_query(q, require_tls=True)
        if not tls:
            tls = {}
        out["tls"] = tls
    transport = field("net")
    if transport and transport.lower() not in ("tcp", "none", "raw", ""):
        path = field("path", "serviceName")
        host_header = field("host")
        q = {"type": [transport]}
        if path:
            q["path"] = [path]
        if host_header:
            q["host"] = [host_header]
        built = _transport_from_query(q)
        if built:
            out["transport"] = built
    return out


def _build_shadowsocks(node: Node) -> dict[str, Any]:
    body = node.uri.removeprefix("ss://")
    body = body.partition("#")[0]
    body, _, query = body.partition("?")
    plugin: str | None = None
    if query.startswith("plugin="):
        plugin = unquote(query.partition("=")[2])
    if "@" in body:
        userinfo, _, authority = body.partition("@")
        if ":" in userinfo:
            method, _, password = userinfo.partition(":")
        else:
            decoded = _b64decode(userinfo)
            method, _, password = decoded.partition(":")
        if not method or not password:
            raise ValueError(
                f"node {node.node_id!r}: ss userinfo lacks method:password"
            )
    else:
        decoded = _b64decode(body)
        userinfo, _, authority = decoded.partition("@")
        if not userinfo:
            raise ValueError(f"node {node.node_id!r}: ss payload lacks userinfo")
        method, _, password = userinfo.partition(":")
    host, port = _host_port(authority, _URI_DEFAULT_PORT[node.protocol])
    method = unquote(method)
    if method not in _SS_METHODS:
        raise ValueError(
            f"node {node.node_id!r}: ss method {method!r} is not supported "
            f"by sing-box; supported: {', '.join(sorted(_SS_METHODS))}"
        )
    out: dict[str, Any] = {
        "type": _PROTOCOL_OUTBOUND_TYPE[node.protocol],
        "tag": node_tag(node),
        "server": host,
        "server_port": port,
        "method": method,
        "password": unquote(password),
    }
    if plugin:
        plugin_name, _, plugin_opts = plugin.partition(";")
        out["plugin"] = unquote(plugin_name)
        if plugin_opts:
            out["plugin_opts"] = unquote(plugin_opts)
    return out


def _build_trojan(node: Node) -> dict[str, Any]:
    u = urlparse(node.uri)
    q = _query(u.query)
    host, port = _host_port(u.netloc, _URI_DEFAULT_PORT[node.protocol])
    out: dict[str, Any] = {
        "type": _PROTOCOL_OUTBOUND_TYPE[node.protocol],
        "tag": node_tag(node),
        "server": host,
        "server_port": port,
        "password": unquote(u.username or ""),
        "tls": _required_tls(q, node, "trojan"),
    }
    transport = _transport_from_query(q)
    if transport:
        out["transport"] = transport
    return out


def _build_tuic(node: Node) -> dict[str, Any]:
    u = urlparse(node.uri)
    q = _query(u.query)
    host, port = _host_port(u.netloc, _URI_DEFAULT_PORT[node.protocol])
    out: dict[str, Any] = {
        "type": _PROTOCOL_OUTBOUND_TYPE[node.protocol],
        "tag": node_tag(node),
        "server": host,
        "server_port": port,
        "uuid": unquote(u.username or ""),
    }
    if u.password:
        out["password"] = unquote(u.password)
    congestion = _first(q, "congestion_control") or _first(q, "congestion-controller")
    if congestion:
        out["congestion_control"] = congestion
    udp_mode = _first(q, "udp_relay_mode") or _first(q, "udp-relay-mode")
    if udp_mode:
        out["udp_relay_mode"] = udp_mode
    out["tls"] = _required_tls(q, node, "tuic")
    return out


def _build_hysteria2(node: Node) -> dict[str, Any]:
    u = urlparse(node.uri)
    q = _query(u.query)
    host, port = _host_port(u.netloc, _URI_DEFAULT_PORT[node.protocol])
    password = unquote(u.username or "")
    if u.password:
        password = f"{password}:{unquote(u.password)}"
    out: dict[str, Any] = {
        "type": _PROTOCOL_OUTBOUND_TYPE[node.protocol],
        "tag": node_tag(node),
        "server": host,
        "server_port": port,
        "password": password,
        "tls": _required_tls(q, node, "hysteria2"),
    }
    obfs_type = _first(q, "obfs")
    if obfs_type:
        obfs: dict[str, Any] = {"type": obfs_type}
        obfs_password = _first(q, "obfs-password")
        if obfs_password:
            obfs["password"] = obfs_password
        out["obfs"] = obfs
    for uri_key, out_key in (("up", "up_mbps"), ("down", "down_mbps")):
        value = _first(q, uri_key)
        if value is not None:
            try:
                out[out_key] = int(float(value))
            except ValueError:
                continue
    return out


def _build_http(node: Node) -> dict[str, Any]:
    u = urlparse(node.uri)
    host, port = _host_port(u.netloc, 80)
    out: dict[str, Any] = {
        "type": _PROTOCOL_OUTBOUND_TYPE[node.protocol],
        "tag": node_tag(node),
        "server": host,
        "server_port": port,
    }
    if u.username:
        out["username"] = unquote(u.username)
        out["password"] = unquote(u.password or "")
    return out


def _build_socks(node: Node) -> dict[str, Any]:
    u = urlparse(node.uri)
    host, port = _host_port(u.netloc, 1080)
    out: dict[str, Any] = {
        "type": _PROTOCOL_OUTBOUND_TYPE[node.protocol],
        "tag": node_tag(node),
        "server": host,
        "server_port": port,
        "version": "5",
    }
    if u.username:
        out["username"] = unquote(u.username)
        out["password"] = unquote(u.password or "")
    return out


_BUILDERS: dict[str, Callable[[Node], dict[str, Any]]] = {
    "vless": _build_vless,
    "vmess": _build_vmess,
    "ss": _build_shadowsocks,
    "trojan": _build_trojan,
    "tuic": _build_tuic,
    "hysteria2": _build_hysteria2,
    "http": _build_http,
    "socks5": _build_socks,
}
