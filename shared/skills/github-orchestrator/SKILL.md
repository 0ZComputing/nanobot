---
name: github-orchestrator
description: "Orchestrate GitHub issues by dispatching Claude Code CLI subagents to work on development tasks."
metadata: {"nanobot":{"emoji":"🎯","requires":{"bins":["gh","claude"]}}}
---

# GitHub Issue Orchestrator

Orchestrate GitHub issues by dispatching Claude Code CLI subagents to work on development tasks.

## Overview

```
┌─────────────┐     ┌───────────────┐     ┌─────────────┐
│   GitHub    │────▶│  Orchestrator │────▶│ Claude Code │
│   Issues    │     │   (nanobot)   │     │    CLI      │
└─────────────┘     └───────────────┘     └─────────────┘
       │                    │                    │
       │                    ▼                    │
       │            ┌───────────────┐            │
       └───────────▶│ PR + Comment  │◀───────────┘
                    └───────────────┘
```

## Setup

### 1. Install Claude Code CLI
```bash
npm install -g @anthropic-ai/claude-code
```

### 2. Configure target repository
```bash
export ORCHESTRATOR_REPO="owner/repo"
export ORCHESTRATOR_ASSIGNEE="sledcycle"  # Assignee to watch for
```

## Workflow

### Manual Trigger
```bash
# List issues assigned to sledcycle
gh issue list --repo $REPO --assignee sledcycle --state open --json number,title,body,assignees

# Pick up a specific issue
nanobot agent -m "Work on issue #123 from owner/repo"
```

### Automated Polling (via HEARTBEAT.md)
nanobot runs two cron jobs every 10 minutes. See HEARTBEAT.md for full instructions.

The trigger is assignee-based — issues are picked up when assigned to `@sledcycle`:
```bash
gh issue list \
  --assignee sledcycle \
  --repo {owner}/{repo} \
  --state open \
  --json number,title,body,assignees
```

---

## Issue Processing Pipeline

### Step 1: Fetch and Parse Issue
```bash
gh issue view 123 --repo owner/repo --json number,title,body,labels,comments
```

### Step 2: Create Worktree (Isolation)
```bash
cd /path/to/repo
git fetch origin
git worktree add ../repo-issue-123 -b ai/issue-123 origin/dev
```

### Step 3: Dispatch Claude Code
```bash
cd ../repo-issue-123
claude --print "$(cat <<EOF
You are working on GitHub issue #123.

**Title:** Fix the login bug
**Description:**
Users report that login fails when...

**Instructions:**
1. Analyze the codebase
2. Implement the fix
3. Write tests
4. Commit with message referencing the issue

When done, output TASK_COMPLETE.
EOF
)"
```

### Step 4: Create PR
```bash
git push -u origin ai/issue-123
gh pr create --repo owner/repo \
  --title "Fix: Issue #123 - Login bug" \
  --body "Closes #123\n\nAutomated fix by Claude Code." \
  --base dev --head ai/issue-123
```

### Step 5: Update Issue
```bash
gh issue comment 123 --repo owner/repo --body "🤖 Created PR #456 to address this issue."
```

---

## Orchestrator Script

Save as `scripts/orchestrate.sh`:

