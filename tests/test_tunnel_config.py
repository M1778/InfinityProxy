"""Offline tests for tunnel config rendering and container control.

Neither the Docker SDK API nor a network connection is required; the docker
client is a recording fake.
"""

from __future__ import annotations

import base64
import builtins
import io
import json
import tarfile

import pytest

from engine.config import Settings
from engine.models import Node, Tunnel
from engine.tunnel import ROTATOR_TAG, ContainerController, TunnelRuntimeUnavailable
from engine.tunnel.config import render_config

# --- fixtures / domain helpers -------------------------------------------------

VMESS_PAYLOAD = {
    "v": "2",
    "ps": "vmess-node",
    "add": "vmess.example.com",
    "port": "443",
    "id": "8f0de1d2-4e6b-4e5a-9f2c-10f2e9d8c7b6",
    "aid": "0",
    "scy": "auto",
    "net": "ws",
    "type": "none",
    "host": "cdn.example.com",
    "path": "/vmess",
    "tls": "tls",
    "sni": "vmess.example.com",
}

VLESS_URI = (
    "vless://b74e0f9a-c7d2-4a1b-9f3e-5d2c4b1a6e8f@vless.example.com:443"
    "?type=tcp&security=reality&flow=xtls-rprx-vision&sni=www.example.com"
    "&fp=chrome&pbk=testPublicKey&sid=0123456789abcdef#reality-node"
)
VMESS_URI = "vmess://" + base64.b64encode(json.dumps(VMESS_PAYLOAD).encode()).decode()
VMESS_URI_FORM = (
    "vmess://8f0de1d2-4e6b-4e5a-9f2c-10f2e9d8c7b6@vmess2.example.com:8443"
    "?encryption=none&security=tls&type=tcp&sni=vmess2.example.com#vmess2"
)
SS_URI = (
    "ss://"
    + base64.b64encode(b"aes-256-gcm:changeit-now").decode()
    + "@ss.example.com:8388?plugin=obfs-local%3Bobfs%3Dhttp#ss-node"
)
SS_URI_PLAIN = "ss://aes-128-gcm:legacypass@ss.example.com:9000#ss2"
SS_URI_FULL_B64 = (
    "ss://"
    + base64.b64encode(b"chacha20-ietf-poly1305:password@ss.example.com:1122").decode()
    + "#ss3"
)
TROJAN_URI = (
    "trojan://trojan-password@trojan.example.com:443"
    "?sni=trojan.example.com&allowInsecure=1#trojan-node"
)
TUIC_UUID = "0b8f10e0-1111-4d9e-8a2b-c3d4e5f6a7b8"
TUIC_URI = (
    f"tuic://{TUIC_UUID}:tuic-secret@tuic.example.com:443"
    "?congestion_control=bbr&alpn=h3&udp_relay_mode=native"
    "&sni=tuic.example.com#tuic-node"
)
HY2_URI = (
    "hysteria2://hy2-secret@hy2.example.com:443"
    "?insecure=1&obfs=salamander&obfs-password=obfssecret"
    "&sni=hy2.example.com#hy2-node"
)
HTTP_URI = "http://http-user:http-pass@http.example.com:8080#http-node"
SOCKS5_URI = "socks5://socks-user:socks-pass@socks.example.com:1080#socks-node"

# protocol, uri, expected sing-box outbound type, server, port
ROUND_TRIPS = [
    ("vless", VLESS_URI, "vless", "vless.example.com", 443),
    ("vmess", VMESS_URI, "vmess", "vmess.example.com", 443),
    ("vmess", VMESS_URI_FORM, "vmess", "vmess2.example.com", 8443),
    ("ss", SS_URI, "shadowsocks", "ss.example.com", 8388),
    ("ss", SS_URI_PLAIN, "shadowsocks", "ss.example.com", 9000),
    ("ss", SS_URI_FULL_B64, "shadowsocks", "ss.example.com", 1122),
    ("trojan", TROJAN_URI, "trojan", "trojan.example.com", 443),
    ("tuic", TUIC_URI, "tuic", "tuic.example.com", 443),
    ("hysteria2", HY2_URI, "hysteria2", "hy2.example.com", 443),
    ("http", HTTP_URI, "http", "http.example.com", 8080),
    ("socks5", SOCKS5_URI, "socks", "socks.example.com", 1080),
]


