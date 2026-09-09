"""Render the project's markdown docs as a navigable web page (/docs)."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

import markdown as _md

_TITLE_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)

_GETTING_STARTED = (("readme", "README"), ("context", "Glossary"))
_REFERENCE = (
    ("architecture", "Architecture"),
    ("api", "Control API"),
    ("scraping", "Scraping + filtering"),
    ("dashboard", "Dashboard + ports"),
    ("benchmark", "Benchmark"),
    ("contributing", "Contributing"),
    ("roadmap", "Roadmap"),
)
_ROOT_FILES = {
    "readme": "README.md",
    "context": "CONTEXT.md",
    "contributing": "CONTRIBUTING.md",
    "roadmap": "ROADMAP.md",
}


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def repo_docs_dir() -> Path:
    return repo_root() / "docs"


def discover(docs_dir: Path | str | None = None) -> list[dict[str, Any]]:
    docs_dir = Path(docs_dir) if docs_dir is not None else repo_docs_dir()
    root = repo_root()
    entries: list[dict[str, Any]] = []

    def add(name: str, label: str, path: Path) -> None:
        if path.is_file():
            entries.append(
                {
                    "name": name,
                    "label": label,
                    "path": str(path),
                    "title": _title(path),
                    "group": "getting-started"
                    if name in ("readme", "context")
                    else "decisions"
                    if "adr/" in str(path)
                    else "reference",
                }
            )

    for name, label in _GETTING_STARTED + _REFERENCE:
        if name in _ROOT_FILES:
            add(name, label, root / _ROOT_FILES[name])
        else:
            add(name, label, docs_dir / f"{name}.md")
    for adr in sorted(docs_dir.glob("adr/*.md")):
        add(adr.stem, adr.stem, adr)
    return entries


def _title(path: Path) -> str:
    match = _TITLE_RE.search(path.read_text(encoding="utf-8"))
    return match.group(1).strip() if match else path.stem.replace("-", " ").title()


def find_doc(name: str, docs_dir: Path | str | None = None) -> dict[str, Any] | None:
    clean = name.removesuffix(".md").removesuffix(".html")
    for entry in discover(docs_dir):
        if entry["name"] == clean:
            return entry
    return None


def render_markdown(content: str) -> str:
    return _md.markdown(
        content,
        extensions=["fenced_code", "tables", "sane_lists"],
        output_format="html5",
    )


def render_doc(entry: dict[str, Any]) -> tuple[str, str]:
    content = Path(entry["path"]).read_text(encoding="utf-8")
    return entry["title"], render_markdown(content)


def nav_html(active: str, docs_dir: Path | str | None = None) -> str:
    groups = {
        "getting-started": "Getting started",
        "reference": "Reference",
        "decisions": "Decisions (ADRs)",
    }
    chunks: list[str] = []
    for group, heading in groups.items():
        items = [e for e in discover(docs_dir) if e["group"] == group]
        if not items:
            continue
        chunks.append(f'<div class="doc-group">{html.escape(heading)}</div>')
        for entry in items:
            cur = entry["name"]
            cls = ' class="active"' if cur == active else ""
            chunks.append(
                f'<a href="/docs/{html.escape(cur)}"{cls}>'
                f"{html.escape(entry['label'])}</a>"
            )
    return "\n".join(chunks)


def page_html(
    title: str, body: str, active: str, docs_dir: Path | str | None = None
) -> str:
    nav = nav_html(active, docs_dir)
    return f"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>{html.escape(title)} · InfinityProxy docs</title>
<link rel="stylesheet" href="/app.css">
</head>
<body class="doc-body">
<header class="topbar">
  <a class="brand" href="/">
    <svg class="mark" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"
      aria-hidden="true">
      <rect x="8" y="8" width="8" height="8" rx="1.5"/>
      <path d="M11 12h2" opacity=".9"/>
      <path d="M3 12h3M18 12h3" opacity=".55"/>
    </svg>
    InfinityProxy<span class="brand-sub">docs</span>
  </a>
  <nav class="tabs">
    <a href="/">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
        <rect x="3.5" y="3.5" width="17" height="17" rx="2"/>
        <path d="M3.5 9.5h17M9.5 3.5v17"/>
      </svg>
      <span class="tablabel">Panel</span>
    </a>
  </nav>
  <div id="conn-pill" class="pill" data-state="offline"><span class="dot"></span>
    <span class="plabel">docs</span>
  </div>
</header>
<div class="doc-layout">
  <aside class="doc-nav">{nav}</aside>
  <main class="doc-content">{body}</main>
</div>
</body>
</html>"""
