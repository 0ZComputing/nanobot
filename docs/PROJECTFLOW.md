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
- **Action:** Notify Discord
- **Who:** Nanobot orchestration

### Backlog
- **Trigger:** Human moves card, or auto-promoted when unblocked by a completed issue
- **Action:** Notify Discord
- **Who:** Nanobot orchestration

### UI Prototyping
- **Trigger:** Human moves card
- **Action:** No-op
- **Who:** —

### Ready for Planning
- **Trigger:** Human moves card
- **Action:**
  1. Create git worktree from `origin/dev`
  2. Render planning prompt template
  3. Move status → In Planning
  4. Fire Claude CLI one-shot (`writing-plans` skill)
  5. If UI issue: enable Figma MCP
- **Who:** Nanobot orchestration → Claude one-shot

### In Planning
- **Trigger:** Set automatically by orchestration layer
- **Action:** No-op
- **Who:** —

### Plan Review
- **Trigger:** Set by Claude planning agent on completion
- **Action:**
  1. Read PLAN.md from branch
  2. Post plan summary to Discord
  3. Reassign to reviewers (heyenlow, tolvap)
- **Who:** Claude one-shot (status move + reassign), Nanobot (Discord notification)

### Ready for Dev
- **Trigger:** Human moves card after approving plan, or auto-promoted when blocker completes
- **Action:**
  1. Render implementation prompt template
  2. Move status → In Development
  3. Fire Claude CLI one-shot (`executing-plans` + `test-driven-development` skills)
  4. If UI issue: enable Figma MCP
- **Who:** Nanobot orchestration → Claude one-shot

### In Development
- **Trigger:** Set automatically by orchestration layer
- **Action:** No-op
- **Who:** —

### Review
- **Trigger:** Set by Claude implementation agent on completion
- **Action:**
  1. Self-review via `requesting-code-review` skill
  2. Post PR link to Discord
  3. Request PR reviews, reassign to reviewers (heyenlow, tolvap)
- **Who:** Claude one-shot (status move + reassign + PR), Nanobot (Discord notification)

### Done
- **Trigger:** Human moves card after approving PR
- **Action:**
  1. Verify all tests pass (`verification-before-completion` skill)
  2. Clean up git worktree
  3. Archive active-tasks.json entry
  4. Notify Discord
  5. Check for unblocked issues: if any blocker-dependents are now fully unblocked and sitting in Backlog → move to Ready for Dev
- **Who:** Nanobot orchestration

## Roles

| Role | Responsibility |
|------|---------------|
| **Human** | Brainstorming, writing issue specs, moving cards (Ready for Planning, Ready for Dev, Done), reviewing plans and PRs |
| **Nanobot (orchestration)** | Webhook routing, worktree management, template rendering, deduplication, Discord notifications, cleanup, unblocking dependents |
| **Claude CLI (one-shot)** | All code changes — planning (PLAN.md), implementation (TDD), PRs, status transitions, reassignment |

## Automation Triggers

| Event | Source | Automated? |
|-------|--------|-----------|
| Issue created | Human | No |
| Brainstorming | Human | No — never automated |
| Move to Ready for Planning | Human | Yes → planning workflow |
| Move to Ready for Dev | Human | Yes → implementation workflow |
| Move to Done | Human | Yes → cleanup + unblock dependents |
| Move to Hold / Backlog | Human | Yes → Discord notification |
| Move to In Planning / In Development | Agent | No-op |
| Move to Plan Review / Review | Agent | Discord notification only |
| Backlog issue unblocked | Agent (on Done) | Yes → auto-promote to Ready for Dev |
