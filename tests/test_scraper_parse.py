"""Offline unit tests for engine.scraper.parse. No network access."""

from __future__ import annotations

import base64
import json

from engine.models import SourceManifest
from engine.scraper.parse import parse_feed, parse_uri


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _source(**overrides) -> SourceManifest:
    fields = {
        "name": "test",
        "urls": ("https://example.invalid/feed",),
        "cadence_s": 60,
        "encoding": "plain",
        "line_separated": True,
        "license": "unittest",
        "protocols": (
            "vless",
            "vmess",
            "ss",
            "ssr",
            "trojan",
            "tuic",
            "hysteria2",
            "http",
            "socks5",
        ),
    }
    fields.update(overrides)
    return SourceManifest(**fields)


def test_vless_with_query():
    c = parse_uri(
        "vless://e23f2a0b-1c2d-4e5f-8a9b-0c1d2e3f4a5b@v1.example.com:443?encryption=none#fast"
    )
    assert c is not None
    assert c.protocol == "vless"
    assert c.server == "v1.example.com"
    assert c.port == 443
    assert c.user == "e23f2a0b-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
    assert c.source == ""


def test_vless_default_port():
    c = parse_uri("vless://uuid@no-port.example.com")
    assert c is not None
    assert c.port == 443


def test_vless_requires_user():
    assert parse_uri("vless://v1.example.com:443") is None


def test_trojan_unquotes_user():
    c = parse_uri("trojan://pass%40word@t.example.com:443?security=tls")
    assert c is not None
    assert c.protocol == "trojan"
    assert c.user == "pass@word"


def test_vmess_from_base64_json():
    data = {
        "v": "2",
        "ps": "named",
        "add": "1.2.3.4",
        "port": "8443",
        "id": "uuid-9",
        "aid": "0",
    }
    token = "vmess://" + _b64(json.dumps(data, separators=(",", ":")))
    c = parse_uri(token)
    assert c is not None
    assert c.protocol == "vmess"
    assert c.server == "1.2.3.4"
    assert c.port == 8443
    assert c.user == "uuid-9"


def test_vmess_bad_json_is_none():
    token = "vmess://" + _b64("not json")
    assert parse_uri(token) is None


def test_vmess_missing_id_is_none():
    data = {"add": "1.2.3.4", "port": 443}
    token = "vmess://" + _b64(json.dumps(data))
    assert parse_uri(token) is None


def test_ss_plain_method_password():
    c = parse_uri("ss://chacha20-ietf-poly1305:secret@ss.example.com:8388")
    assert c is not None
    assert c.protocol == "ss"
    assert c.user == "secret"
    assert c.port == 8388


def test_ss_base64_method_password():
    token = "ss://" + _b64("aes-256-gcm:secret") + "@5.6.7.8:8388"
    c = parse_uri(token)
    assert c is not None
    assert c.user == "secret"
    assert c.server == "5.6.7.8"


def test_ss_legacy_whole_authority_base64():
    token = "ss://" + _b64("aes-256-gcm:secret@9.9.9.9:8388")
    c = parse_uri(token)
    assert c is not None
    assert c.server == "9.9.9.9"
    assert c.port == 8388
    assert c.user == "secret"


def test_ssr_from_base64_payload():
    payload = "8.8.8.8:6222:origin:aes-256-cfb:plain:aGVsbG8="
    c = parse_uri("ssr://" + _b64(payload))
    assert c is not None
    assert c.protocol == "ssr"
    assert c.server == "8.8.8.8"
    assert c.port == 6222
    assert c.user is None


def test_tuic_leading_user():
    c = parse_uri("tuic://myuser:password@tuic.example.com:443?congestion_control=bbr")
    assert c is not None
    assert c.protocol == "tuic"
    assert c.user == "myuser"


def test_tuic_requires_user():
    assert parse_uri("tuic://tuic.example.com:443") is None


