# Plan Revision: GitHub Issue #{N}
Title: {TITLE}

Human feedback to address:
---
{FEEDBACK}
---

STEP 1 — Read the existing PLAN.md.
STEP 2 — Update PLAN.md to address the feedback above.
STEP 3 — Commit and push:
  git add PLAN.md
  git commit -m "plan(rev): #{N} address review feedback"
  git push

STEP 4 — Comment on the issue (NOT a PR) with updated high-level summary:
  gh issue comment {N} --repo 0ZComputing/hashi \
    --body "## Plan (revised)\n\n{2-3 sentence summary of changes made}\n\nUpdated plan on branch ai/issue-{N} in PLAN.md."

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
