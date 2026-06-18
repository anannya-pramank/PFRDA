"""HTTP scraping layer for the PFRDA watcher.

Loads ``sources.yaml`` and yields :class:`~pfrda_watcher.models.Mention` objects
for every document row found on each enabled source page.

Two scraping modes are supported (set per-source in the YAML):

``teaser``
    Scrapes the homepage "What's New" widget — a small, fast snapshot of the
    most recent items.  No pagination.

``archive``
    Scrapes a full listing page.  Supports optional pagination via a
    query-string parameter (e.g. ``?start=2``).  Disabled sources or sources
    with TODO selectors are skipped automatically.

The module is deliberately stateless: it does not touch the database or the
seen-set — that is the repository's job.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Generator, Iterator
from urllib.parse import urlencode, urljoin, urlparse, urlunparse, parse_qs

import requests
import yaml
from bs4 import BeautifulSoup

from .models import Mention

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    """Parse *sources.yaml* and return the config dict.

    Merges each source entry with the top-level ``defaults`` so callers never
    have to check for missing keys.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    defaults = raw.get("defaults", {})
    sources = []
    for src in raw.get("sources", []):
        merged = _deep_merge(defaults, src)
        sources.append(merged)

    return {"defaults": defaults, "sources": sources}


def _deep_merge(base: dict, override: dict) -> dict:
    """Return a new dict that is *base* updated recursively with *override*."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def scrape_all(cfg: dict) -> Iterator[Mention]:
    """Yield :class:`Mention` objects for every enabled source in *cfg*."""
    session = _build_session(cfg)
    for source in cfg.get("sources", []):
        if not source.get("enabled", True):
            logger.debug("Skipping disabled source: %s", source.get("id"))
            continue
        if _has_todo_selectors(source):
            logger.warning(
                "Skipping source %s — selectors contain TODO placeholders",
                source.get("id"),
            )
            continue
        try:
            yield from _scrape_source(source, session, cfg)
        except Exception as exc:  # noqa: BLE001
            logger.error("Error scraping %s: %s", source.get("id"), exc, exc_info=True)


# ---------------------------------------------------------------------------
# Per-source dispatch
# ---------------------------------------------------------------------------

def _scrape_source(
    source: dict, session: requests.Session, cfg: dict
) -> Generator[Mention, None, None]:
    mode = source.get("mode", "teaser")
    if mode == "teaser":
        yield from _scrape_teaser(source, session)
    elif mode == "archive":
        yield from _scrape_archive(source, session)
    else:
        logger.warning("Unknown mode %r for source %s — skipping", mode, source.get("id"))


# ---------------------------------------------------------------------------
# Teaser mode  (homepage widget, no pagination)
# ---------------------------------------------------------------------------

def _scrape_teaser(source: dict, session: requests.Session) -> Generator[Mention, None, None]:
    url = source["list_url"]
    sel = source["selectors"]
    scraped_at = _now()

    soup = _fetch_soup(session, url, source)
    if soup is None:
        return

    base_url = source.get("base_url", _origin(url))

    for row in soup.select(sel["row"]):
        title_el = row.select_one(sel["title"])
        link_el = row.select_one(sel["link"])
        date_el = row.select_one(sel["date"])

        if not (title_el and link_el):
            continue

        title = title_el.get_text(strip=True)
        href = link_el.get("href", "")
        abs_url = _clean_url(urljoin(base_url, href)) if href else ""
        date_raw = date_el.get_text(strip=True) if date_el else ""

        if not title or not abs_url:
            continue

        yield Mention(
            source_id=source["id"],
            doc_type=source["doc_type"],
            title=title,
            url=abs_url,
            date_raw=date_raw,
            scraped_at=scraped_at,
        )


# ---------------------------------------------------------------------------
# Archive mode  (full listing, optional query-string pagination)
# ---------------------------------------------------------------------------

def _scrape_archive(source: dict, session: requests.Session) -> Generator[Mention, None, None]:
    sel = source["selectors"]
    pagination = source.get("pagination", {})
    base_url = source.get("base_url", _origin(source["list_url"]))
    scraped_at = _now()

    for page_url in _paginate(source["list_url"], pagination):
        soup = _fetch_soup(session, page_url, source)
        if soup is None:
            break

        rows = soup.select(sel["row"])
        if not rows:
            logger.debug("No rows found on %s — stopping pagination", page_url)
            break

        for row in rows:
            title_el = row.select_one(sel["title"])
            link_el = row.select_one(sel["link"])
            date_el = row.select_one(sel["date"])

            if not (title_el and link_el):
                continue

            title = title_el.get_text(strip=True)
            href = link_el.get("href", "")
            abs_url = _clean_url(urljoin(base_url, href)) if href else ""
            date_raw = date_el.get_text(strip=True) if date_el else ""
            # Strip "Issue Date:" / "Ref:" label prefixes, e.g. "Issue Date: 17-06-2026"
            date_raw = date_raw.split(":", 1)[-1].strip()

            if not title or not abs_url:
                continue

            yield Mention(
                source_id=source["id"],
                doc_type=source["doc_type"],
                title=title,
                url=abs_url,
                date_raw=date_raw,
                scraped_at=scraped_at,
            )


def _paginate(base_url: str, pagination: dict) -> Iterator[str]:
    """Yield page URLs based on the pagination config.

    Supported types:
    - ``none`` / missing  → single page only
    - ``query_param``     → bare URL first (page 1), then ``?<param>=2``,
                            ``?<param>=3``, … until the archive scraper sees
                            an empty page and stops iteration.
    """
    ptype = pagination.get("type", "none")

    if ptype in ("none", "TODO", None):
        yield base_url
        return

    if ptype == "query_param":
        param = pagination.get("param", "page")
        yield base_url  # page 1 — no param needed (Liferay default)
        page = 2
        while True:
            sep = "&" if "?" in base_url else "?"
            yield f"{base_url}{sep}{param}={page}"
            page += 1
            if page > 200:  # hard safety cap
                logger.warning("Pagination safety cap reached for %s", base_url)
                break
        return

    # next_link / load_more are JavaScript-driven and not supported here.
    logger.warning("Pagination type %r is not yet supported — fetching first page only", ptype)
    yield base_url


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _build_session(cfg: dict) -> requests.Session:
    req_cfg = cfg.get("defaults", {}).get("request", {})
    session = requests.Session()
    session.headers["User-Agent"] = req_cfg.get(
        "user_agent", "Mozilla/5.0 (compatible; pfrda-watcher/1.0)"
    )
    return session


def _fetch_soup(
    session: requests.Session, url: str, source: dict
) -> BeautifulSoup | None:
    req_cfg = source.get("request", {})
    timeout = req_cfg.get("timeout_seconds", 30)
    retries = req_cfg.get("retry", 3)
    backoff = req_cfg.get("backoff_seconds", 4)

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning(
                "Attempt %d/%d failed for %s: %s", attempt, retries, url, exc
            )
            if attempt < retries:
                time.sleep(backoff * attempt)

    logger.error("All %d attempts failed for %s: %s", retries, url, last_exc)
    return None


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


_LIFERAY_NOISE = {"p_l_back_url", "p_l_back_url_title"}


def _clean_url(url: str) -> str:
    """Strip Liferay back-navigation params that vary by referring page.

    Without this, the same document scraped from two different listing pages
    produces two different URLs (and therefore two different stable IDs).
    """
    parsed = urlparse(url)
    params = {
        k: v for k, v in parse_qs(parsed.query).items()
        if k not in _LIFERAY_NOISE
    }
    return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))


def _has_todo_selectors(source: dict) -> bool:
    sel = source.get("selectors", {})
    return any(str(v).upper() == "TODO" for v in sel.values())
