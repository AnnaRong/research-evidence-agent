# Research Evidence Agent

An auditable literature-research web agent built around a server-side LLM Supervisor, MCP-facing Worker contracts, and Elicit/Edison capability adapters.

## Features

- Server-side OpenAI or Anthropic model integration for protocol generation and Agent chat.
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

## Model Configuration

Configure one public, server-side model credential. Never put the key in frontend JavaScript or commit it to Git.

```bash
export OPENAI_API_KEY="..."
export OPENAI_MODEL="gpt-5-mini"
# Or: ANTHROPIC_API_KEY and ANTHROPIC_MODEL
```

Without a configured model, `/api/protocol` and `/api/agent/chat` return HTTP 503. The application does not silently fall back to a rule template.

## Public Deployment

`render.yaml` defines a public Render web service. Connect this repository in Render, enter `OPENAI_API_KEY` as a secret, and deploy the `main` branch. The process binds to Render's `PORT` on `0.0.0.0`.

The free Render filesystem is ephemeral. SQLite checkpoints survive browser refreshes but may be lost on service restart or redeploy. Use a persistent disk or external database before calling the deployment durable production storage.

## Run Locally

```bash
python3 ui/server.py 4174
```

Open `http://127.0.0.1:4174`.

## Tests

```bash
python3 -m py_compile ui/server.py scripts/model_agent.py
node --check ui/app.js
python3 -m unittest discover -s tests -v
```

## MCP Configuration

See `mcp-config.example.json` and `references/mcp-routing.md` for the Elicit and Edison MCP routes. Credentials are intentionally not stored in this repository.

The protocol and chat routes execute a real server-side LLM. The current search, extraction, novelty, and synthesis screens still contain explicitly labelled demonstration records until a cloud MCP runner is configured. Do not treat those records as Elicit/Edison results.
