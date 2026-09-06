# ADR-0002: SQLite as the state store

Status: **accepted**

The persistence layer is a single SQLite file holding two things: **tunnels**
(id, state, port, credentials, requested/granted counts, `auto_renew`) and a
**node cache** (URIs, source attribution, last latency, assignment). Raw scraped
batches are ephemeral.

SQLite is embedded (no separate database service to run), transactional (safe
single-writer against the always-on Engine), and on the same filesystem as the
engine container — trivially backed up with a bind mount. This is a single-host
control plane; a client/server database (Postgres) would add operational weight
without buying anything v1 needs.

**Considered options:**
- *In-memory only, rebuild on restart* — loses tunnels and credentials on every
  reboot; clients would need to re-create tunnels. Rejected.
- *Postgres* — real multi-writer capacity we don't need for one process on one
  host. Rejected for v1.

**Consequences:**
- A tunnel's nodes are *data the Engine owns*: allocation is recorded so a node
  is never in two tunnels at once (exclusive sets), and is recoverable after
  restart.
- If the Engine ever needs horizontal scaling or multi-host control, this ADR
  gets revisited — SQLite is the deliberate single-node choice.