def make_node(
    node_id: str,
    uri: str,
    protocol: str,
    server: str,
    port: int,
) -> Node:
    return Node(
        node_id=node_id,
        uri=uri,
        protocol=protocol,
        server=server,
        port=port,
        user=None,
        source="test",
        first_seen_s=0.0,
    )


def make_tunnel(tunnel_id: str = "tu_1") -> Tunnel:
    return Tunnel(
        tunnel_id=tunnel_id,
        port=10244,
        username="alice",
        password="s3cret",
    )


def outbound_by_tag(cfg: dict, tag: str) -> dict:
    return next(o for o in cfg["outbounds"] if o["tag"] == tag)


# --- render_config ------------------------------------------------------------


def test_render_config_shape_and_inbounds() -> None:
    tunnel = make_tunnel()
    nodes = [
        make_node("aaa", VLESS_URI, "vless", "vless.example.com", 443),
        make_node("bbb", VMESS_URI, "vmess", "vmess.example.com", 443),
        make_node("ccc", SS_URI, "ss", "ss.example.com", 8388),
    ]
    cfg = render_config(tunnel, nodes)

    json.dumps(cfg)  # must be JSON-serializable

    assert cfg["log"]["level"] == "warn"
    # One mixed inbound serves SOCKS5 + HTTP together; sing-box cannot share a
    # listen_port across two inbounds.
    assert [ib["type"] for ib in cfg["inbounds"]] == ["mixed"]
    for inbound in cfg["inbounds"]:
        assert inbound["listen"] == "0.0.0.0"
        assert inbound["listen_port"] == tunnel.port
        assert inbound["users"] == [
            {"username": tunnel.username, "password": tunnel.password}
        ]

    tags = ["n_aaa", "n_bbb", "n_ccc"]
    assert [o["tag"] for o in cfg["outbounds"][:-1]] == tags
    assert cfg["route"]["final"] == ROTATOR_TAG
    rotator = cfg["outbounds"][-1]
    assert rotator["type"] == "urltest"
    assert rotator["tag"] == ROTATOR_TAG
    assert rotator["outbounds"] == tags


def test_ss_percent_encoded_userinfo_decodes() -> None:
    from urllib.parse import quote

    userinfo = quote(base64.b64encode(b"aes-256-gcm:p%40ssword").decode(), safe="")
    uri = f"ss://{userinfo}@ss.example.com:8443#pct"
    node = make_node("nd_pct_ss", uri, "ss", "ss.example.com", 8443)
    cfg = render_config(make_tunnel(), [node])
    out = next(o for o in cfg["outbounds"] if o["type"] == "shadowsocks")
    assert out["method"] == "aes-256-gcm"
    assert out["password"] == "p@ssword"


def test_ss_unknown_method_rejected() -> None:
    bogus = base64.b64encode(b"definitely-not-a-cipher:secret").decode()
    node = make_node(
        "nd_bad_algo",
        f"ss://{bogus}@ss.example.com:8388#a",
        "ss",
        "ss.example.com",
        8388,
    )
    with pytest.raises(ValueError, match="method"):
        render_config(make_tunnel(), [node])


@pytest.mark.parametrize(
    ("uri", "protocol"),
    [
        ("trojan://pw@trojan.example.com:443#no-tls", "trojan"),
        (f"tuic://{TUIC_UUID}:pw@tuic.example.com:443#no-tls", "tuic"),
        ("hysteria2://secret@hy2.example.com:443#no-tls", "hysteria2"),
    ],
)
def test_tls_mandatory_protocols_require_server_name(uri: str, protocol: str) -> None:
    from urllib.parse import urlparse

    host = urlparse(uri).hostname or "example.com"
    port = urlparse(uri).port or 443
    node = make_node(f"nd_tls_{protocol}", uri, protocol, host, port)
    with pytest.raises(ValueError, match="server_name"):
        render_config(make_tunnel(), [node])


