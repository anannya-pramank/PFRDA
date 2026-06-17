# pfrda-watcher

Tracks PFRDA Notifications, Circulars, and Regulations as a single combined feed
(each item tagged with `doc_type` for downstream filtering). Same architectural
template as the takedown tracker and CERC scraper: declarative `sources.yaml`,
event-sourced append-only store, swappable repository, file-based CSV/JSON output
committed to git. Power Automate / Teams wiring is deliberately deferred — the
`output/new_this_run.json` file is the handoff point for it.

## Layout
```
pfrda_watcher/
  sources.yaml     declarative per-source rulebook (selectors live here)
  models.py        Mention -> Document, slug-based stable IDs, date parsing
  scraper.py       fetch + BeautifulSoup extraction driven by sources.yaml
  repository.py    Repository protocol; SqliteRepository (Postgres+pgvector later)
  cli.py           run: import_state -> scrape -> upsert -> export_state + outputs
.github/workflows/watch.yml   daily run, commits state + outputs
```

## Run locally
```
pip install -r requirements.txt
python -m pfrda_watcher.cli run
```

## Outputs (committed to git)
- `output/pfrda_documents.csv` / `.json` — full current state
- `output/new_this_run.json` — items first seen this run (the channel feed)
- `state/seen.json` — snapshot rehydrated on the next ephemeral runner

## Status
- **Teaser source (homepage What's New widget): confirmed and tested.** Selectors
  verified against live HTML; idempotent dedup confirmed.
- **Archive sources (`/active` full listing pages): scaffolded, disabled.**
  Selectors are `TODO` placeholders in `sources.yaml`. To finish: supply the
  outerHTML of one `/active` listing page (rows + pagination control), fill in
  the `row`/`link`/`title`/`date` selectors and the `pagination` block, then flip
  `enabled: true`. No other code changes needed — `scrape_source` already handles
  any source whose selectors are filled in (pagination loop is the one remaining
  addition, gated on knowing the pagination type).

## Open items
1. Confirm sibling archive URLs for notification/regulation (circular is known:
   `/web/pfrda/regulatory-framework/circular/active`).
2. Detail pages are `/w/<slug>` — if you want the actual PDF link captured, add a
   second-stage fetch of the detail page (one selector for the PDF `<a>`).