def test_hysteria2_and_hy2_alias():
    c = parse_uri("hysteria2://authpass@hy.example.com:443?insecure=1")
    assert c is not None
    assert c.protocol == "hysteria2"
    assert c.user == "authpass"

    alias = parse_uri("hy2://anotherpass@hy.example.com:444")
    assert alias is not None
    assert alias.protocol == "hysteria2"
    assert alias.port == 444


def test_http_scheme_with_and_without_user():
    bare = parse_uri("http://1.2.3.4:8080")
    assert bare is not None
    assert bare.protocol == "http"
    assert bare.user is None

    with_user = parse_uri("http://alice:secret@1.2.3.4:8080")
    assert with_user is not None
    assert with_user.user == "alice"


def test_plain_host_port_is_http():
    c = parse_uri("43.16.7.90:3128")
    assert c is not None
    assert c.protocol == "http"
    assert c.server == "43.16.7.90"
    assert c.port == 3128


def test_socks5_leading_user():
    c = parse_uri("socks5://proxyuser:proxypass@socks.example.com:1080")
    assert c is not None
    assert c.protocol == "socks5"
    assert c.user == "proxyuser"


def test_socks5_without_user():
    c = parse_uri("socks5://socks.example.com:1080")
    assert c is not None
    assert c.user is None


def test_unsupported_schemes_are_none():
    assert parse_uri("https://example.com/path") is None
    assert parse_uri("socks4://1.2.3.4:1080") is None
    assert parse_uri("wireguard://key@wg.example.com:51820/ip=10.0.0.2/24") is None


def test_invalid_ports_are_none():
    assert parse_uri("1.2.3.4:0") is None
    assert parse_uri("1.2.3.4:65536") is None
    assert parse_uri("vless://u@host:99999") is None


def test_empty_and_comment_lines_are_none():
    assert parse_uri("") is None
    assert parse_uri("   ") is None
    assert parse_uri("# comment line") is None
    assert parse_uri("no colon here") is None


def test_parse_feed_plain_line_separated_tags_source():
    text = (
        "# header comment\n"
        "vless://uuid@v1.example.com:443\n"
        "1.2.3.4:8080\n"
        "ss://" + _b64("aes-256-gcm:secret") + "@5.6.7.8:8388\n"
        "not a usable line\n"
    )
    out = parse_feed(text, _source())
    assert len(out) == 3
    assert all(c.source == "test" for c in out)
    protocols = {c.protocol for c in out}
    assert protocols == {"vless", "http", "ss"}


def test_parse_feed_extracts_embedded_uris():
    text = (
        "Steps: copy the config <vless://abc@e.example.org:443> or "
        "trojan://t@1.2.3.4:443 into your client.\n"
        "Paid option: socks5://u:p@10.0.0.1:1080\n"
    )
    out = parse_feed(text, _source())
    assert len(out) == 3
    assert {c.protocol for c in out} == {"vless", "trojan", "socks5"}
    assert all(c.source == "test" for c in out)


def test_parse_feed_base64_whole_body():
    body = "vless://a@h1.example.com:443\ntrojan://b@h2.example.com:443\n"
    out = parse_feed(_b64(body), _source(encoding="base64", line_separated=False))
    assert len(out) == 2
    assert {c.protocol for c in out} == {"vless", "trojan"}
    assert {c.server for c in out} == {"h1.example.com", "h2.example.com"}


def test_parse_feed_base64_whole_body_garbage_is_empty():
    assert (
        parse_feed("not-base64!!", _source(encoding="base64", line_separated=False))
        == []
    )


def test_parse_feed_base64_per_line():
    first = _b64("vless://a@h1.example.com:443")
    second = _b64("trojan://b@h2.example.com:443")
    out = parse_feed(
        f"{first}\n{second}\n", _source(encoding="base64", line_separated=True)
    )
    assert len(out) == 2
    assert all(c.source == "test" for c in out)


def test_parse_feed_all_comments_is_empty():
    assert parse_feed("# one\n# two\n\n", _source()) == []


def test_parse_feed_skips_unsupported_schemes():
    text = "socks4://1.2.3.4:1080\nhttps://example.com\nwireguard://k@h:51820\n"
    assert parse_feed(text, _source()) == []
