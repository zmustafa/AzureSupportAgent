---
layout: default
title: Progressive Tool Discovery and Skills Plan
parent: Improvement Plans
nav_order: 90
---

# Progressive Tool Discovery and Skills Plan

**Status:** Implemented locally

**Created:** 2026-08-11

**Completed:** 2026-08-11
**Scope:** Prevent LLM provider tool-count failures and progressively expose Azure, Entra, first-party, connector, and workload tools.

## Executive summary

The OpenAI request fails when raw Entra MCP tools are enabled because the application eagerly sends every enabled tool schema on every model round. In the observed production request, the provider received 133 function definitions while accepting at most 128.

Skills alone will not fix this error. Skills describe workflows, but they do not remove tool schemas from provider requests. The immediate shared solution is a provider-independent tool router with local deferred tool discovery. Native OpenAI `tool_search` can be added later as an optional optimization for supported Responses API models.

## Confirmed cause

The current catalogs measure as follows:

| Source | Tools |
|---|---:|
| Azure MCP | 68 |
| Entra MCP | 44 |
| Built-in utilities | 7 |
| IAM cached-access tools | 10 |
| Ownership tools | 3 |
| Default Entra identity tools | 2 |
| Workload Performance Profiler | 1 when a workload is selected |

The observed 133-tool request is explained by:

$$68 + (44 - 2) + 7 + 10 + 3 + 2 + 1 = 133$$

The two subtracted raw Entra tools are behavioral-history tools withheld when the caller lacks `investigate.activity`. With Entra disabled, the equivalent request exposes approximately 91 tools, so it remains below OpenAI's hard limit.

### Current behavior

- The orchestrator eagerly merges Azure MCP and enabled raw Entra MCP tools.
- First-party, connector, IAM, Ownership, Entra identity, and workload tools are appended to the same provider request.
- Every selected schema is sent again on each model round.
- No provider tool-count guard or dynamic catalog router exists.
- The global Entra control is all-or-nothing.
- Custom agents can allow all Azure or Entra tools but cannot select scoped MCP bundles.
- Existing result limits protect context from large tool responses, not from excessive tool schemas.

## Design goals

1. No provider request may exceed the provider's declared tool-definition limit.
2. Relevant tools must not be discarded through arbitrary array truncation.
3. The baseline solution must work across OpenAI, Azure OpenAI, Claude, GitHub Copilot, Gemini, Ollama, and other providers.
4. Tool discovery must preserve existing permissions, read/write classification, approval gates, and audit behavior.
5. The model should initially receive only the tools relevant to the request and be able to load more during the turn.
6. Skills should progressively expose workflow instructions without becoming an authorization mechanism.
7. Routing decisions and catalog pressure must be observable and testable.

## Non-goals

- Do not migrate the entire provider layer to the OpenAI Responses API merely to patch the immediate failure.
- Do not expose a local Entra stdio MCP server directly to a public provider.
- Do not solve the problem through blind `tools[:128]` truncation.
- Do not weaken behavioral-data permissions, write approvals, tenant isolation, or custom-agent allowlists.

# Implementation plan

## Phase 0 — Measurement and safety invariant

1. Add a reusable catalog diagnostic that records counts by source:
   - Azure MCP.
   - Raw Entra MCP.
   - Built-in utilities.
   - IAM tools.
   - Ownership tools.
   - First-party Entra identity tools.
   - Workload and profiler tools.
   - Connector tools.
2. Record duplicate names, permission-filtered counts, and serialized schema bytes.
3. Add a fixture that reproduces the observed 133-tool configuration.
4. Introduce provider capability metadata:
   - Hard maximum tool definitions.
   - Recommended operating budget.
   - Native deferred-search support.
   - Chat Completions versus Responses API support.
5. Assert before every provider request that the selected catalog is within the provider's hard limit.
6. Recognize OpenAI's `array_above_max_length` response and retry once through bounded routing as defense in depth.

### Acceptance criteria

- The 133-tool fixture reproduces the failure before the change.
- No provider adapter can submit more tools than its declared maximum afterward.
- An internal actionable error is emitted before an external provider 400 if the invariant is violated.

## Phase 1 — Unified internal tool catalog

Create one normalized catalog entry for every executable tool. Each entry should include:

- Name and concise description.
- Full parameter schema.
- Source and invocation route.
- Domain, subdomain, bundle, and search keywords.
- Read/write classification and risk level.
- Required product permission.
- Custom-agent eligibility.
- Always-available, explicitly selected, or searchable status.
- Stable priority and tie-breaking metadata.

Merge by name across all sources, not only Azure versus Entra. Reject or explicitly resolve ambiguous cross-source collisions instead of silently overwriting handlers.

