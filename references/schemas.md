# Artifact Schemas

All JSONL files contain one JSON object per line. Extra fields are allowed; required fields are listed below.

## `screening/decisions.jsonl`

```json
{
  "paper_id": "openalex:W123",
  "stage": "title_abstract",
  "decision": "include",
  "reason": "Matches population, exposure, outcome, and study type",
  "reviewer": "agent-pass-a",
  "human_confirmed": false,
  "timestamp": "2026-10-14T08:00:00Z"
}
```

Allowed stages: `title_abstract`, `full_text`.

Allowed decisions: `include`, `exclude`, `uncertain`.

## `evidence/evidence.jsonl`

```json
{
  "paper_id": "openalex:W123",
  "research_object": "ecosystem transition zones",
  "geography": "global",
  "observation_period": "2001-2020",
  "spatial_scale": "0.05 degree",
  "datasets": [{"name": "ERA5-Land", "version": "unknown"}],
  "variables": [{"name": "air temperature", "definition": "2 m temperature", "unit": "K"}],
  "methods": ["zonal statistics", "trend analysis"],
  "validation": "Sensitivity analysis across thresholds",
  "findings": ["Compound exposure increased in the reported period"],
  "limitations": ["Dataset version was not stated"],
  "source_spans": [
    {
      "paper_id": "openalex:W123",
      "location": "Methods, p. 4",
      "access_level": "full_text",
      "quote_or_paraphrase": "The study used ERA5-Land 2 m temperature..."
    }
  ],
  "extractor": "agent-pass-a",
  "human_verified": false
}
```

Do not use `unknown`, empty text, and numeric zero interchangeably. Use `null` for a field that was checked but unavailable, and omit only fields outside the protocol.

## `novelty/claims.jsonl`

```json
{
  "claim_id": "N001",
  "proposed_contribution": "Combine heat, drought, NPP, population, and GDP at global ecosystem boundaries",
  "comparison_dimensions": ["phenomenon", "geography", "variables", "scale", "method"],
  "verdict": "partial_precedent",
  "supporting_paper_ids": ["openalex:W123"],
  "contradicting_paper_ids": ["openalex:W456"],
  "search_queries": ["ecosystem boundary compound heat drought NPP population GDP"],
  "scope_limit": "OpenAlex and Web of Science; English and Chinese; through 2026-10-14",
  "rationale": "Prior work combines climate and vegetation indicators, but the socioeconomic exposure pair is absent from the closest studies.",
  "human_confirmed": false
}
```

Allowed verdicts: `precedent_found`, `partial_precedent`, `method_analogue`, `no_precedent_found_within_search`, `unresolved`.

`no_precedent_found_within_search` requires documented queries and a non-empty scope limitation. It must remain explicitly bounded even after human confirmation.

## Worker result envelope

Every Worker artifact submitted to the Supervisor is a JSON object with this envelope:

```json
{
  "task_id": "search-2026-10-14-001",
  "worker": "SearchWorker",
  "status": "completed",
  "source_citations": [
    {"doi": "10.1000/example", "url": "https://doi.org/10.1000/example", "kind": "paper"}
  ],
  "mcp_calls": [
    {"server": "elicit", "tool": "search_papers", "session_id": "s-1"}
  ],
  "result": {}
}
```

`task_id` and `source_citations` are required. `source_citations` must be non-empty for every Worker, and each reference must contain a DOI, URL, or both. `mcp_calls` must identify the MCP server and tool used. The Supervisor stores the artifact path and does not rewrite the Worker result.

## Shared state entry

After validation, the Supervisor writes one entry per stage under `workflow/state.json.shared_state`. The entry contains exactly these fields:

```json
{
  "task_id": "screening-2026-10-14-001",
  "stage": "screening",
  "input": {
    "search": {
      "task_id": "search-2026-10-14-001",
      "artifact": "/absolute/path/search.json",
      "artifact_sha256": "..."
    }
  },
  "output": {"decisions": [], "prisma": {}},
  "source_citations": [{"doi": "10.1000/example", "url": "https://doi.org/10.1000/example"}],
  "timestamp": "2026-10-14T12:00:00Z"
}
```

Artifact paths and hashes live in the stage metadata and downstream `input` references, not as additional shared-entry fields. A retry replaces the current stage entry, while every attempt remains append-only in `workflow/runs.jsonl` and the audit log.

Stage-specific `result` requirements:

- `SearchWorker`: `candidates` with `paper_id`, `title`, `abstract`, and DOI or URL where available;
- `ScreeningWorker`: `decisions` and `prisma`;
- `ExtractionWorker`: `extractions` with source spans;
- `NoveltyWorker`: `claims` with a bounded verdict and closest-paper references;
- `SynthesisWorker`: `evidence_table`, `method_matrix`, and `narrative`.
