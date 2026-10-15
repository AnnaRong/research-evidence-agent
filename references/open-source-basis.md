# Open-source Basis

This skill is an original, small orchestration layer around hosted MCP capabilities. It does not vendor their code or reimplement their search/synthesis engines.

- **Elicit MCP/API**: hosted paper search, reports, and systematic-review sessions. The official docs describe the MCP endpoint at `https://elicit.com/api/mcp`, OAuth authentication in MCP clients, and Pro-or-above API access for the REST API. Use the live client's `tools/list` inventory as the authority for exact MCP tool names and schemas.
- **Edison MCP** (`longevity-genie/edison_mcp`, PyPI package `edison-mcp`): hosted Edison/FutureHouse research agents exposed through MCP. Treat the connected server's live tool schema and account permissions as authoritative.

- **agentic-research** (`kurtvalcorza/agentic-research`, MIT): PRISMA-oriented stage separation, explicit human gates, and reconciled review counts.
- **Grounded** (`jostelzer/grounded`, MIT): claims-ledger discipline, DOI verification, contrary-evidence search, and source-bound audit receipts.

OpenAlex remains useful as an optional offline fallback or verification source, but it is deliberately not the primary search implementation in this Skill.

The local project keeps dependencies optional so that its audit artifacts remain usable without a particular model vendor, vector database, or agent framework.