Keep schema selection and executable handler selection synchronized: a model must never see a schema whose handler is unavailable, and a dynamically loaded schema must remain callable for the rest of the turn.

Correct tool-loading status counters so Azure and Entra counts are reported separately rather than presenting their merged total as the Azure count.

### Acceptance criteria

- Every exposed schema resolves to exactly one callable handler.
- Duplicate names are detected deterministically.
- Catalog ordering is stable across equivalent runs.
- Source, domain, permission, and risk metadata are available for every tool.

## Phase 2 — Provider-independent initial routing

Create a routing service, such as `app/agent/tool_router.py`, that accepts:

- Latest user request.
- Conversation and tool-result context.
- Selected workload or scope hint.
- Custom-agent instructions and allowlists.
- Effective user permissions.
- Read/write and approval policy.
- Provider capabilities and budget.
- The complete normalized catalog.

Use a configurable initial operating budget of approximately 15–25 tools, substantially below provider hard limits.

Selection order:

1. Apply tenant, permission, behavioral-data, read/write, and custom-agent filters.
2. Include a very small mandatory core.
3. Detect broad domains deterministically.
4. Activate matching curated bundles.
5. Rank remaining candidates using names, descriptions, metadata, conversation context, and explicit scope hints.
6. Deduplicate and use stable tie-breaking.
7. Preserve explicitly requested agent tools unless forbidden by a security filter.

Example routing expectations:

| Request | Initial surface |
|---|---|
| “Hi” | Deferred-search capability only; no raw Azure or Entra catalog |
| “Investigate John's MFA” | Identity dossier, user lookup, MFA, roles, and permitted sign-in tools |
| “Find expiring app credentials” | Applications, service principals, and credential-analysis tools |
| “Evaluate Conditional Access for this app” | Conditional Access, application lookup, and identity context |
| “Why is this VM unavailable?” | Azure compute, Resource Graph, Monitor, and network diagnostics; no raw Entra tools |

Keep the selected set stable within a turn. Re-ranking may add tools after new evidence, but it must not remove a tool that was already exposed or used during that turn.

### Acceptance criteria

- Ordinary requests expose no more than the configured initial budget.
- Relevant Entra tools survive selection for representative identity prompts.
- Irrelevant Entra tools are absent from Azure-only troubleshooting prompts.
- Selection is deterministic for equivalent inputs.

## Phase 3 — Entra and Azure domain bundles

Define explicit bundles instead of treating MCP servers as indivisible catalogs.

Recommended Entra bundles:

- Users and guests.
- Groups and membership.
- Applications, service principals, and credentials.
- Authentication methods and MFA.
- Sign-ins and audit activity.
- Conditional Access.
- Directory roles and privileged access.
- Permissions, consent, and governance.
- Managed devices.
- Writes, separated from reads.

Include first-party IAM, Ownership, and Entra identity tools in the same metadata and bundle system so the router can prefer higher-level tools over low-level Graph calls when appropriate.

Keep named individual behavioral-history tools unavailable to callers without `investigate.activity`, including through catalog search and bundle expansion.

### Acceptance criteria

- Bundles are independently selectable.
- Read and write operations can be enabled separately.
- Behavioral bundles cannot bypass `investigate.activity`.
- First-party higher-level tools can outrank equivalent low-level operations.

## Phase 4 — Local deferred tool search

Add one provider-neutral first-party capability, conceptually `search_tools` or `load_tool_bundle`, to every tool-enabled turn.

Flow:

1. The model initially receives the routed 15–25 tools plus the search capability.
2. Search runs against the complete catalog only after all security filters have been applied.
3. It returns compact matches containing name, source, short description, and selection reason.
4. The model requests named tools or bundles.
5. The orchestrator adds a bounded number, such as eight, on the next model round.
6. A configurable per-turn ceiling, such as 32 total exposed schemas, remains enforced.

Search must not execute target tools directly. Expansion must reapply permission, custom-agent, behavioral, write, and approval checks.

### Acceptance criteria

- An initially omitted relevant tool can be found and loaded in a later round.
- Blocked tools are absent from search results.
- Search cannot bypass agent allowlists or write restrictions.
- Dynamic loading remains below both operating and provider hard limits.

## Phase 5 — Correct custom-agent tool controls

1. Make `allow_all_azure` constrain the eligible catalog in the chat runtime.
2. Change `allow_all_azure` and `allow_all_entra` semantics from “eagerly expose every schema” to “allow the router to search and select from this catalog.”
3. Add optional scoped fields:
   - `azure_tools`.
   - `azure_bundles`.
   - `entra_tools`.
   - `entra_bundles`.
