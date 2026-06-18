"""Swappable persistence layer.

Repository is the interface; SqliteRepository is the default. A future
Postgres+pgvector backend implements the same protocol with zero caller changes.
Event-sourced: every observation is appended to `events`; `documents` is the
materialized current-state view.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Protocol

from .models import Document, Mention


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository(Protocol):
    def upsert(self, m: Mention) -> tuple[Document, bool]: ...   # (doc, is_new)
    def all_documents(self) -> list[Document]: ...
    def export_seen(self) -> dict: ...
    def import_state(self, state: dict) -> None: ...


class SqliteRepository:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                observed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                doc_type TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                date TEXT,
                date_raw TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                source_ids TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def upsert(self, m: Mention) -> tuple[Document, bool]:
        from .models import parse_date
        now = _now()
        row = self._conn.execute(
            "SELECT * FROM documents WHERE id = ?", (m.stable_id,)
        ).fetchone()

        if row is None:
            doc = Document(
                id=m.stable_id, doc_type=m.doc_type, title=m.title, url=m.url,
                date=parse_date(m.date_raw, "%d-%m-%Y"), date_raw=m.date_raw,
                first_seen=now, last_seen=now, source_ids=[m.source_id],
            )
            self._conn.execute(
                "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?)",
                (doc.id, doc.doc_type, doc.title, doc.url, doc.date,
                 doc.date_raw, doc.first_seen, doc.last_seen,
                 json.dumps(doc.source_ids)),
            )
            self._append_event(doc.id, "discovered", m, now)
            self._conn.commit()
            return doc, True

        # existing — refresh last_seen and source set
        source_ids = set(json.loads(row["source_ids"]))
        source_ids.add(m.source_id)
        self._conn.execute(
            "UPDATE documents SET last_seen=?, source_ids=? WHERE id=?",
            (now, json.dumps(sorted(source_ids)), m.stable_id),
        )
        self._append_event(m.stable_id, "reseen", m, now)
        self._conn.commit()
        doc = Document(
            id=row["id"], doc_type=row["doc_type"], title=row["title"],
            url=row["url"], date=row["date"], date_raw=row["date_raw"],
            first_seen=row["first_seen"], last_seen=now,
            source_ids=sorted(source_ids),
        )
        return doc, False

    def _append_event(self, doc_id: str, etype: str, m: Mention, ts: str) -> None:
        self._conn.execute(
            "INSERT INTO events (doc_id,event_type,payload,observed_at) VALUES (?,?,?,?)",
            (doc_id, etype, json.dumps(m.__dict__), ts),
        )

    def all_documents(self) -> list[Document]:
        rows = self._conn.execute(
            "SELECT * FROM documents ORDER BY date DESC, first_seen DESC"
        ).fetchall()
        return [
            Document(
                id=r["id"], doc_type=r["doc_type"], title=r["title"], url=r["url"],
                date=r["date"], date_raw=r["date_raw"], first_seen=r["first_seen"],
                last_seen=r["last_seen"], source_ids=json.loads(r["source_ids"]),
            )
            for r in rows
        ]

    # --- ephemeral-runner round-trip (GitHub Actions) ---
    def export_seen(self) -> dict:
        return {"documents": [d.to_dict() for d in self.all_documents()]}

    def import_state(self, state: dict) -> None:
        for d in state.get("documents", []):
            exists = self._conn.execute(
                "SELECT 1 FROM documents WHERE id=?", (d["id"],)
            ).fetchone()
            if not exists:
                self._conn.execute(
                    "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?)",
                    (d["id"], d["doc_type"], d["title"], d["url"], d["date"],
                     d["date_raw"], d["first_seen"], d["last_seen"],
                     json.dumps(d["source_ids"])),
                )
        self._conn.commit()
