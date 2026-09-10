"""Offline tests for demo/build_demo.py static-link rewriting (GitHub Pages).

The localhost panel serves at `/`, so its sources use site-root-absolute
paths; the Pages demo ships under `<owner>.github.io/<repo>/`, where those
resolve to the domain root and 404. No network, no filesystem.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo.build_demo import rewrite_static_links


def test_rewrite_removes_site_root_absolute_links():
    html = (
        '<a href="/docs/architecture">arch</a>'
        '<a href="/docs/api.html">api</a>'
        '<link rel="stylesheet" href="/app.css">'
        '<a class="brand" href="/">home</a>'
    )
    out = rewrite_static_links(html)
    assert 'href="/' not in out
    assert 'href="architecture.html"' in out
    assert 'href="api.html"' in out
    assert ".html.html" not in out
    assert 'href="../app.css"' in out
    assert 'href="../"' in out


def test_rewrite_maps_markdown_body_links_to_prerendered_pages():
    html = (
        '<a href="./api.md">api</a>'
        '<a href="../architecture.md#sqlite-store">store</a>'
        '<a href="./adr/0001-per-tunnel-containers.md">adr</a>'
        '<a href="../ROADMAP.md#Tunneled-Failure-Attribution">roadmap</a>'
    )
    out = rewrite_static_links(html)
    assert 'href="api.html"' in out
    assert 'href="architecture.html#sqlite-store"' in out
    assert 'href="0001-per-tunnel-containers.html"' in out
    assert 'href="roadmap.html#Tunneled-Failure-Attribution"' in out
    assert ".md" not in out


def test_rewrite_leaves_external_and_fragment_links_alone():
    html = (
        '<a href="https://example.test/x">ext</a>'
        '<a href="#overview">frag</a>'
        '<a href="docs/">relative</a>'
    )
    assert rewrite_static_links(html) == html
