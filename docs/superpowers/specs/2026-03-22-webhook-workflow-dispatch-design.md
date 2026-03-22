# Webhook-Driven Workflow Dispatch

## Problem

Nanobot currently relies on cron-based watcher scripts (`planning-watcher.sh`, `implementation-watcher.sh`) that poll the GitHub project board every 30-60 minutes to detect status changes. This introduces latency between a status change and workflow execution, and the polling approach is wasteful when no changes have occurred.

## Decision

Replace cron-based polling with real-time webhook-driven dispatch. When a GitHub `projects_v2_item` event arrives indicating a status change on project #9, nanobot routes to the appropriate workflow based on the new status. Each workflow leverages specific superpowers skills for structured execution. Workflows run inside the nanobot-1 Docker container using mounted volumes for the hashi repo and worktrees.

**Key principles:**
- Brainstorming is always done by humans. The issue body contains the brainstormed spec. Sled (the bot) never brainstorms — it picks up from `writing-plans` onward.
- **Orchestration vs execution split:** Nanobot handles all orchestration (webhook routing, status checks, template rendering, deduplication, notifications, cleanup). All actual code changes are done by firing off a Claude CLI one-shot — nanobot never writes code itself.
- **Figma MCP for UI work:** When an issue involves UI changes (detected by `UI` label or UI-related keywords in title/body), the Claude one-shot is configured to use the Figma MCP server for design reference.

## Constraints

- Only issues assigned to `sledcycle` are processed
- All Discord notifications go to channel `1476319678707011768`
- Workflows run inside nanobot-1 container
- Hashi repo and worktrees are mounted as read-write volumes
- `~/.claude` mounted read-write (Claude CLI needs to write session state)
- `ANTHROPIC_API_KEY` provided via environment/secrets
- `active-tasks.json` lives at `/root/.nanobot/workspace/active-tasks.json` inside the container (mapped from `./configs/nanobot-1/workspace/active-tasks.json` on host); created as `[]` if missing
- Figma MCP config at `/root/.nanobot/figma-mcp.json` inside the container

## Architecture

```
GitHub (projects_v2_item event)
  → Caddy reverse proxy (port 443)
    → nanobot-1 (port 18790)
      → ORCHESTRATION LAYER (nanobot LLM agent):
        → webhook handler validates signature
        → fetch issue details via GraphQL (number, title, body, assignees, labels)
        → check assignee filter (sledcycle)
        → check active-tasks.json for deduplication
        → status router determines workflow
        → orchestration tasks: git worktree setup, template rendering,
          status transitions, Discord notifications, cleanup
        → EXECUTION LAYER (Claude CLI one-shot):
          → fired off via nohup for code-changing workflows
          → planning: writes PLAN.md
          → implementation: writes code via TDD
          → if UI issue: configured with Figma MCP server
```

## Status → Workflow Routing

| Status              | Workflow          | Superpowers Skill                              | Description                                                                 |
|---------------------|-------------------|-------------------------------------------------|-----------------------------------------------------------------------------|
| Hold                | notify            | —                                               | Post to Discord: issue put on hold                                         |
| Backlog             | notify            | —                                               | Post to Discord: issue moved to backlog                                    |
| UI Prototyping      | no-op             | —                                               | No action                                                                  |
| Ready for Planning  | planning          | `writing-plans`                                 | Create worktree, write implementation plan from issue spec                 |
| In Planning         | no-op             | —                                               | Set automatically by planning workflow                                     |
| Plan Review         | plan-review       | —                                               | Read PLAN.md from branch, post summary to Discord, reassign to reviewers   |
| Ready for Dev       | implementation    | `executing-plans` + `test-driven-development`   | Execute plan with TDD in worktree                                          |
| In Development      | no-op             | —                                               | Set automatically by implementation workflow                               |
| Review              | code-review       | `requesting-code-review`                        | Self-review, then link PR in Discord, request reviews, reassign            |
| Done                | cleanup           | `verification-before-completion`                | Verify everything passes, clean up worktree, archive task, notify Discord  |

## Agent-Initiated vs Human-Initiated Status Changes

Status changes can come from two sources:
1. **Human-initiated**: Someone moves a card on the project board (e.g., "Ready for Planning", "Ready for Dev")
2. **Agent-initiated**: A Claude workflow moves status after completing its work (e.g., planning agent moves to "Plan Review")

**Rule:** Only human-initiated transitions trigger new workflows. Agent-initiated transitions trigger notification-only workflows (Plan Review, Review) or no-ops (In Planning, In Development).

**Implementation:** Before dispatching a workflow, check `active-tasks.json`. If an entry exists for the issue with a status that would naturally transition to the new status (e.g., `in_planning` → Plan Review), treat it as agent-initiated and run only the notification portion, not a full workflow spawn.

| Transition | Source | Action |
|------------|--------|--------|
| → Ready for Planning | Human | Full planning workflow |
| → In Planning | Agent | No-op (set by planning workflow) |
| → Plan Review | Agent | Notify Discord + reassign only |
| → Ready for Dev | Human | Full implementation workflow |
| → In Development | Agent | No-op (set by impl workflow) |
| → Review | Agent | Notify Discord + request PR reviews + reassign only |
| → Done | Human | Full cleanup workflow |
| → Hold | Human | Notify Discord |
| → Backlog | Human | Notify Discord |

