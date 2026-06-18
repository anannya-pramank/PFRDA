"""CLI: scrape -> dedup/upsert -> write CSV+JSON for git commit.

Usage:
    python -m pfrda_watcher.cli run \
        --config pfrda_watcher/sources.yaml \
        --db state/pfrda.db \
        --state state/seen.json \
        --out output/

State round-trip pattern (ephemeral GitHub runners):
  1. import_state(seen.json)  -> rehydrate from committed snapshot
  2. scrape + upsert          -> new items flagged
  3. export_seen -> seen.json -> commit snapshot back
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from .repository import SqliteRepository
from .scraper import load_config, scrape_all


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    repo = SqliteRepository(args.db)

    state_path = Path(args.state)
    if state_path.exists():
        repo.import_state(json.loads(state_path.read_text()))

    new_items = []
    for mention in scrape_all(cfg):
        doc, is_new = repo.upsert(mention)
        if is_new:
            new_items.append(doc)

    # snapshot for next run
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(repo.export_seen(), indent=2))

    # outputs
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    docs = repo.all_documents()

    with (out / "pfrda_documents.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "doc_type", "title", "date", "url", "first_seen", "last_seen"])
        for d in docs:
            w.writerow([d.id, d.doc_type, d.title, d.date, d.url, d.first_seen, d.last_seen])

    (out / "pfrda_documents.json").write_text(
        json.dumps([d.to_dict() for d in docs], indent=2)
    )
    # New-this-run feed = what Power Automate will post to the channel later.
    (out / "new_this_run.json").write_text(
        json.dumps([d.to_dict() for d in new_items], indent=2)
    )

    print(f"total={len(docs)} new={len(new_items)}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="pfrda-watcher")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--config", default="pfrda_watcher/sources.yaml")
    r.add_argument("--db", default="state/pfrda.db")
    r.add_argument("--state", default="state/seen.json")
    r.add_argument("--out", default="output")
    r.set_defaults(func=cmd_run)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
