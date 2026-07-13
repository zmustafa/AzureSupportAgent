---
layout: default
title: Use Dashboard, Chat, and Deep Investigation
parent: Core and workload operations
grand_parent: How-to guides
nav_order: 1
description: Review dashboard posture, ask grounded questions, and run a structured Deep Investigation War Room.
permalink: /how-to/core-workloads/dashboard-chat/
---

# Use Dashboard, Chat, and Deep Investigation

![Deep Investigation hypothesis tree]({{ site.baseurl }}/assets/deep-investigation.png)

## Prerequisites

- `chat.use`, an active AI provider/model, and a readable Azure connection for live evidence.
- A workload is strongly recommended for scope control.
- Graph or Log Analytics access is required only when the investigation needs those sources.
- A configured Jira, ServiceNow, or XSOAR connector is required for the corresponding ticket handoff.

## Route

Dashboard: `/dashboard` (also `/`). Chat: `/chat`, `/chat?deep=1`, or an existing `/c/{chatId}`.

## How to triage from the Dashboard

1. Open `/dashboard` and complete visible **Setup guide** items.
2. Select the **primary workload** used by scope-aware trend cards.
3. Review workload, architecture, assessment, coverage, posture, risk, recent-investigation, insight, reservation, retirement, identity, and RBAC summaries visible to your role.
4. Treat each card as a cached navigation cue; open its owning feature before deciding.
5. Prioritize severity and deadline, then start Chat or open the source feature.

**Expected result:** A short, workload-scoped list of signals requiring review.

**Verification:** In each source feature, confirm scope, generated time, freshness, and whether missing means unknown rather than healthy.

## How to ask a grounded Chat question

1. Open `/chat`; select the intended provider/model, connection, and workload when shown.
2. State the symptom, UTC time window, expected behavior, affected service, recent changes, and checks already performed.
3. Send the question and inspect streamed tool calls as well as prose.
4. Ask follow-ups for missing scope, stale evidence, or conflicting observations.
5. Use a breakout thread when a new hypothesis should preserve the original conversation.
6. Copy or hand off only after checking resource names, timestamps, commands, and sensitive content.

**Expected result:** A bounded answer tied to accessible Azure evidence, with uncertainties called out.

**Verification:** Open cited resources or source modules and independently confirm the important observations.

## How to run a Deep Investigation War Room

1. Enable **Deep investigation** in the composer or open `/chat?deep=1`.
2. Select one workload and a precise incident interval.
3. Describe impact, symptoms, recent deployments, known-good comparisons, and exclusions.
4. Start the turn. Follow Research, Hypothesis, Validation, and Conclusion progress.
5. Expand the investigation tree and inspect every validated, invalidated, and inconclusive branch.
6. Check the conclusion's root cause, severity, evidence, confidence, and prioritized actions.
7. If inconclusive, add missing access or evidence and narrow the next turn instead of repeating the same broad prompt.
8. Where enabled, save a conclusive RCA to linked architecture Memory, create a ticket, or continue in a breakout.

**Expected result:** A persisted hypothesis tree and evidence-backed conclusion, or an explicit inconclusive result.

**Verification:** Reproduce the decisive check in the owning Azure/source feature and confirm the time window and workload boundary.

## How to manage chat lifecycle

1. Reopen `/c/{chatId}` to continue a durable conversation or reconnect after a stream interruption.
2. Archive completed chats when they should leave recents.
3. Restore from Trash if archived accidentally.
4. Purge or empty Trash only after retention and transcript-review requirements are met.

**Expected result:** Active and retained conversations remain intentional.

**Verification:** Confirm archived chats appear in Trash and purged chats no longer open by URL.

## Safety and rollback

- Never paste credentials, tokens, private keys, connection strings, or unnecessary personal data.
- Tool success proves execution, not completeness. Model confidence is not evidence quality.
- Deep Investigation is read-oriented by default, but proposed commands and linked workflows may mutate systems; review them separately.
- Stopping or navigating away may not cancel server-side work. Reopen the chat to inspect state.
- Archive is reversible; purge is not. Review transcripts before external ticket/PDF handoff.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| No model is available | Ask an administrator to test and activate a provider. |
| Stream stops | Reopen the chat; then check provider health, rate limits, and connection state. |
| Tool is unauthorized | Verify selected connection, workload, application permission, Azure RBAC, and Graph/log access. |
| Conclusion is missing | Narrow scope/time and ensure specialists can reach the required evidence. |
| RCA save is unavailable | Link an architecture and Memory to the workload; inconclusive conclusions are not suitable for case law. |
| Ticket fails | Test the selected connector and review its endpoint and credentials. |

## Related docs

- [Dashboard reference]({{ site.baseurl }}/user-guide/core/dashboard/)
- [Chat and Deep Investigation reference]({{ site.baseurl }}/user-guide/core/chat-deep-investigation/)
- [Mission Control recipes]({{ site.baseurl }}/how-to/core-workloads/mission-control/)