## Responsibility Boundary

**Nanobot orchestration layer owns:**
- Webhook event processing and routing
- Deduplication via active-tasks.json
- Git worktree creation/deletion
- Template rendering (sed substitution)
- Registering/deregistering entries in active-tasks.json
- Discord notifications for all status changes
- Distinguishing agent vs human transitions
- Detecting UI issues and configuring Figma MCP for Claude one-shots

**Claude CLI one-shots own:**
- All code changes (nanobot never writes code)
- Planning: exploring codebase, writing PLAN.md
- Implementation: TDD, commits, pushes
- GitHub API calls (PR creation, status transitions, reassignment, issue comments)
- Referencing Figma designs via MCP when configured

This means the prompt templates remain responsible for moving status and reassigning at the end of their workflow (as they do now). The orchestration layer does NOT duplicate those operations. Nanobot sets up the environment and fires off the one-shot; the one-shot does all the actual work.

## Webhook Event Processing

### Event Filter

```
on projects_v2_item event:
  if action != "edited" → skip
  if project != #9 (PVT_kwDODgSGac4BPTGn) → skip
  if field changed != Status (PVTSSF_lADODgSGac4BPTGnzg9vz6Y) → skip
  fetch issue details (number, title, body, assignees) via GraphQL
  if "sledcycle" not in assignees → skip

  # Deduplication
  check active-tasks.json for existing entry with this issue_number
  if entry exists with status that naturally precedes new_status → agent-initiated, notify only
  if entry exists with same target status → duplicate event, skip

  route to workflow based on new status value
```

### Workflow: notify (Hold, Backlog, agent-initiated transitions)

1. Format a Discord message: `"Issue #{N}: {TITLE} moved to {STATUS}"`
2. Post to Discord channel `1476319678707011768` via nanobot Discord channel integration

### Workflow: planning (Ready for Planning)

**Superpowers: `writing-plans`** — The issue body contains the brainstormed spec. The planning agent uses `writing-plans` to create a structured implementation plan.

1. Check if branch `ai/issue-{N}` exists on origin → determines NEW vs REVISION path
2. **NEW path:**
   - `git fetch origin` in mounted hashi repo
   - Create worktree: `git worktree add /root/hashi-worktrees/issue-{N} -b ai/issue-{N} origin/dev`
   - Render `planning-agent.md` template with sed substitution (`{N}`, `{TITLE}`, `{BODY}`)
3. **REVISION path:**
   - Fetch latest issue comment for feedback
   - Render `planning-revision-agent.md` template (`{N}`, `{TITLE}`, `{FEEDBACK}`)
4. Move project status → "In Planning" (`15a164f6`) — done by orchestration layer before spawning
5. Register in `active-tasks.json` with `status: "in_planning"`
6. Detect if UI issue (has `UI` label or UI keywords in title/body)
7. Spawn Claude CLI one-shot in background via `nohup`:
   - Base: `claude -p --dangerously-skip-permissions --model opus`
   - If UI issue: add `--mcp-config figma-mcp.json` to enable Figma MCP server
8. (The Claude one-shot handles: committing PLAN.md, moving to "Plan Review", reassigning, and cleaning up active-tasks.json)

### Workflow: plan-review (Plan Review — agent-initiated)

1. Read `PLAN.md` from branch `ai/issue-{N}` in the worktree
2. Post plan summary to Discord channel `1476319678707011768`
3. (Reassignment already handled by the planning agent prompt)

### Workflow: implementation (Ready for Dev)

**Superpowers: `executing-plans` + `test-driven-development`** — Execute the implementation plan using TDD discipline.

1. Render `impl-agent.md` template with sed substitution (`{N}`, `{TITLE}`)
2. Move project status → "In Development" (`47fc9ee4`) — done by orchestration layer before spawning
3. Register in `active-tasks.json` with `status: "in_development"`
4. Detect if UI issue (has `UI` label or UI keywords in title/body)
5. Spawn Claude CLI one-shot in background via `nohup` in existing worktree:
   - Base: `claude -p --dangerously-skip-permissions --model opus`
   - If UI issue: add `--mcp-config figma-mcp.json` to enable Figma MCP server
6. (The Claude one-shot handles: TDD, PR creation, moving to "Review", reassigning, and cleaning up active-tasks.json)

### Workflow: code-review (Review — agent-initiated)

**Superpowers: `requesting-code-review`** — The agent self-reviews its own work before humans look at it.

1. Find PR for branch `ai/issue-{N}` via `gh pr list`
2. Post PR link to Discord channel `1476319678707011768`
3. (PR review requests and reassignment already handled by the impl agent prompt)

### Workflow: cleanup (Done)

**Superpowers: `verification-before-completion`** — Verify all tests pass, no regressions, before cleaning up.