4. Preserve backward compatibility for existing saved and built-in agents.
5. Make explicit agent selections outrank automatic relevance ranking while still obeying permissions and provider limits.
6. Update the agent designer prompt, validation, persistence, runner, and preview surfaces together.

### Acceptance criteria

- `allow_all_azure=false` and `allow_all_entra=false` are enforced.
- Existing agents continue loading without migration failures.
- Administrators can preview the effective routed surface for an agent.
- Explicit selections cannot override security or approval policy.

## Phase 6 — Admin UX and diagnostics

Enhance Azure and Entra tool administration with:

- Catalog counts by source.
- Current provider hard limit.
- Initial and expanded routing budgets.
- Read versus write counts.
- Permission-withheld and behavioral-withheld counts.
- Searchable bundle and per-tool controls.
- Enabled-count indicators.
- Custom-agent effective-surface preview.
- Warning when a configuration would exceed a provider limit without routing.

Replace raw horizontally scrolling provider JSON errors with a concise actionable message and retain detailed diagnostics in logs.

Per-turn telemetry should include:

- Total discovered and eligible tools.
- Selected and dynamically loaded counts.
- Counts by source and domain.
- Serialized schema bytes.
- Routing reason tags.
- Tools removed by the final safety guard.
- Provider budget and hard limit.

Avoid logging prompt contents, tool arguments, access tokens, or sensitive tool results merely to explain routing.

### Acceptance criteria

- Administrators can understand why tools are available or withheld.
- The UI shows pressure against provider limits before a failure occurs.
- Production telemetry can reconstruct selection decisions without sensitive payloads.

## Phase 7 — Skills as progressive workflow instructions

Add an application-native skill catalog after bounded tool routing is operational.

Candidate initial skills:

- Entra user investigation.
- Conditional Access evaluation.
- Application credential hygiene.
- Access review and privilege analysis.
- VM availability triage.
- Network diagnostics.

Initially expose only skill identifiers, names, and short descriptions. Load the complete procedure only when selected, then inject it into the model's instruction context.

Skills should reference required domains or bundles rather than embedding dozens of tool schemas. Skill content is guidance, not authorization; all permission, tool, write, and approval controls continue to apply independently.

Existing custom-agent instructions can seed the first curated workflows. FastMCP skill-resource support may be reused for storage/discovery if appropriate, but it does not replace application orchestration.

### Acceptance criteria

- Full skill instructions are absent until selected.
- Selecting a skill activates workflow guidance and requests relevant bundles.
- Skills cannot authorize or reveal otherwise blocked tools.
- Skill content does not duplicate the complete MCP schema catalog.

## Phase 8 — Optional native OpenAI `tool_search`

Treat native OpenAI tool search as a provider capability rather than the shared architecture.

1. Add a Responses API path only for supported OpenAI models.
2. Keep the provider-independent router and local deferred search as the fallback for unsupported OpenAI models and every other provider.
3. For local stdio MCP servers, prefer client-executed local search rather than sending a public remote MCP endpoint to OpenAI.
4. If remote HTTPS MCP is introduced later, require authentication, tenant isolation, permission filtering, write approvals, auditing, and behavioral-data restrictions.
5. Compare native search quality, latency, cost, and observability against local routing before making it the default.

Simply adding `defer_loading` to existing ordinary Chat Completions function definitions will not provide native tool search.

### Acceptance criteria

- Supported OpenAI models can use the Responses API path without changing behavior for other providers.
- The local router remains a complete fallback.
- Native search cannot bypass local authorization and execution controls.

## Phase 9 — Compact and paginated tool outputs

Tool routing solves schema count; output controls solve context growth. Implement both independently.

1. Prefer compact structured results with default field projections.
2. Add `top`, pagination, and continuation tokens where supported.
3. Return artifact IDs or references for large result sets instead of embedding everything in the conversation.
4. Preserve separate limits for discovery results and normal tool results.
5. Record truncation explicitly so the model does not mistake a partial result for a complete inventory.

### Acceptance criteria

- Large Entra and Azure inventories do not flood the model context.
- Pagination is explicit and resumable.
- Truncation never masquerades as a complete result.

# Validation plan

## Unit tests

- Reproduce and route the 133-tool catalog into a bounded selection.
- Ensure relevant Entra tools are selected for users, MFA, Conditional Access, credentials, roles, and devices.
- Ensure irrelevant tools are excluded for greetings and Azure-only prompts.
- Verify stable ordering and tie-breaking.
- Detect duplicate names across all sources.
- Verify selected schemas and callable handlers remain synchronized.
- Verify per-source, per-domain, permission, write, and custom-agent filtering.
- Verify bundle expansion and local tool search.
- Verify schema-byte and count telemetry.

## Provider adapter tests

