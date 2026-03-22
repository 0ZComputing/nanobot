# Project Flow

## Status Pipeline

```
                          +--------+
                          |  Hold  |
                          +--------+
                              ↕
+----------+    +-----------+    +------------------+    +--------------+
|  Backlog | ←→ | UI Proto  | →  | Ready for Plan   | →  | In Planning  |
+----------+    +-----------+    +------------------+    +--------------+
     ↑                                                          |
     |                                                          ↓
     |                                                   +--------------+
     |                                                   | Plan Review  |
     |                                                   +--------------+
     |                                                          |
     |                                                          ↓
     |                                                   +--------------+    +----------------+
     |                                                   | Ready for Dev| →  | In Development |
     |                                                   +--------------+    +----------------+
     |                                                                              |
     |    unblocked + in backlog                                                    ↓
     +←─────────────────────────────────────────────────────────────+        +--------+
                                                                    |        | Review |
                                                                    |        +--------+
                                                                    |            |
                                                                    |            ↓
                                                                    +←───── +--------+
                                                                           |  Done  |
                                                                           +--------+
```

## What Happens at Each Status

### Hold
- **Trigger:** Human moves card
- **Action:** No-op
- **Who:** —

### Backlog
- **Trigger:** Human moves card, or auto-promoted when unblocked
- **Action:** No-op
- **Who:** —

### UI Prototyping
- **Trigger:** Human moves card
- **Action:** No-op
- **Who:** —

### Ready for Planning
- **Trigger:** Human moves card
- **Action:**
  1. Verify not already tracked in active-tasks.json
  2. Create git worktree from `origin/dev`
  3. Push branch `ai/issue-{N}` and link to issue
  4. Register in active-tasks.json
  5. Move status → In Planning
- **Who:** Nanobot orchestration (setup only, no Claude CLI)

### In Planning
- **Trigger:** Set by Ready for Planning workflow
- **Action:**
  1. Detect NEW vs REVISION (check for existing PLAN.md)
  2. Render prompt template (planning-agent.md or planning-revision-agent.md)
  3. Fire Claude CLI one-shot
  4. If UI issue: enable Figma MCP
- **Who:** Nanobot orchestration → Claude one-shot
- **Claude one-shot handles:** Explore codebase, write PLAN.md, commit, push, comment on issue, move to Plan Review, reassign to reviewers

### Plan Review
- **Trigger:** Set by Claude planning agent on completion
- **Action:** No-op (status set + reassignment done by Claude one-shot)
- **Who:** —

### Ready for Dev
- **Trigger:** Human moves card after approving plan, or auto-promoted when blocker completes
- **Action:** TODO — not yet implemented
- **Who:** —

### In Development
- **Trigger:** Set by Ready for Dev workflow
- **Action:** TODO — not yet implemented
- **Who:** —

### Review
- **Trigger:** Set by Claude implementation agent on completion
- **Action:** TODO — not yet implemented
- **Who:** —

### Done
- **Trigger:** Human moves card after approving PR
- **Action:** TODO — not yet implemented
- **Who:** —

## Roles

| Role | Responsibility |
|------|---------------|
| **Human** | Brainstorming, writing issue specs, moving cards (Ready for Planning, Ready for Dev, Done), reviewing plans and PRs |
| **Nanobot (orchestration)** | Webhook routing, worktree setup, branch linking, template rendering, deduplication via active-tasks.json, spawning Claude CLI |
| **Claude CLI (one-shot)** | All code changes — planning (PLAN.md), implementation (TDD), PRs, status transitions, reassignment, issue comments |

## Automation Triggers

| Event | Source | Automated? |
|-------|--------|-----------|
| Issue created | Human | No |
| Brainstorming | Human | No — never automated |
| Move to Hold / Backlog / UI Prototyping | Human | No-op |
| Move to Ready for Planning | Human | Yes → setup worktree + branch, move to In Planning |
| Move to In Planning | Orchestration | Yes → fire Claude CLI planning one-shot |
| Move to Plan Review | Claude one-shot | No-op |
| Move to Ready for Dev | Human | TODO |
| Move to In Development | Orchestration | TODO |
| Move to Review | Claude one-shot | TODO |
| Move to Done | Human | TODO |

## Token Efficiency

The webhook handler is designed to minimize LLM token usage:
- **No-op statuses** respond with `"#{N} - noop"` and make zero tool calls (single LLM call)
- **Workflow statuses** only make the exact exec calls needed for their step
- **Each status does one small job** — Ready for Planning does setup, In Planning fires the agent
- **memoryWindow: 5** keeps conversation context small across events
- **maxToolIterations: 10** caps runaway tool loops
