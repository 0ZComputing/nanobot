# Implementation Task: GitHub Issue #{N}
Title: {TITLE}

## Your job: read PLAN.md, implement the solution using TDD, open a PR, and exit.

STEP 1 — Read PLAN.md in the repo root. This is your implementation plan.

STEP 2 — Understand the plan fully before writing any code. Use agents to explore the codebase for context on affected files.

STEP 3 — TDD: RED → GREEN → REFACTOR for every change.
  - Write failing test first
  - Implement minimal code to pass
  - Refactor
  - Run full test suite before every commit
  - Only commit when all tests pass

STEP 4 — Principles: KISS, SOLID, YAGNI, DRY (extract at 3+ occurrences).

STEP 5 — Atomic commits referencing issue #{N}.

STEP 6 — Open draft PR to dev (NOT main):
  git push (branch already exists)
  gh pr create --repo 0ZComputing/hashi \
    --title "WIP: {TITLE}" \
    --body "Closes #{N}\n\nSee PLAN.md for implementation plan.\n\n---\n*Do not merge until marked ready.*" \
    --base dev --head ai/issue-{N} --draft

STEP 7 — Delete PLAN.md and commit:
  git rm PLAN.md
  git commit -m "chore: remove PLAN.md for #{N}"

STEP 8 — When all tests pass and implementation is complete:
  PR_NUMBER=$(gh pr list --repo 0ZComputing/hashi --head ai/issue-{N} --json number --jq '.[0].number')
  PR_URL=$(gh pr list --repo 0ZComputing/hashi --head ai/issue-{N} --json url --jq '.[0].url')
  gh pr ready --repo 0ZComputing/hashi $PR_NUMBER
  gh pr edit --repo 0ZComputing/hashi $PR_NUMBER --title "{TITLE}"
  gh issue comment {N} --repo 0ZComputing/hashi \
    --body "Development complete. PR ready for review: $PR_URL"

STEP 9 — Move project status → Review and reassign for review:
  ITEM_ID=$(gh project item-list 9 --owner 0ZComputing --format json --limit 200 \
    --jq '.items[] | select(.content.number == {N}) | .id')
  gh project item-edit \
    --project-id PVT_kwDODgSGac4BPTGn \
    --id "$ITEM_ID" \
    --field-id PVTSSF_lADODgSGac4BPTGnzg9vz6Y \
    --single-select-option-id 3b449a1a
  gh issue edit {N} --repo 0ZComputing/hashi --remove-assignee sledcycle --add-assignee heyenlow,tolvap

STEP 10 — Remove this task from the active-tasks registry:
  Read ~/.nanobot/workspace/active-tasks.json, parse as JSON array, remove the entry where issue_number == {N}, write the updated array back.

Exit when done.