def test_render_config_no_nodes_falls_back_to_block() -> None:
    cfg = render_config(make_tunnel(), [])
    placements = [o for o in cfg["outbounds"] if o["tag"] != ROTATOR_TAG]
    assert [o["type"] for o in placements] == ["block"]
    rotator = next(o for o in cfg["outbounds"] if o["tag"] == ROTATOR_TAG)
    assert rotator["outbounds"] == [placements[0]["tag"]]
    assert cfg["route"]["final"] == ROTATOR_TAG


@pytest.mark.parametrize(
    "protocol,uri,expected_type,expected_server,expected_port",
    ROUND_TRIPS,
)
def test_round_trip_uri_to_outbound(
    protocol: str,
    uri: str,
    expected_type: str,
    expected_server: str,
    expected_port: int,
) -> None:
    tunnel = make_tunnel()
    node = make_node("rt1", uri, protocol, expected_server, expected_port)
    cfg = render_config(tunnel, [node])
    outbound = outbound_by_tag(cfg, "n_rt1")
    assert outbound["type"] == expected_type
    assert outbound["server"] == expected_server
    assert outbound["server_port"] == expected_port


def test_round_trip_carries_secrets_and_options() -> None:
    tunnel = make_tunnel()
    nodes = [
        make_node("t1", TUIC_URI, "tuic", "tuic.example.com", 443),
        make_node("t2", SS_URI, "ss", "ss.example.com", 8388),
        make_node("t3", HY2_URI, "hysteria2", "hy2.example.com", 443),
        make_node("t4", HTTP_URI, "http", "http.example.com", 8080),
        make_node("t5", SOCKS5_URI, "socks5", "socks.example.com", 1080),
    ]
    cfg = render_config(tunnel, nodes)

    assert outbound_by_tag(cfg, "n_t1")["uuid"] == TUIC_UUID
    assert outbound_by_tag(cfg, "n_t1")["password"] == "tuic-secret"
    assert outbound_by_tag(cfg, "n_t1")["congestion_control"] == "bbr"
    assert outbound_by_tag(cfg, "n_t1")["udp_relay_mode"] == "native"

    ss = outbound_by_tag(cfg, "n_t2")
    assert ss["method"] == "aes-256-gcm"
    assert ss["password"] == "changeit-now"
    assert ss["plugin"] == "obfs-local"
    assert ss["plugin_opts"] == "obfs=http"

    hy2 = outbound_by_tag(cfg, "n_t3")
    assert hy2["password"] == "hy2-secret"
    assert hy2["tls"]["insecure"] is True
    assert hy2["tls"]["server_name"] == "hy2.example.com"
    assert hy2["obfs"] == {"type": "salamander", "password": "obfssecret"}

    assert outbound_by_tag(cfg, "n_t4")["username"] == "http-user"
    assert outbound_by_tag(cfg, "n_t4")["password"] == "http-pass"
    assert outbound_by_tag(cfg, "n_t5")["username"] == "socks-user"
    assert outbound_by_tag(cfg, "n_t5")["password"] == "socks-pass"
    assert outbound_by_tag(cfg, "n_t5")["version"] == "5"


def test_vless_reality_renders_tls_reality() -> None:
    tunnel = make_tunnel()
    node = make_node("vr", VLESS_URI, "vless", "vless.example.com", 443)
    outbound = outbound_by_tag(render_config(tunnel, [node]), "n_vr")
    assert outbound["flow"] == "xtls-rprx-vision"
    assert outbound["tls"]["enabled"] is True
    assert outbound["tls"]["server_name"] == "www.example.com"
    assert outbound["tls"]["reality"]["enabled"] is True
    assert outbound["tls"]["reality"]["public_key"] == "testPublicKey"
    assert outbound["tls"]["reality"]["short_id"] == "0123456789abcdef"


def test_vmess_json_payload_round_trips() -> None:
    tunnel = make_tunnel()
    node = make_node("vj", VMESS_URI, "vmess", "vmess.example.com", 443)
    outbound = outbound_by_tag(render_config(tunnel, [node]), "n_vj")
    assert outbound["uuid"] == VMESS_PAYLOAD["id"]
    assert outbound["security"] == "auto"
    assert outbound["alter_id"] == 0
    assert outbound["transport"] == {
        "type": "ws",
        "path": "/vmess",
        "headers": {"Host": "cdn.example.com"},
    }


