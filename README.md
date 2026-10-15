# Research Evidence Agent

An auditable literature-research workspace built around a deterministic Supervisor, MCP-facing Worker contracts, and Elicit/Edison capability adapters.

## Features

- Research-question protocol generation and human confirmation gates.
- Search and screening workspace with PRISMA progress.
- Editable evidence extraction with confidence and source spans.
- Bounded novelty analysis and method comparison outputs.
- CSV, XLSX, DOCX, PDF-print, Markdown, and CSV-bundle exports.
- SQLite checkpoints for resumable tasks.
- Worker failure handling, retries, tool switching, stage skipping, and cost limits.
- Feedback loops for screening, extraction, and novelty decisions.
- A/B assignment between `elicit_only` and `elicit_edison` pipelines.
- Post-task interview with useful-stage, improvement-stage, satisfaction, and NPS questions.

## Run Locally

```bash
python3 ui/server.py 4174
```

Open `http://127.0.0.1:4174`.

## Tests

```bash
python3 -m py_compile ui/server.py
node --check ui/app.js
python3 -m unittest discover -s tests -v
```

## MCP Configuration

See `mcp-config.example.json` and `references/mcp-routing.md` for the Elicit and Edison MCP routes. Credentials are intentionally not stored in this repository.

The UI and state APIs are ready for an external MCP runner to write real session IDs, Worker checkpoints, source citations, and failure states.