1. In the worktree, run full test suite and verify passing
2. Remove worktree: `git worktree remove /root/hashi-worktrees/issue-{N}`
3. Remove entry from `active-tasks.json`
4. Post to Discord: `"Issue #{N}: {TITLE} — Done"`
5. **Unblock dependent issues:**
   - Query the completed issue's timeline for "connected" / "blocked by" references using GraphQL (`closingIssuesReferences` or timeline cross-reference events)
   - For each issue that listed `#{N}` as a blocker:
     - Check if ALL of its blockers are now closed/done (not just this one)
     - If fully unblocked AND current project status is "Backlog" → move to "Ready for Dev" (`4cc28e6c`)
     - Post to Discord: `"Issue #{BLOCKED_N}: {BLOCKED_TITLE} unblocked — moved to Ready for Dev"`

## Docker Changes

### docker-compose.yml — nanobot-1

**Volume mounts** (add to existing):

```yaml
volumes:
  # ... existing mounts ...
  - ~/LocalDev/hashi:/root/hashi
  - ~/LocalDev/hashi-worktrees:/root/hashi-worktrees
  - ~/.claude:/root/.claude        # changed from :ro to read-write
```

**Environment** (add):

```yaml
environment:
  - ANTHROPIC_API_KEY_FILE=/run/secrets/anthropic-api-key
```

Or via secrets mount (preferred):

```yaml
secrets:
  - anthropic-api-key
```

### Background process management

The container uses `nohup` + background processes instead of tmux (which is not installed in the container). Claude CLI is spawned as:

```bash
nohup claude -p --dangerously-skip-permissions --model opus \
  < /tmp/claude-plan-{N}.md \
  > /tmp/claude-plan-{N}.out 2>&1 &
```

### Webhook config

Add `projects_v2_item` to `allowEvents` in `configs/nanobot-1/config.json`:

```json
"allowEvents": ["push", "pull_request", "issues", "issue_comment", "projects_v2_item"]
```

### Webhook handler

The dispatch logic is added to the nanobot LLM prompt (`shared/webhooks/github.md`). The nanobot agent processes `projects_v2_item` events and uses its `exec` tool to run git commands, render templates, and spawn Claude CLI.

**Required config changes:**
- `restrictToWorkspace: false` — the agent needs to operate on mounted hashi repo
- `tools.exec.timeout: 300` — increase from 60s to allow git operations and template rendering (Claude CLI itself runs detached via nohup, so timeout only needs to cover setup)

## Error Handling

### Workflow failure

- If Claude CLI exits non-zero, the output is logged to `/tmp/claude-{workflow}-{N}.out`
- The HEARTBEAT.md monitoring system (already exists) detects stale entries in `active-tasks.json` and can alert via Discord
- Failed workflows leave the issue in the "In Planning" or "In Development" status — a human can move it back to retry

### Webhook delivery failures

- GitHub retries webhook delivery on failure (built-in)
- Deduplication via `active-tasks.json` prevents double-dispatch on retries

### Git operation failures

- If `git worktree add` fails (already exists), continue with existing worktree
- If `git fetch` fails, notify Discord and skip the issue

## Files to Remove

- `~/.nanobot/cron/planning-watcher.sh`
- `~/.nanobot/cron/implementation-watcher.sh`
- Cron job entries for planning-watcher and implementation-watcher in `~/.nanobot/cron/jobs.json`
- Any system crontab entries that invoke these scripts

## Files to Add/Modify

- `shared/webhooks/github.md` — add `projects_v2_item` dispatch logic with status routing
- `docker-compose.yml` — add hashi + worktrees volume mounts, change .claude to rw, add ANTHROPIC_API_KEY
- `configs/nanobot-1/config.json` — add `projects_v2_item` to allowEvents, set `restrictToWorkspace: false`, increase exec timeout
- `impl-agent.md` — already fixed (was corrupted, STEP 1-2 restored)
- Prompt templates (`planning-agent.md`, `planning-revision-agent.md`) — no content changes, available via shared volume
- `configs/nanobot-1/figma-mcp.json` — Figma MCP server config for Claude CLI, mounted into container
- `configs/nanobot-1/workspace/active-tasks.json` — ensure exists as `[]` if missing

## GitHub Project IDs Reference

| Entity | ID |
|--------|----|
| Project | `PVT_kwDODgSGac4BPTGn` |
| Status field | `PVTSSF_lADODgSGac4BPTGnzg9vz6Y` |
| Hold | `0ff6962c` |
| Backlog | `f75ad846` |
| UI Prototyping | `2359674f` |
| Ready for Planning | `99c0b68a` |
| In Planning | `15a164f6` |
| Plan Review | `d193658a` |
| Ready for Dev | `4cc28e6c` |
| In Development | `47fc9ee4` |
| Review | `3b449a1a` |
| Done | `98236657` |

## Security

- All webhook payloads validated via HMAC signature (existing)
- Payload content (issue titles, bodies) treated as untrusted — used only for template substitution, never executed
- `--dangerously-skip-permissions` used for Claude CLI (same as current cron approach)
- Assignee filter prevents unauthorized workflow dispatch
- `ANTHROPIC_API_KEY` stored in secrets volume, not in docker-compose.yml or env files
