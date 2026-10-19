# Research Evidence Agent

An auditable literature-research web agent built around a server-side LLM Supervisor, MCP-facing Worker contracts, and Elicit/Edison capability adapters.

## Features

- Server-side OpenAI, Anthropic, approved OpenAI-compatible gateway, or local OpenAI-compatible model integration.
- Model timeouts, exponential retry, provider fallback, JSON Schema validation, token usage, and configurable cost estimates.
- Server-side MCP Runner for live Elicit and Edison Worker calls.
- Research-question protocol generation and human confirmation gates.
- Search and screening workspace with PRISMA progress.
- Editable evidence extraction with confidence and source spans.
- Bounded novelty analysis and method comparison outputs.
- CSV, XLSX, DOCX, PDF-print, Markdown, and CSV-bundle exports.
- SQLite checkpoints for resumable tasks.
- Per-task event replay, cancellation flags, and persisted medium/high-risk tool approvals.
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

Optional resilience and accounting settings:

```bash
export LLM_MAX_RETRIES=2
export LLM_TIMEOUT_SECONDS=90
export LLM_INPUT_USD_PER_1M=0
export LLM_OUTPUT_USD_PER_1M=0
# Local OpenAI-compatible server (Ollama/vLLM/etc.)
export LOCAL_LLM_BASE_URL="http://127.0.0.1:11434/v1"
export LOCAL_LLM_MODEL="your-local-model"
```

The first configured provider is primary; retryable failures fall through to the next configured provider. Keys remain server-side. Local HTTP endpoints are restricted to loopback hosts.

The local server also accepts the same approved OpenAI-compatible gateway contract used by the recruiting Agent:

```bash
export LLM_API_KEY="..."
export LLM_EGRESS_ENABLED=1
export LLM_BASE_URL="https://your-approved-gateway.example/v1"
export LLM_ALLOWED_HOST="your-approved-gateway.example"
export LLM_MODEL="gpt-5-mini"
```

When opening `ui/index.html` directly as a `file://` preview, the UI sends API requests to `http://127.0.0.1:4174`. Start the server from this repository first; a deployed Worker page uses its own same-origin `/api` routes.

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

## Runtime Control APIs

- `GET /api/tasks/{task_id}/events`: replay persisted task/checkpoint/approval events.
- `POST /api/tasks/{task_id}/cancel`: request cooperative cancellation.
- `POST /api/tools/approval`: create a persisted medium/high-risk action approval.
- `POST /api/tasks/{task_id}/approvals/{approval_id}/decide`: approve or reject it.

This MVP executes only the registered Elicit/Edison research tools. General shell, file mutation, HTTP, and browser tools are deliberately not exposed to model-selected execution; add each behind an allowlist and the approval API only when a concrete workflow needs it. SSE streaming and cross-session vector memory are also deferred because the current UI and bounded task checkpoints do not require them.

## MCP Configuration

See `mcp-config.example.json` and `references/mcp-routing.md` for the Elicit and Edison MCP routes. Credentials are intentionally not stored in this repository.

The protocol and chat routes execute a real server-side LLM. `/api/agent/run` executes the configured MCP Worker and persists raw outputs, session IDs, paper candidates, and DOI/URL citations into the task checkpoint. The UI starts SearchWorker after protocol confirmation and uses returned MCP candidates when available. If MCP credentials are absent, the task is paused with a configuration error; demonstration records are explicitly labelled and must not be treated as Elicit/Edison results.
