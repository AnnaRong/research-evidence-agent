# Research Evidence Agent

An auditable literature-research web agent built around a server-side LLM Supervisor, MCP-facing Worker contracts, and Elicit/Edison capability adapters.

## Features

- Server-side OpenAI or Anthropic model integration for protocol generation and Agent chat.
- Server-side MCP Runner for live Elicit and Edison Worker calls.
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
export ELICIT_MCP_TOKEN="..."
export EDISON_API_KEY="..."
```

Without a configured model, `/api/protocol` and `/api/agent/chat` return HTTP 503. The application does not silently fall back to a rule template.

## Cloudflare Free MVP

The free deployment uses a Cloudflare Worker, Workers AI, D1, and the existing static UI. It does not require Workers Paid, Docker, or a Python runtime. Search uses the public OpenAlex API and every candidate keeps its DOI/URL. Elicit and Edison MCP are deliberately disabled in this free profile; they require external credentials and a HTTPS-compatible adapter.

Create the free D1 database, apply the schema, replace `database_id` in `wrangler.jsonc`, and deploy:

```bash
npx wrangler d1 create research-evidence-agent
npx wrangler d1 migrations apply research-evidence-agent --remote
npm run deploy
```

`src/index.js` defines the Supervisor, D1 checkpoint store, Workers AI protocol/Worker reasoning, OpenAlex search adapter, feedback logging, and human gates. `ui/` is served as Cloudflare static assets.

`render.yaml` remains available as an alternative runtime. Its free filesystem is ephemeral; do not call that option durable storage without a persistent disk or external database.

## Local Verification Only

```bash
python3 ui/server.py 4174
```

Opening `http://127.0.0.1:4174` is only for development verification. The intended user-facing deployment is the Cloudflare Worker and D1 described above.

## Tests

```bash
python3 -m py_compile ui/server.py scripts/model_agent.py
node --check ui/app.js
python3 -m unittest discover -s tests -v
```

## MCP Configuration

See `mcp-config.example.json` and `references/mcp-routing.md` for the Elicit and Edison MCP routes. Credentials are intentionally not stored in this repository.

The protocol and chat routes execute a real server-side LLM. `/api/agent/run` executes the configured MCP Worker and persists raw outputs, session IDs, paper candidates, and DOI/URL citations into the task checkpoint. The UI starts SearchWorker after protocol confirmation and uses returned MCP candidates when available. If MCP credentials are absent, the task is paused with a configuration error; demonstration records are explicitly labelled and must not be treated as Elicit/Edison results.