```bash
#!/bin/bash
set -e

REPO="${1:-$ORCHESTRATOR_REPO}"
ASSIGNEE="${2:-sledcycle}"
WORKDIR="${3:-/tmp/ai-workspaces}"

if [ -z "$REPO" ]; then
  echo "Usage: orchestrate.sh owner/repo [assignee] [workdir]"
  exit 1
fi

echo "🔍 Checking for issues assigned to '$ASSIGNEE' in $REPO..."

# Get open issues assigned to the bot account
ISSUES=$(gh issue list --repo "$REPO" --assignee "$ASSIGNEE" --state open --json number,title --jq '.[].number')

if [ -z "$ISSUES" ]; then
  echo "✅ No issues to process"
  exit 0
fi

for ISSUE_NUM in $ISSUES; do
  echo "📋 Processing issue #$ISSUE_NUM..."

  # Fetch issue details
  ISSUE_JSON=$(gh issue view "$ISSUE_NUM" --repo "$REPO" --json number,title,body)
  TITLE=$(echo "$ISSUE_JSON" | jq -r '.title')
  BODY=$(echo "$ISSUE_JSON" | jq -r '.body')

  # Create workspace
  WORKSPACE="$WORKDIR/issue-$ISSUE_NUM"
  mkdir -p "$WORKSPACE"

  # Clone if needed (or use worktree for existing repos)
  if [ ! -d "$WORKSPACE/.git" ]; then
    gh repo clone "$REPO" "$WORKSPACE" -- --depth=1
  fi

  cd "$WORKSPACE"
  git checkout -b "ai/issue-$ISSUE_NUM" 2>/dev/null || git checkout "ai/issue-$ISSUE_NUM"

  # Create prompt file
  cat > /tmp/claude-prompt-$ISSUE_NUM.md <<EOF
# Task: GitHub Issue #$ISSUE_NUM

**Repository:** $REPO
**Title:** $TITLE

## Description
$BODY

## Instructions
1. Analyze the codebase to understand the issue
2. Implement a fix or feature as described
3. Write or update tests if applicable
4. Make atomic commits with clear messages referencing #$ISSUE_NUM
5. When complete, summarize what you changed

Do not create a PR - just make the commits.
EOF

  echo "🤖 Dispatching Claude Code for issue #$ISSUE_NUM..."

  # Run Claude Code (non-interactive)
  claude --print "$(cat /tmp/claude-prompt-$ISSUE_NUM.md)" || {
    echo "⚠️ Claude Code failed for issue #$ISSUE_NUM"
    continue
  }

  # Check if there are commits to push
  if git diff --quiet origin/dev HEAD; then
    echo "ℹ️ No changes made for issue #$ISSUE_NUM"
    continue
  fi

  # Push and create PR
  git push -u origin "ai/issue-$ISSUE_NUM"

  PR_URL=$(gh pr create --repo "$REPO" \
    --title "AI: $TITLE" \
    --body "Closes #$ISSUE_NUM

This PR was automatically generated by Claude Code.

---
*Review carefully before merging.*" \
    --base dev --head "ai/issue-$ISSUE_NUM" 2>&1) || {
    echo "⚠️ PR creation failed (may already exist)"
    continue
  }

  echo "✅ Created PR: $PR_URL"

  # Comment on issue
  gh issue comment "$ISSUE_NUM" --repo "$REPO" \
    --body "🤖 I've created a PR to address this issue: $PR_URL"

done

echo "🎉 Orchestration complete!"
```

---

## Nanobot Integration

### HEARTBEAT.md Pattern (Current)

nanobot reads `~/.nanobot/workspace/HEARTBEAT.md` on each cron tick. Two jobs run every 10 minutes:

1. **issue-watcher** — detects new issues assigned to `@sledcycle`, creates worktrees, launches Claude Code in tmux
2. **agent-monitor** — checks tmux alive + PR status + CI, sends Discord notification when ready

```
~/.nanobot/workspace/HEARTBEAT.md       ← source of truth (deployed from Vault)
~/.nanobot/workspace/active-tasks.json  ← task registry
~/.nanobot/cron/jobs.json               ← cron job definitions
```

### As a Subagent Task (Manual)
```
spawn: "Process GitHub issue #123 from owner/repo using Claude Code CLI.
        Create a worktree, run claude with the issue context,
        then create a PR and comment on the issue."
```

---

## Safety Considerations

1. **Review before merge** - Always require human review of AI-generated PRs
2. **Sandboxing** - Run in isolated worktrees/containers
3. **Rate limiting** - Don't process too many issues at once
4. **Cost awareness** - Claude Code can use significant tokens on large codebases

---

## Advanced: Parallel Processing

For multiple issues, spawn separate subagents:

```python
# Pseudocode for nanobot
for issue in get_issues(repo, assignee="sledcycle"):
    spawn(f"Process issue #{issue.number}: {issue.title}")
```

Each subagent runs independently and reports back when done.
