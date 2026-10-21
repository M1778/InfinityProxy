"""Recovery must bypass cached deployment and retry changed configurations."""

from test_scheduler import HealController, TopUpStore, _alive_node, make_tunnel

from engine.assigner import Assigner
from engine.config import Settings
from engine.models import ProbeResult
from engine.scheduler import Engine
from engine.tunnel.container import TunnelRuntimeUnavailable


def test_self_heal_bypasses_cached_config():
    tunnel = make_tunnel("tu_cached", state="running")
    store = TopUpStore(tunnel, [_alive_node("n1")])
    controller = HealController(running=False)
    engine = Engine(Settings(), store, Assigner(Settings()), controller)
    engine._rendered[tunnel.tunnel_id] = (("n1",), {"cached": True})
    engine._self_heal(tunnel.tunnel_id)
    assert controller.updated == [tunnel.tunnel_id]
    assert engine.tunnel_health(tunnel)["restarts_24h"] == 1


def test_failed_redeploy_retries_on_next_healthy_pass(monkeypatch):
    tunnel = make_tunnel("tu_retry", state="running")
    tunnel.node_count_requested, tunnel.node_count_granted = 2, 1
    store = TopUpStore(tunnel, [_alive_node("n1"), _alive_node("n2")])
    controller = HealController(running=True)
    engine = Engine(Settings(), store, Assigner(Settings()), controller)
    original = controller.update

    def fail(*args):
        raise TunnelRuntimeUnavailable("temporary docker failure")

    monkeypatch.setattr(controller, "update", fail)
    monkeypatch.setattr(
        "engine.scheduler.batch_probe",
        lambda nodes, **kw: {
            n.node_id: ProbeResult(n.node_id, alive=True) for n in nodes
        },
    )
    assert engine.health_check(tunnel) == (0, 1)
    assert tunnel.tunnel_id in engine._pending_redeploy
    monkeypatch.setattr(controller, "update", original)
    assert engine.health_check(tunnel) == (0, 0)
    assert controller.updated == [tunnel.tunnel_id]
    assert tunnel.tunnel_id not in engine._pending_redeploy


def test_last_dead_node_redeploys_blocking_config(monkeypatch):
    tunnel = make_tunnel("tu_empty", state="running")
    tunnel.node_count_requested = tunnel.node_count_granted = 1
    store = TopUpStore(tunnel, [_alive_node("n1")])
    controller = HealController(running=True)
    engine = Engine(Settings(), store, Assigner(Settings()), controller)
    monkeypatch.setattr(
        "engine.scheduler.batch_probe",
        lambda nodes, **kw: {
            n.node_id: ProbeResult(n.node_id, alive=False) for n in nodes
        },
    )
    assert engine.health_check(tunnel) == (0, 0)
    assert engine.health_check(tunnel) == (1, 0)
    assert controller.updated == [tunnel.tunnel_id]
    signature, config = engine._rendered[tunnel.tunnel_id]
    assert signature == ()
    assert config["outbounds"][0]["type"] == "block"
    engine._pending_redeploy.add(tunnel.tunnel_id)
    engine.forget_tunnel(tunnel.tunnel_id)
    assert tunnel.tunnel_id not in engine._pending_redeploy
