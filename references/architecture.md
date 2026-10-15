# Three-layer architecture

## Layer 1: Supervisor

The Supervisor owns orchestration and state. It does not search papers, read papers, or write synthesis prose.

Responsibilities:

- turn one research question into the fixed worker pipeline;
- expose the next runnable worker and its MCP route;
- persist global state and worker-run provenance;
- write every Worker output into the shared state object with `task_id`, `stage`, upstream `input` references, structured `output`, `source_citations`, and `timestamp`;
- stop at confirmation gates;
- reject out-of-order or citation-free worker outputs;
- assemble a final manifest without changing scientific content.

The deterministic implementation is `scripts/supervisor.py`. It writes `workflow/state.json` and `workflow/runs.jsonl` inside a review project.

## Layer 2: Worker Agents

Workers are MCP-facing contracts. A Worker Agent may be implemented by Codex or another MCP-capable runtime, but it must return the contract shown in [schemas.md](schemas.md). A worker never advances the workflow itself.

| Worker | MCP route | Required output |
|---|---|---|
| `SearchWorker` | Elicit `search_papers` + Edison `edison_quick_search_agent` | candidate papers with DOI/title/abstract, per-source provenance, merge notes |
| `ScreeningWorker` | Elicit `create_systematic_review` / `get_systematic_review` | abstract/full-text decisions, exclusion reasons, PRISMA counts |
| `ExtractionWorker` | Elicit structured extraction + Edison `edison_deep_search_agent` | study design, sample size, effect measures, statistics, methods, source spans |
| `NoveltyWorker` | Edison `edison_precedent_search_agent` | closest work, partial precedent, method analogue, bounded verdict |
| `SynthesisWorker` | Edison `edison_deep_search_agent` | evidence table, method matrix, narrative synthesis, citations |

The Supervisor's `next` command returns the exact route and output contract for the current Worker. The Supervisor never calls the MCP tools on the Worker's behalf.

## Layer 3: MCP tools

Elicit and Edison remain the capability layer. Keep raw responses or a content-addressed copy under `mcp/raw/`, and put the session/task ID in `workflow/runs.jsonl`. All paper-level claims must resolve to a DOI or URL.

## Gates

The pipeline cannot advance without confirmation at these points:

1. `protocol`: question, scope, eligibility, and extraction fields;
2. `post_search`: candidate set and deduplication are acceptable;
3. `post_screening`: inclusion/exclusion decisions and PRISMA counts are acceptable;
4. `pre_synthesis`: extraction and novelty outputs are ready to synthesize.

An unresolved MCP error, quota pause, missing full text, or citation-free result keeps the workflow blocked at the current stage. It must not be represented as a successful Worker completion.

After each Worker submission, the Supervisor runs these deterministic quality gates:

- search: candidate paper count must be greater than zero;
- screening: included-paper count must be positive, not exceed the candidate count, and for candidate sets of five or more the default inclusion ratio must be 2%–80%;
- extraction: missing cells across `study_design`, `sample_size`, `effect_measures`, and `statistical_methods` must be below 20%;
- novelty/synthesis: schema and DOI/URL citation validation.

One failed gate returns the same stage as `needs_retry`. A second consecutive failure changes the workflow to the `quality_intervention` confirmation gate. Confirmation acknowledges the problem and reopens the same stage for another attempt; it does not override the failed quality result.

