# MCP Routing

## Connection targets

Configure these servers in the Codex MCP/connector settings before running the workflow:

```json
{
  "mcpServers": {
    "elicit": {
      "transport": "streamable_http",
      "url": "https://elicit.com/api/mcp",
      "auth": "oauth"
    },
    "edison": {
      "transport": "stdio",
      "command": "uvx",
      "args": ["edison-mcp@latest", "stdio"],
      "env": {
        "EDISON_API_KEY": "<configure-in-client-secret-store>"
      }
    }
  }
}
```

The exact Codex UI/configuration surface may differ by client version. Do not put OAuth tokens or API keys in this file. Authenticate Elicit through the MCP client's browser flow. Configure Edison credentials through its documented account/environment flow.

## Tool routing

| Intent | Primary tool | Secondary/check |
|---|---|---|
| Broad paper discovery | Elicit `search_papers` | Edison quick search |
| Structured report | Elicit `create_report` → `get_report` | Edison deep search |
| Full systematic-review flow | Elicit `create_systematic_review` → `get_systematic_review` | Human review of included/excluded decisions |
| Focused cited answer | Edison `edison_quick_search_agent` | Elicit report |
| Long literature synthesis | Edison `edison_deep_search_agent` | Elicit report/systematic review |
| Prior-art / innovation check | Edison `edison_precedent_search_agent` | Elicit search plus explicit closest-precedent table |
| Resume work | Edison `edison_continue_task` or Elicit `resume_session` | Check quota and status |

## Call protocol

1. Confirm the local protocol before calling either server.
2. Call the primary tool with a structured task that names the research object, geography, period, variables, methods, and desired output fields.
3. Save the returned session/task id and raw result.
4. Inspect warnings, access limits, missing full text, and citation completeness.
5. Call the secondary/check tool for load-bearing synthesis or novelty claims.
6. Import only evidence with paper identifiers and source attribution into the local ledger.
7. Reconcile disagreements explicitly and require human confirmation before final claims.

## Prompt templates

### Evidence review

```text
Research question: <question>
Scope: <population/system, geography, period>
Required extraction fields: study design, datasets and versions, variables and units,
spatial/temporal scale, preprocessing, model, validation, findings, limitations.
Return a paper-level evidence table. For every field, cite the paper and identify
whether support came from abstract, full text, table, figure, or supplement.
Separate missing information from zero, absence, and negative findings.
```

### Precedent check

```text
Proposed contribution: <contribution>
Compare the conjunction of phenomenon, geography, period, variable set, scale,
method, and outcome. Return closest precedents, partial precedents, method analogues,
contrary evidence, and unresolved access/search gaps. Do not claim "first". Use a
bounded verdict tied to the documented search scope.
```