@pytest.mark.parametrize("protocol", ["ssr", "naive", "wireguard", "socks4"])
def test_unsupported_protocol_raises_value_error(protocol: str) -> None:
    tunnel = make_tunnel()
    node = make_node("dead", f"{protocol}://x@1.2.3.4:5", protocol, "1.2.3.4", 5)
    with pytest.raises(ValueError, match="unsupported proxy protocol"):
        render_config(tunnel, [node])


# --- ContainerController ------------------------------------------------------


class FakeContainer:
    def __init__(self, name: str, registry: dict[str, FakeContainer]) -> None:
        self.name = name
        self._registry = registry
        self.stopped = False
        self.removed = False
        self.restarted = False
        self.started = False
        self.archives: list[tuple[str, dict[str, bytes]]] = []
        self.stop_raise: Exception | None = None
        self.put_archive_raise: Exception | None = None

    def put_archive(self, destination: str, archive: bytes) -> None:
        if self.put_archive_raise is not None:
            raise self.put_archive_raise
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as tar:
            contents = {
                member.name: tar.extractfile(member).read()
                for member in tar.getmembers()
                if member.isfile()
            }
        self.archives.append((destination, contents))

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True
        if self.stop_raise is not None:
            raise self.stop_raise

    def remove(self, force: bool = False) -> None:
        self.removed = True
        self.remove_force = force
        self._registry.pop(self.name, None)

    def restart(self) -> None:
        self.restarted = True


class StopAlready(Exception):
    """Stand-in for docker's 304 `container already stopped` APIError."""


class FakeContainers:
    def __init__(self) -> None:
        self.registry: dict[str, FakeContainer] = {}
        self.create_calls: list[dict] = []
        self.fail_on_put: dict[str, Exception] = {}

    def create(
        self, image: str, name: str | None = None, **kwargs: object
    ) -> FakeContainer:
        self.create_calls.append({"image": image, "name": name, **kwargs})
        container = FakeContainer(name, self.registry)
        if name in self.fail_on_put:
            container.put_archive_raise = self.fail_on_put[name]
        self.registry[name] = container
        return container

    def get(self, name: str) -> FakeContainer:
        if name not in self.registry:
            raise KeyError(name)
        return self.registry[name]

    def list(
        self, all: bool = False, filters: dict | None = None, **kwargs: object
    ) -> list[FakeContainer]:
        prefix = (filters or {}).get("name", "")
        containers = list(self.registry.values())
        if prefix:
            containers = [c for c in containers if prefix in c.name]
        return containers


class FakeDocker:
    def __init__(self) -> None:
        self.containers = FakeContainers()


SETTINGS = Settings()


def archived_config_of(container: FakeContainer) -> dict:
    return json.loads(container.archives[-1][1]["sing-box/config.json"])


def test_start_creates_container_shipping_config_via_put_archive() -> None:
    fake = FakeDocker()
    controller = ContainerController(SETTINGS, docker_client=fake)
    config = {"log": {"level": "warn"}}

    controller.start(make_tunnel("tu_1"), config)

    assert len(fake.containers.create_calls) == 1
    call = fake.containers.create_calls[0]
    assert call["name"] == "infinity-tu_1"
    assert call["image"] == SETTINGS.singbox_image
    assert call["network_mode"] == "host"
    assert call["command"] == ["run", "-c", "/etc/sing-box/config.json"]
    assert call["restart_policy"] == {"Name": "always"}
    assert "volumes" not in call
    container = fake.containers.get("infinity-tu_1")
    assert container.started is True
    assert container.archives[0][0] == "/etc/"
    assert archived_config_of(container) == config


def test_start_removes_partial_container_when_shipment_fails() -> None:
    fake = FakeDocker()
    fake.containers.fail_on_put["infinity-tu_2"] = RuntimeError("config dir missing")
    controller = ContainerController(SETTINGS, docker_client=fake)

    with pytest.raises(RuntimeError, match="config dir missing"):
        controller.start(make_tunnel("tu_2"), {"log": {"level": "warn"}})

    assert "infinity-tu_2" not in fake.containers.registry


