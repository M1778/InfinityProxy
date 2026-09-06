"""Scraper package: source manifest, HTTPS fetch, URI parsing, dedup."""

from engine.scraper.dedup import dedup
from engine.scraper.fetch import fetch_text
from engine.scraper.parse import parse_feed, parse_uri
from engine.scraper.sources import SOURCES

__all__ = ["SOURCES", "dedup", "fetch_text", "parse_feed", "parse_uri"]
