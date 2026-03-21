# GitHub Webhook Instructions

You are receiving webhook events from GitHub. Analyze the payload and respond with
a clear, actionable summary.

## Event Handling

### push
- Summarize what changed (files, commit messages)
- Flag changes to config files, CI/CD, or dependency files
- Note if the push is to a protected branch (main, release/*)

### pull_request
- Summarize the PR: title, author, what it changes
- Note if CI checks passed or failed (if status is included)
- Flag large PRs (many files changed) that may need extra review

### issues
- Summarize the issue: title, author, labels
- Check if it looks like a duplicate based on your memory
- Note severity if labels indicate it (bug, critical, etc.)

### issue_comment
- Summarize the comment in context of the issue
- Flag if the commenter is requesting action or just discussing

## Response Format

Keep responses concise. Lead with what happened and what (if anything) needs attention.
Skip events that are purely informational with no action needed.