def test_update_replaces_container_with_new_config() -> None:
    fake = FakeDocker()
    controller = ContainerController(SETTINGS, docker_client=fake)
    old_config = {"log": {"level": "warn"}}
    new_config = {"log": {"level": "debug"}}

    controller.start(make_tunnel("tu_2"), old_config)
    controller.update("tu_2", new_config)

    assert len(fake.containers.create_calls) == 2
    assert fake.containers.create_calls[1]["name"] == "infinity-tu_2"
    updated = fake.containers.get("infinity-tu_2")
    assert updated.started is True
    assert archived_config_of(updated) == new_config


def test_stop_stops_and_removes_container() -> None:
    fake = FakeDocker()
    controller = ContainerController(SETTINGS, docker_client=fake)
    controller.start(make_tunnel("tu_3"), {"log": {"level": "warn"}})
    container = fake.containers.get("infinity-tu_3")

    controller.stop("tu_3")

    assert container.stopped is True
    assert container.removed is True
    with pytest.raises(KeyError):
        fake.containers.get("infinity-tu_3")


def test_stop_unknown_tunnel_is_a_noop() -> None:
    fake = FakeDocker()
    controller = ContainerController(SETTINGS, docker_client=fake)
    controller.stop("never-started")  # must not raise


def test_stop_tolerates_already_stopped_304_error() -> None:
    fake = FakeDocker()
    controller = ContainerController(SETTINGS, docker_client=fake)
    controller.start(make_tunnel("tu_4"), {"log": {"level": "warn"}})
    container = fake.containers.get("infinity-tu_4")
    container.stop_raise = StopAlready("container already stopped")

    controller.stop("tu_4")

    assert container.removed is True
    assert container.remove_force is True
    assert "infinity-tu_4" not in fake.containers.registry


def test_stop_removes_running_container_when_stop_fails() -> None:
    fake = FakeDocker()
    controller = ContainerController(SETTINGS, docker_client=fake)
    controller.start(make_tunnel("tu_5"), {"log": {"level": "warn"}})
    container = fake.containers.get("infinity-tu_5")
    container.stop_raise = RuntimeError("daemon hiccup")

    controller.stop("tu_5")

    # A failed stop must not leave a running container behind.
    assert container.removed is True
    assert container.remove_force is True
    assert "infinity-tu_5" not in fake.containers.registry


def test_restart_restarts_existing_container() -> None:
    fake = FakeDocker()
    controller = ContainerController(SETTINGS, docker_client=fake)
    controller.start(make_tunnel("tu_r"), {"log": {"level": "warn"}})
    container = fake.containers.get("infinity-tu_r")

    controller.restart("tu_r")

    assert container.restarted is True


def test_reconcile_removes_orphans_and_keeps_expected() -> None:
    fake = FakeDocker()
    controller = ContainerController(SETTINGS, docker_client=fake)
    for tunnel_id in ("a", "b", "orphan"):
        controller.start(make_tunnel(tunnel_id), {})
    orphan = fake.containers.get("infinity-orphan")

    controller.reconcile(expected_ids={"a", "b"})

    assert orphan.removed is True
    assert "infinity-orphan" not in fake.containers.registry
    assert fake.containers.get("infinity-a").removed is False
    assert fake.containers.get("infinity-b").removed is False


def test_reconcile_ignores_unrelated_containers() -> None:
    fake = FakeDocker()
    controller = ContainerController(SETTINGS, docker_client=fake)
    controller.start(make_tunnel("mine"), {})
    mine = fake.containers.get("infinity-mine")
    unrelated = FakeContainer("other-app", fake.containers.registry)
    fake.containers.registry["other-app"] = unrelated

    controller.reconcile(expected_ids=set())

    assert mine.removed is True
    assert unrelated.removed is False


def test_injected_client_used_directly() -> None:
    fake = FakeDocker()
    controller = ContainerController(SETTINGS, docker_client=fake)
    assert controller.docker is fake


def test_missing_docker_sdk_raises_tunnel_runtime_unavailable(monkeypatch) -> None:
    real_import = builtins.__import__

    def no_docker(name: str, *args, **kwargs):
        if name.split(".")[0] == "docker":
            raise ImportError("docker not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_docker)
    controller = ContainerController(SETTINGS, docker_client=None)
    with pytest.raises(TunnelRuntimeUnavailable, match="Docker"):
        _ = controller.docker
