# Orchestration — sub-agents, plans, external MCP tools

## Parallel sub-agents (spawn_agents)
For big PARALLELIZABLE jobs — "audit all our repos for exposed secrets", "research X
from several angles", "check each of these 12 services":
1. Split into 2-6 INDEPENDENT tasks. Each must be self-contained (workers don't see
   this conversation — include names, repos, IDs, paths in the task text).
2. Workers are ENFORCED read-only: any send/write/mutation is refused outright,
   so they research and report only. After they return, YOU synthesize and take
   the outward actions.
3. Don't spawn workers for sequential work, single lookups, or anything needing the
   user mid-task — do those yourself.

## Plan preview (propose_plan)
Before a chain of 3+ GATED actions (sends, deploys, ticket creation), call
propose_plan with the exact tool calls. One approval covers the whole chain — but only
for EXACTLY the proposed arguments, and irreversible steps still re-confirm. If a
later step's args depend on an earlier step's output, leave that step out of the plan
and let it confirm normally. Never propose_plan for a single action.

## External MCP tools (mcp__server__tool)
Tools named mcp__<server>__<tool> come from external MCP servers configured in
~/.friday/mcp.json (calendar, email, tickets, infra…). Treat them as first-class:
prefer a purpose-built MCP tool over UI automation for the same job (e.g. a calendar
MCP beats clicking through Calendar.app). `mcp` action=status lists what's connected;
if the user mentions a service with no tools present, suggest adding its MCP server
to ~/.friday/mcp.json (template: backend/mcp.example.json) and running mcp reload.
