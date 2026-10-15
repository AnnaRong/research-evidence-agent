# Worker contracts

Each Worker receives the JSON contract emitted by `supervisor.py next`, reads only the listed input artifacts, calls its assigned MCP tools, and writes one Worker result envelope with a unique `task_id`. It must not edit `workflow/state.json` or call `supervisor.py confirm`/`complete`.

## SearchWorker

Inputs: confirmed protocol and research question.

Calls Elicit `search_papers` and Edison `edison_quick_search_agent` in parallel. Preserve both raw result sets. Merge records by normalized DOI first, then conservative title/year/first-author matching. Do not invent an abstract when a source omits it.

Output `result.candidates`, with:

- `paper_id`;
- DOI and/or URL;
- title;
- abstract or `null`;
- contributing MCP sources;
- duplicate/merge explanation when multiple results were combined.

## ScreeningWorker

Inputs: SearchWorker artifact and confirmed inclusion/exclusion criteria.

Calls Elicit `create_systematic_review` and retrieves status/results through `get_systematic_review`. Preserve the Elicit review/session ID. Map each title/abstract and full-text decision to a candidate paper. Do not convert missing full text into an exclusion without an explicit protocol rule.

Output `result.decisions` and `result.prisma`. PRISMA numbers must reconcile with the candidate and decision lists.

## ExtractionWorker

Inputs: ScreeningWorker artifact, Elicit systematic-review session, and included-paper set.

Reads Elicit's structured extraction output through `get_systematic_review`. Calls Edison `edison_deep_search_agent` to supplement method details, not to overwrite conflicting Elicit fields. Preserve conflicts for adjudication.

Output `result.extractions`, with the exact critical keys `study_design`, `sample_size`, `effect_measures`, and `statistical_methods`, plus variables, datasets, methods, validation, findings, limitations, and source spans. Every numeric field must state its unit and verification status. The Supervisor rejects completion when the critical-field missing rate is 20% or higher.

## NoveltyWorker

Inputs: ExtractionWorker artifact, proposed hypothesis/contribution, and confirmed search scope.

Calls Edison `edison_precedent_search_agent`. Compare the conjunction of phenomenon, geography, period, variables, scale, method, and outcome. Search for contrary and partial precedents, not only supporting novelty.

Output `result.claims`, using only the bounded verdict vocabulary in [schemas.md](schemas.md). Never output an unqualified “first” claim.

## SynthesisWorker

Inputs: SearchWorker, ScreeningWorker, ExtractionWorker, and NoveltyWorker artifacts, plus the Supervisor's global-state snapshot.

Calls Edison `edison_deep_search_agent` for the narrative synthesis. The local Worker assembles the final evidence table and method matrix from validated upstream artifacts; Edison prose cannot add papers or facts absent from the evidence ledger.

Output:

- `result.evidence_table`;
- `result.method_matrix`;
- `result.narrative`;
- DOI/URL citations for every load-bearing claim;
- unresolved conflicts, access gaps, and human-confirmation needs.

