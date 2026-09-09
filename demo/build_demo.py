"""Assemble the GitHub Pages demo site under _site/ (pages.yml).

Copies panel/static into _site/, injects demo/demo-bootstrap.js (stubbed
/api + EventSource) ahead of app.js, and pre-renders every markdown doc to
_site/docs/<name>.html plus a _site/docs/index.html landing page.
"""

from __future__ import annotations

import re
import shutil
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from panel import docs as _docs  # noqa: E402

STATIC = ROOT / "panel" / "static"
DEMO = ROOT / "demo"
OUT = ROOT / "_site"

BOOTSTRAP_TAG = '  <script src="demo-bootstrap.js"></script>\n'
_EXT_RE = re.compile(r'href="/docs/([^"]+)"')


def main() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    shutil.copytree(STATIC, OUT)
    shutil.copy2(DEMO / "demo-bootstrap.js", OUT / "demo-bootstrap.js")

    index_path = OUT / "index.html"
    index = index_path.read_text(encoding="utf-8")
    if '<script src="app.js"></script>' not in index:
        raise SystemExit(
            "index.html: app.js include not found; cannot inject bootstrap"
        )
    index = index.replace(
        '<script src="app.js"></script>',
        BOOTSTRAP_TAG + '<script src="app.js"></script>',
    )
    index_path.write_text(index, encoding="utf-8")

    _build_docs(OUT / "docs")


def _build_docs(docs_out: Path) -> None:
    docs_out.mkdir(parents=True, exist_ok=True)
    for entry in _docs.discover():
        title, body = _docs.render_doc(entry)
        html_page = _docs.page_html(title, body, entry["name"])
        html_page = _EXT_RE.sub(r'href="/docs/\1.html"', html_page)
        (docs_out / f"{entry['name']}.html").write_text(html_page, encoding="utf-8")

    entries = _docs.discover()
    links = "".join(
        f'<p><a href="/docs/{escape(e["name"])}.html">{escape(e["label"])}</a></p>'
        for e in entries
    )
    landing = _docs.page_html("Index", f"<h1>Docs</h1>\n{links}", "index")
    landing = _EXT_RE.sub(r'href="/docs/\1.html"', landing)
    (docs_out / "index.html").write_text(landing, encoding="utf-8")


if __name__ == "__main__":
    main()
