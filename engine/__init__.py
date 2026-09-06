# InfinityProxy engine package.

# Public module paths (owned by teams, see docs/architecture.md
# "Planned repository layout"):
#   engine.config    - settings from the environment
#   engine.models    - canonical domain dataclasses
#   engine.db        - SQLite store (tunnels + node cache)
#   engine.scraper   - sources, fetch, parse, dedup
#   engine.filter    - liveness probing (batched)
#   engine.assigner  - port/credential allocator + exclusive assignment
#   engine.tunnel    - sing-box config rendering + container control
#   engine.pool      - facade joining scraper -> filter -> db
#   engine.scheduler - background fetch + health loops
#   engine.app       - Flask control API + dashboard
