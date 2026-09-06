# ADR-0004: MIT project license, MIT-style upstream attribution

Status: **accepted**

InfinityProxy itself is **MIT**. It scrapes node data from external feeds with
mixed licenses — one GPL-3.0 (Epodonios/v2ray-configs), MIT (gfpcom), Unlicense
(FreeFolksOn), and two unlicensed (ebrasha, free-nodes/v2rayfree) — and we do
not redistribute those repositories, only reference raw files and parse the
published URIs.

MIT keeps the project maximally reusable. For the feeds themselves we follow
license hygiene rather than formal obligations: maintain the attribution table
in [docs/scraping.md](../scraping.md), record per-node first-seen source, and
treat scraping of published facts as data access, not derivative work.

**Considered options:**
- *GPL-3.0 for InfinityProxy* — would require derivative works to share alike,
  which penalizes adopters while the copyleft feed's data is only *scraped*.
- *Rejecting GPL feeds* — loses Epodonios (the freshest, ~5-min source) for a
  rigidity benefit we don't need. Rejected.

**Consequences:**
- The [aggregate subscription feed](../ROADMAP.md) is a *redistribution of
  scraped data*, which changes the calculus: it must carry GPL-3.0 notice and
  source attribution for Epodonios-derived nodes. That feature explicitly cannot
  ship until its attribution mechanism is designed.
- Scraper must never claim authorship of upstream node data or strip upstream
  attribution.