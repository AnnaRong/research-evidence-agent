# Review Workflow

## 1. Protocol gate

Do not search broadly until `protocol.json` states:

- review question and review type;
- question framework and populated elements;
- inclusion and exclusion criteria;
- publication types, languages, and date limits;
- planned databases and supplementary search methods;
- primary extraction fields;
- human reviewer and confirmation status.

For environmental and geospatial reviews, include geography, observation period, spatial and temporal resolution, dataset/version, variable definition, units, preprocessing, missing-data treatment, model, validation, and uncertainty.

## 2. Discovery and acquisition through MCP

Use the connected Elicit MCP server as the primary discovery and structured-review backend. Use Edison as an independent evidence and prior-art pass:

- `search_papers`: broad semantic discovery;
- `create_report` / `get_report`: focused research reports;
- `create_systematic_review` / `get_systematic_review`: staged search, screening, extraction, and reporting;
- `edison_quick_search_agent`: concise, cited answers;
- `edison_deep_search_agent`: long-form literature synthesis;
- `edison_precedent_search_agent`: prior-art and innovation checks;
- `edison_continue_task`: continuation of a long-running Edison task.

Record each call in `mcp/calls.jsonl`, including server, tool, input summary, task/session id, status, warnings, and output artifact path. Save raw tool results under `mcp/raw/` when the client exposes them.

Use Elicit and Edison as independent passes rather than asking one tool to validate its own output. If they disagree, preserve both results and create an explicit reconciliation item.

## 3. Deduplication

Deduplicate in this order:

1. normalized DOI;
2. conservative title similarity plus year and first-author guard;
3. preprint versus published-version reconciliation.

Spot-check every fuzzy merge in a high-stakes review.

## 4. Screening

Run title/abstract screening before full-text screening. Decisions are `include`, `exclude`, or `uncertain`. Exclusions require a protocol-linked reason; uncertainty advances to human review.

For systematic reviews, use Elicit's configured workflow plus an independent Edison check where practical, then adjudicate disagreements. For exploratory scoping reviews, a single AI pass is acceptable only when its status is disclosed and final decisions are human-confirmed.

## 5. Evidence extraction

Extract claims from the source, not from model memory. A source span must identify the paper, location, access level, and the supporting quotation or faithful paraphrase. Mark numeric fields and reconstructed chart values as unverified until checked by a human.

Do not silently harmonize incompatible variables. For example, annual precipitation, growing-season precipitation, and SPEI are separate constructs even when all relate to drought.

## 6. Method comparison

Compare studies across a stable matrix:

- research object and geography;
- observation period and sampling design;
- data sources and versions;
- variables, definitions, units, and scales;
- preprocessing and missing-data handling;
- model or analytical method;
- validation and uncertainty;
- findings, limitations, and applicability.

## 7. Novelty analysis

Test each proposed contribution as a conjunction of dimensions, for example:

`phenomenon + geography + time range + variable set + spatial scale + method + outcome`

Classify the result as:

- `precedent_found`: a study substantially covers the proposed conjunction;
- `partial_precedent`: important dimensions overlap, but the full conjunction does not;
- `method_analogue`: a similar method exists in another domain or setting;
- `no_precedent_found_within_search`: no close precedent was found in the documented search scope;
- `unresolved`: access, search, or evidence gaps prevent a responsible judgment.

The fourth label is not proof of global novelty. Report the exact databases, dates, queries, languages, and document types searched.

## 8. MCP failure and quota handling

If a server returns an authentication error, quota pause, incomplete session, or non-fatal warning, record it and stop short of claiming completion. Do not silently switch to a local search implementation. Ask the user to authenticate, resume, increase quota, or approve a different source set.

## 9. Completion gate

A review is complete only when:

- protocol scope is confirmed;
- search logs and deduplication counts exist;
- included/excluded counts reconcile;
- included papers have evidence rows;
- load-bearing claims have source spans;
- novelty comparisons name the closest precedents and contrary evidence;
- validation passes;
- remaining access and human-confirmation gaps are disclosed.
