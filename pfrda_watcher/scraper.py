"""Domain types and ID derivation for the PFRDA watcher.

Mirrors the takedown-tracker template: a raw scrape yields `Mention`s; these are
normalized and deduped into canonical `Order`-equivalent records (here: `Document`).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


def _slug_from_url(url: str) -> str:
    """PFRDA /w/<slug> and /web/.../<slug> both carry a stable slug as last path part."""
    return url.rstrip("/").rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Mention:
    """A single raw row observed on a listing page during one scrape."""
    source_id: str
    doc_type: str            # notification | circular | regulation
    title: str
    url: str
    date_raw: str
    scraped_at: str

    @property
    def stable_id(self) -> str:
        """Identity = the URL slug (stable across re-titling / re-dating).

        Falls back to a title+type hash if the slug is empty.
        """
        slug = _slug_from_url(self.url)
        if slug:
            return f"{self.doc_type}:{slug}"
        digest = hashlib.sha256(f"{self.doc_type}|{self.title}".encode()).hexdigest()[:16]
        return f"{self.doc_type}:h:{digest}"


@dataclass
class Document:
    """Canonical deduped record committed to the store."""
    id: str
    doc_type: str
    title: str
    url: str
    date: Optional[str]      # ISO YYYY-MM-DD if parseable, else None
    date_raw: str
    first_seen: str
    last_seen: str
    source_ids: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def parse_date(raw: str, fmt: str) -> Optional[str]:
    raw = (raw or "").strip()
    try:
        return datetime.strptime(raw, fmt).date().isoformat()
    except (ValueError, TypeError):
        return None
