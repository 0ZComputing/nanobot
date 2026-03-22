# Planning Task: GitHub Issue #{N}
Title: {TITLE}
Description: {BODY}

## Your job: explore the codebase, write PLAN.md, commit it, post a comment, and move the issue to Plan Review. Then exit.

STEP 1 — Use a team of agents to explore the codebase to understand what needs to change.

STEP 2 — Write PLAN.md in the repo root:
  - Guiding Development Principles: SOLID DRY YAGNI KISS
  - Problem: one-paragraph summary
  - Decision: chosen approach with rationale
  - Affected files: every file that will change
  - Edge cases: how each is handled
  - Test strategy: unit and integration tests to write
  Default: nullable DB columns use NULL over empty string.

STEP 3 — Commit and push:
  git add PLAN.md
  git commit -m "plan: #{N} - {TITLE}"
  git push -u origin ai/issue-{N}

STEP 4 — Comment on the issue (NOT a PR) with a HIGH-LEVEL overview only:
  gh issue comment {N} --repo 0ZComputing/hashi \
    --body "## Plan\n\n{2-3 sentence summary of approach}\n\nFull plan committed to branch ai/issue-{N} in PLAN.md."

STEP 5 — Move project status → Plan Review and reassign for review:
  ITEM_ID=$(gh project item-list 9 --owner 0ZComputing --format json --limit 200 \
    --jq '.items[] | select(.content.number == {N}) | .id')
  gh project item-edit \
    --project-id PVT_kwDODgSGac4BPTGn \
    --id "$ITEM_ID" \
    --field-id PVTSSF_lADODgSGac4BPTGnzg9vz6Y \
    --single-select-option-id d193658a
  gh issue edit {N} --repo 0ZComputing/hashi --remove-assignee sledcycle --add-assignee heyenlow,tolvap

STEP 6 — Remove this task from the active-tasks registry:
  Read ~/.nanobot/workspace/active-tasks.json, parse as JSON array, remove the entry where issue_number == {N}, write the updated array back.

Exit when done.