- OpenAI never receives more than 128 definitions.
- The configured operating budget is enforced below the hard maximum.
- Oversized internal input is routed rather than blindly truncated.
- An OpenAI limit error receives one bounded retry and cannot loop.
- Providers without native search continue through local routing.

## Security tests

- Behavioral Entra tools remain undiscoverable without `investigate.activity`.
- Write tools remain unavailable unless allowed by role, custom-agent policy, runtime intent, and approval policy.
- Catalog search cannot bypass custom-agent allowlists.
- Bundle expansion cannot bypass permission filters.
- Tool-name collisions do not redirect execution to another source.
- Tenant-scoped handlers remain bound to the correct tenant.

## End-to-end scenarios

With raw Entra MCP enabled and OpenAI `gpt-5.5` selected:

1. Send “hi” with no workload selected.
2. Send “hi” with a workload selected.
3. Ask for a user's MFA status.
4. Investigate a permitted user's sign-in activity.
5. Evaluate Conditional Access for an application.
6. Find expiring application credentials.
7. Diagnose a VM availability problem.
8. Run an IAM access-review request.
9. Invoke a connector-backed workflow.
10. Repeat through a custom agent, scheduled agent, and deep investigation.

For each scenario verify:

- No tool-array 400 occurs.
- The initial and expanded surfaces remain within budget.
- Expected relevant tools remain callable.
- Unauthorized or irrelevant tools remain absent.
- The answer is evidence-backed.
- Routing telemetry explains the selected surface.

## Performance checks

- Measure routing latency independently from MCP startup and provider latency.
- Cache normalized catalog metadata alongside the existing MCP catalog cache.
- Avoid adding a second LLM call merely to select tools in the initial implementation.
- Compare token usage before and after routing.
- Ensure deferred expansion does not repeatedly respawn MCP servers.

# Delivery sequence

Recommended release order:

1. Measurement fixture, provider capabilities, and hard-limit safety invariant.
2. Unified catalog and deterministic initial router.
3. Local deferred search and per-turn expansion.
4. Entra/Azure bundles and custom-agent enforcement.
5. Admin UX, preview, and telemetry.
6. Skills and curated workflows.
7. Optional OpenAI Responses API/native `tool_search` path.
8. Further result compaction and pagination.

The first three items should ship together as the minimum complete correction. A provider guard without relevance routing avoids the 400 but risks losing useful tools; routing without a provider guard lacks defense in depth.

# Temporary operational workaround

Until the first delivery is complete, disable **Expose EntraID tools to the default assistant** in Settings. The first-party `identity_investigate` and `ca_evaluate` tools remain independently available when enabled, so cached identity workflows can continue without eagerly exposing all 44 raw Graph operations.

# Final recommendation

Use three complementary layers:

1. **Tool router:** controls which executable schemas the model sees now.
2. **Deferred catalog search:** lets the model request additional permitted tools during a turn.
3. **Skills:** progressively loads workflow instructions that explain how to use those tools.

Add native OpenAI `tool_search` only as an optimized provider-specific implementation behind the same abstraction. This fixes the immediate 133-versus-128 failure while preserving cross-provider support, security controls, and future extensibility.

# Implementation record

All phases are implemented locally:

- Unified catalog metadata, stable collision detection, source/domain/bundle counts, and schema-byte diagnostics.
- Provider hard-limit guards and concise over-limit errors.
- Deterministic 24-tool initial routing with a 32-tool per-turn ceiling.
- Provider-neutral `search_tools`, `load_tool_bundle`, `load_skill`, and `read_tool_artifact` capabilities.
- Azure and Entra domain bundles, per-tool global controls, behavioral permission filtering, and read/write-aware selection.
- Scoped custom-agent Azure/Entra tools and bundles, with backward-compatible allow-all fields and effective-surface preview.
- Six on-demand support skills.
- Optional direct-OpenAI Responses API client-executed native tool search, off by default; local routing remains the fallback.
- Valid paginated large-result artifacts instead of mid-JSON character truncation.
- Per-turn logs and audit metadata for counts, schema bytes, sources, domains, selected names, and reasons.

Validation completed locally:

- Production-sized catalog regression: 133 executable tools plus four routing tools are bounded before provider submission.
- Greeting with Entra enabled: 138 eligible tools, four initially exposed, successful Azure OpenAI response, no provider 400.
- Conditional Access routing: seven selected tools, limited to internal discovery and relevant Entra CA tools.
- Deferred-search E2E: `search_tools` expanded the surface, then `find_ownerless_applications` executed successfully.
- Backend fast suite: 5,517 passed, 16 skipped, 8 expected failures.
- Frontend TypeScript and production Vite build passed.
