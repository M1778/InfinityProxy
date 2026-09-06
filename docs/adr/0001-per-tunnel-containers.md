# ADR-0001: Per-tunnel sing-box containers

Status: **accepted**

Each tunnel is a dedicated `sing-box` container spawned by the Engine, rather
than a sing-box subprocess the Engine manages in-process.

Containers give us process isolation (a crashy tunnel config can never take down
the Engine), independent lifecycle (stop/restart one tunnel without touching the
pool), and uniform fan-out/restart mechanics via the Docker API. Renewal is then
"render a new config, restart the container" — simple and auditable.

**Considered options:**
- *Engine runs sing-box as a subprocess per tunnel* — cheaper (no per-tunnel
  container overhead) but couples crash and resource scope to the Engine, and
  makes per-tunnel restart logic bespoke.
- *One container for the Engine + all tunnels* — rejected outright: a single
  bad config would orphan every tunnel at once.

**Consequences:**
- Tunnels share no sing-box runtime; memory scales with active tunnels. This is
  an accepted cost in exchange for isolation.
- The Engine must reconcile orphaned tunnel containers on boot so a crash never
  leaks listeners (see [architecture.md](../architecture.md)).