---
name: research-evidence-agent
description: Build auditable scientific literature reviews with reproducible search logs, conservative screening, structured evidence extraction, method comparison, citation checks, and bounded novelty analysis. Use for literature reviews, systematic or scoping review preparation, evidence tables, research-gap analysis, or checking whether a proposed contribution has prior art. Do not use it to claim exhaustive novelty without a documented multi-source search and human confirmation.
---

# Research Evidence Agent

Turn a research question into an auditable evidence project with a three-layer architecture: deterministic Supervisor, MCP-facing Worker Agents, and the connected Elicit/Edison MCP tools. The Supervisor is the control plane; it does not search, extract, or synthesize scientific content.

## Start

Create or resume a local audit project with the deterministic helper:

```bash
python3 scripts/evidence_agent.py init --project <project-dir> --question "<research question>"
python3 scripts/supervisor.py init --project <project-dir> --question "<research question>"
```

Read [references/architecture.md](references/architecture.md) before operating the pipeline. Read [references/workers.md](references/workers.md) for the active Worker. Read [references/mcp-routing.md](references/mcp-routing.md) before calling an MCP tool. Read [references/schemas.md](references/schemas.md) before writing or submitting Worker artifacts.

## Non-negotiable contract

- Treat the protocol as the source of truth. Confirm the question, scope, eligibility rules, date range, languages, and allowed publication types before screening.
- Log every MCP tool call, session/task id, source, date, result count, and warning. Never describe a convenience sample as exhaustive.
- Keep uncertain records as `uncertain`; do not silently exclude them.
- Every extracted finding, number, variable definition, or method detail needs a source span and access level (`abstract`, `full_text`, `table`, `figure`, or `supplement`).
- Preserve units, spatial and temporal scale, dataset version, missingness, and incompatible definitions. Do not normalize silently.
- Novelty is comparative and bounded. Use `no_precedent_found_within_search`, never an unqualified "first" or "no prior study" claim.
- Require human confirmation for final inclusion decisions, numeric evidence, risk-of-bias judgments, and novelty language intended for a paper or proposal.
- Keep AI-generated decisions and human-confirmed decisions distinguishable in the audit trail.

## Workflow

1. Initialize the project and complete `protocol.json`. The Supervisor stops at the `protocol` confirmation gate.
2. Confirm the pending gate, then ask the Supervisor for the next Worker contract:

   ```bash
   python3 scripts/supervisor.py confirm --project <project-dir> --gate protocol --by <reviewer>
   python3 scripts/supervisor.py next --project <project-dir>
   ```

3. Run only the returned Worker. The five Workers are `SearchWorker`, `ScreeningWorker`, `ExtractionWorker`, `NoveltyWorker`, and `SynthesisWorker`. Each calls the MCP tools listed in its contract and writes a Worker result envelope with DOI/URL sources.
4. Submit the immutable Worker artifact to the Supervisor:

   ```bash
   python3 scripts/supervisor.py complete --project <project-dir> --artifact <worker-result.json>
   ```

5. When the Supervisor returns a confirmation gate, show the relevant candidate set, decisions, PRISMA counts, extraction gaps, or novelty limitations to the user. Do not continue until the named gate is confirmed.
6. After `SynthesisWorker` completes, produce deterministic tables and check invariants:

   ```bash
   python3 scripts/evidence_agent.py report --project <project-dir>
   python3 scripts/evidence_agent.py validate --project <project-dir>
   ```

7. Present unresolved conflicts, unavailable full texts, single-source fields, tool warnings, quota pauses, and unconfirmed novelty claims as limitations, not completed findings.

## Tool choice

- Use the connected Elicit and Edison MCP tools for discovery, reports, systematic reviews, synthesis, and precedent search.
- Use `scripts/supervisor.py` only for orchestration and global state. Use `scripts/evidence_agent.py` only for local audit records, deduplication, validation, and report export.
- Never let the Supervisor replace a Worker or let a Worker advance its own stage.
- Use official DOI/bibliographic verification only when an MCP result needs identity or status checking. Prefer DOI-based identity over title matching.
- Do not install or recreate a second literature-search agent unless the user explicitly requests a fallback implementation.

## Outputs

A completed project contains:

- `protocol.json`
- `workflow/state.json` and `workflow/runs.jsonl`
- `search/queries.jsonl`
- `mcp/calls.jsonl` and `mcp/raw/`
- `corpus/records.jsonl` and `corpus/deduped.jsonl`
- `screening/decisions.jsonl`
- `evidence/evidence.jsonl`
- `novelty/claims.jsonl`
- `audit/events.jsonl`
- generated CSV and Markdown files under `reports/`

Run validation before calling a review complete. A clean structural validation does not prove scientific truth; it proves that required evidence, MCP provenance, and audit fields are present.
