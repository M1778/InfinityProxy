# ADR-0010: Host system proxy + TUN mode via a privileged hostagent container

Status: **accepted**

InfinityProxy ships host-wide client features (v2rayN-style): **System Proxy**
sets the OS default HTTP(S) proxy to a tunnel; **TUN mode** routes the whole
host's traffic through the tunnel and needs admin privileges. Both actions
mutate the *Docker host* — session proxy settings and a kernel TUN interface —
while the engine and panel are unprivileged loopback containers. The host
surface is a third compose service, the **hostagent**: a privileged container
with its own loopback control API that the engine orchestrates.

## Context

Tunnels are sing-box containers on the host network; their mixed inbound listens
on `127.0.0.1:<port>` with stable credentials (ADR-0001, ADR-0005). To put the
*entire host* behind a tunnel the operator needs code that can:

- touch the desktop session bus to change GNOME/KDE proxy settings, and
- create a TUN interface in the host network namespace and install its routes.

A normal container cannot do either. The engine/panel containers are
deliberately unprivileged (ADR-0003 threat model: loopback is the boundary).
Options considered:

- **Native host agent process** (a small program the operator runs directly on
  the host OS). The only way to reach a *Windows* registry/system proxy or
  wintun from Windows. But it needs a separate install, a second auth story
  (it is not localhost-loopback-in-Docker anymore), and cannot be exercised by
  this repo's tests or live runs.
- **Privileged engine**: give the engine CAP_NET_ADMIN and the bus socket. It
  already holds the docker socket; concentrating host-wide power in one process
  widens the least-privilege gap and couples tunnel lifecycle to host control.
- **Hostagent container** (chosen): a dedicated, *opt-in* privileged companion
  that is the only component with host-wide power. The engine drives it over
  loopback HTTP exactly like it drives the panel; the panel stays a thin proxy.

## Decision

Add a `hostagent` package and compose service:

| Service | Address | Env | Default |
| --- | --- | --- | --- |
| Engine control API | `127.0.0.1:8787` | `INFINITY_PORT` | `8787` |
| Panel | `127.0.0.1:8000` | `INFINITY_PANEL_PORT` | `8000` |
| Hostagent control API | `127.0.0.1:8788` | `INFINITY_HOSTAGENT_PORT` | `8788` |

- **Deployment**: `hostagent` runs the same image (`python -m hostagent`),
  `network_mode: host`, `privileged: true`, and mounts the docker socket and —
  when a desktop user bus is available — `/run/user/<uid>/bus`. It is gated
  behind a compose **profile** (`docker compose --profile host up -d`) and
  `INFINITY_HOST_ENABLED`, so a default stack never runs a privileged
  container. It binds loopback and is unauthenticated like the other two
  control surfaces (ADR-0003 semantics).
- **System proxy**: the hostagent sets the desktop session proxy to
  `127.0.0.1:<tunnel.port>`. Linux: `gsettings` (GNOME) primary, `kwriteconfig5`
  (KDE) secondary; the setter is *capability-detected* per-host, and a host
  without a detected desktop reports `capabilities.proxy: "none"` instead of
  failing. On (disable) it restores `mode 'none'`.
- **TUN mode**: the hostagent spawns a sing-box container (same image/pattern as
  tunnels) with a `tun` inbound (`auto_route`, `strict_route`, host network
  namespace) whose `socks` outbound dials `127.0.0.1:<tunnel.port>` with the
  tunnel's credentials. Disabling removes that container. TUN mode **requires a
  live tunnel**: if the tunnel dies, the host loses uplink until the mode is
  toggled — same property as v2rayN.
- **Target selection**: the engine resolves the target tunnel. `tunnel: "auto"`
  runs *auto-pick-best*: measure every running tunnel's egress (latency through
  the tunnel proxy, then a download throughput sample) and pick the best by
  latency, tie-broken by throughput. Results are cached for
  `INFINITY_HOST_PICK_TTL_S` (default 60s). A panel-visible "re-measure" call
  forces a fresh pass.
- **Cross-platform contract**: the hostagent JSON API
  (`GET /host`, `POST /host/proxy`, `POST /host/tun`) and the engine's
  `/host*` surface are OS-agnostic; the hostagent reports `platform` and
  `capabilities` so a future native/Windows hostagent can implement the same
  contract. Windows is a documented next step, not silent credit.

## Consequences

- A privileged container is added to the stack, but only when explicitly
  enabled via `--profile host` and `INFINITY_HOST_ENABLED`. Anything with shell
  access on the host already owns the desktop and kernel; the hostagent extends
  the ADR-0003 localhost threat model by one *opt-in* component, and the docs
  repeat the loopback-only warning for port 8788 too.
- The engine gains `GET /host`, `POST /host/pick`,
  `POST /host/proxy`, `POST /host/tun`; the panel gains a **Host** view with
  System Proxy and TUN toggles, a per-tunnel target selector, and
  auto-pick-best with measured ping/speed (ADR-0010).
- Hostwide features are testable with fakes (HTTP double for the hostagent,
  injected subprocess/docker for the hostagent's setters) and live-testable on
  a Linux Docker host; Windows TUN/registry behavior is specified but not
  implemented here.