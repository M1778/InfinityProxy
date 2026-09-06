# ADR-0003: Localhost control API, unauth'd in v1

Status: **accepted**

The control API (`POST/GET /tunnels`, `POST /tunnels/{id}/renew`,
`DELETE /tunnels/{id}`, `GET /status`) binds to `127.0.0.1:8000` and is
**unauthenticated** in v1.

This is deliberate. The API's job in v1 is to be driven by *local* processes on
the same host, so loopback binding is the security boundary; adding auth now
would complicate every client with zero real-world benefit. The engine binds
loopback and the container does not publish the port externally.

**Considered options:**
- *API key / bearer token from day one* — more setup for a localhost-only API;
  keys get stored in clients that don't need protecting from each other. Deferred.
- *Tunnel-scoped API keys for remote control* — the honest long-term answer when
  the engine leaves single-host localhost. On the [roadmap](../ROADMAP.md).

**Consequences:**
- Anyone with shell access on the host already owns the tunnels; auth would not
  change that threat model.
- The docs must keep reminding operators not to publish port 8000.
- When remote control ships (roadmap: *tunnel-scoped API keys*), each key manages
  only its own tunnels — the localhost-unauthenticated semantics stay local.