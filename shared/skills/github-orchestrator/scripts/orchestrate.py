#!/usr/bin/env python3
"""
GitHub Issue Orchestrator for Claude Code

Monitors GitHub issues and dispatches Claude Code CLI to work on them.
Designed to integrate with nanobot's subagent system.

Usage:
    python orchestrate.py --repo owner/repo --label ai-task
    python orchestrate.py --repo owner/repo --issue 123
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import datetime


def run(cmd: list[str], cwd: str = None, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a command and return result."""
    print(f"  → {' '.join(cmd)}")
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=capture,
        text=True,
    )


def gh_api(endpoint: str, repo: str) -> dict:
    """Call GitHub API via gh cli."""
    result = run(["gh", "api", f"repos/{repo}/{endpoint}"])
    if result.returncode != 0:
        raise Exception(f"GitHub API error: {result.stderr}")
    return json.loads(result.stdout)


def get_issue(repo: str, number: int) -> dict:
    """Fetch issue details."""
    result = run([
        "gh", "issue", "view", str(number),
        "--repo", repo,
        "--json", "number,title,body,labels,comments,state"
    ])
    if result.returncode != 0:
        raise Exception(f"Failed to fetch issue: {result.stderr}")
    return json.loads(result.stdout)


def list_issues(repo: str, label: str) -> list[dict]:
    """List open issues with a specific label."""
    result = run([
        "gh", "issue", "list",
        "--repo", repo,
        "--label", label,
        "--state", "open",
        "--json", "number,title,labels"
    ])
    if result.returncode != 0:
        raise Exception(f"Failed to list issues: {result.stderr}")
    return json.loads(result.stdout)


def setup_workspace(repo: str, issue_num: int, base_dir: Path) -> Path:
    """Clone repo and create branch for the issue."""
    workspace = base_dir / f"issue-{issue_num}"
    branch = f"ai/issue-{issue_num}"
    
    if not workspace.exists():
        print(f"📦 Cloning {repo} to {workspace}...")
        result = run(["gh", "repo", "clone", repo, str(workspace), "--", "--depth=50"])
        if result.returncode != 0:
            raise Exception(f"Clone failed: {result.stderr}")
    
    # Create or checkout branch
    print(f"🌿 Setting up branch {branch}...")
    run(["git", "fetch", "origin"], cwd=str(workspace))
    
    # Check if branch exists remotely
    result = run(["git", "ls-remote", "--heads", "origin", branch], cwd=str(workspace))
    if branch in (result.stdout or ""):
        run(["git", "checkout", branch], cwd=str(workspace))
        run(["git", "pull", "origin", branch], cwd=str(workspace))
    else:
        # Create new branch from main/master
        run(["git", "checkout", "-b", branch, "origin/HEAD"], cwd=str(workspace))
    
    return workspace


def build_prompt(issue: dict, repo: str) -> str:
    """Build the prompt for Claude Code."""
    comments_text = ""
    if issue.get("comments"):
        comments_text = "\n\n## Discussion\n"
        for c in issue["comments"][:5]:  # Limit to 5 most recent
            comments_text += f"\n**{c.get('author', {}).get('login', 'unknown')}:** {c.get('body', '')[:500]}\n"
    
    return f"""# GitHub Issue #{issue['number']}

**Repository:** {repo}
**Title:** {issue['title']}

## Description

{issue.get('body', 'No description provided.')}
{comments_text}

---

## Your Task

You are an AI developer working on this GitHub issue. Please:

1. **Analyze** the codebase to understand the context and find relevant files
2. **Plan** your approach before making changes
3. **Implement** the fix or feature described in the issue
4. **Test** your changes if there's a test suite (run existing tests)
5. **Commit** your changes with clear messages that reference #{issue['number']}

### Guidelines
- Make small, focused commits
- Follow existing code style and conventions
- Don't modify unrelated files
- If you're unsure about something, leave a TODO comment

### When Done
Summarize what you changed and any notes for reviewers.
"""


def run_claude_code(workspace: Path, prompt: str, issue_num: int) -> bool:
    """Run Claude Code CLI with the prompt."""
    prompt_file = workspace / f".claude-prompt-{issue_num}.md"
    prompt_file.write_text(prompt)
    
    print(f"🤖 Running Claude Code in {workspace}...")
    
    # Claude Code CLI invocation
    # Using --print for non-interactive mode (adjust based on actual CLI)
    result = run(
        ["claude", "--print", prompt],
        cwd=str(workspace),
        capture=False  # Let output stream to terminal
    )
    
    return result.returncode == 0


def create_pr(repo: str, issue: dict, workspace: Path) -> str | None:
    """Create a PR for the changes."""
    branch = f"ai/issue-{issue['number']}"
    
    # Check if there are changes
    result = run(["git", "status", "--porcelain"], cwd=str(workspace))
    if not result.stdout.strip():
        # Check for unpushed commits
        result = run(["git", "log", "origin/HEAD..HEAD", "--oneline"], cwd=str(workspace))
        if not result.stdout.strip():
            print("ℹ️  No changes to commit")
            return None
    
    # Push branch
    print(f"📤 Pushing branch {branch}...")
    result = run(["git", "push", "-u", "origin", branch], cwd=str(workspace))
    if result.returncode != 0:
        print(f"⚠️  Push failed: {result.stderr}")
        return None
    
    # Check if PR already exists
    result = run([
        "gh", "pr", "list",
        "--repo", repo,
        "--head", branch,
        "--json", "url"
    ])
    existing = json.loads(result.stdout or "[]")
    if existing:
        print(f"ℹ️  PR already exists: {existing[0]['url']}")
        return existing[0]['url']
    
    # Create PR
    print("📝 Creating pull request...")
    pr_body = f"""Closes #{issue['number']}

## Summary

This PR was automatically generated by Claude Code to address:

> **{issue['title']}**

---

⚠️ **Please review carefully before merging.**

*Generated on {datetime.now().isoformat()}*
"""
    
    result = run([
        "gh", "pr", "create",
        "--repo", repo,
        "--title", f"[AI] {issue['title']}",
        "--body", pr_body,
        "--base", "main",
        "--head", branch
    ])
    
    if result.returncode != 0:
        # Try with master as base
        result = run([
            "gh", "pr", "create",
            "--repo", repo,
            "--title", f"[AI] {issue['title']}",
            "--body", pr_body,
            "--base", "master",
            "--head", branch
        ])
    
    if result.returncode == 0:
        # Extract PR URL from output
        pr_url = result.stdout.strip()
        return pr_url
    else:
        print(f"⚠️  PR creation failed: {result.stderr}")
        return None


def comment_on_issue(repo: str, issue_num: int, message: str):
    """Add a comment to the issue."""
    run([
        "gh", "issue", "comment", str(issue_num),
        "--repo", repo,
        "--body", message
    ])


def process_issue(repo: str, issue_num: int, base_dir: Path, dry_run: bool = False):
    """Process a single issue."""
    print(f"\n{'='*60}")
    print(f"📋 Processing issue #{issue_num} from {repo}")
    print(f"{'='*60}\n")
    
    # Fetch issue
    issue = get_issue(repo, issue_num)
    print(f"Title: {issue['title']}")
    print(f"State: {issue['state']}")
    
    if issue['state'] != 'OPEN':
        print("⏭️  Issue is not open, skipping")
        return
    
    if dry_run:
        print("\n[DRY RUN] Would process this issue:")
        print(build_prompt(issue, repo))
        return
    
    # Setup workspace
    workspace = setup_workspace(repo, issue_num, base_dir)
    
    # Build prompt
    prompt = build_prompt(issue, repo)
    
    # Run Claude Code
    success = run_claude_code(workspace, prompt, issue_num)
    
    if not success:
        print("⚠️  Claude Code did not complete successfully")
        comment_on_issue(repo, issue_num, 
            "🤖 I attempted to work on this issue but encountered problems. A human should take a look.")
        return
    
    # Create PR
    pr_url = create_pr(repo, issue, workspace)
    
    if pr_url:
        print(f"\n✅ Created PR: {pr_url}")
        comment_on_issue(repo, issue_num,
            f"🤖 I've created a pull request to address this issue: {pr_url}\n\nPlease review the changes.")
    else:
        print("\nℹ️  No PR created (no changes or already exists)")


def main():
    parser = argparse.ArgumentParser(description="GitHub Issue Orchestrator for Claude Code")
    parser.add_argument("--repo", "-r", required=True, help="Repository (owner/repo)")
    parser.add_argument("--issue", "-i", type=int, help="Specific issue number to process")
    parser.add_argument("--label", "-l", default="ai-task", help="Label to filter issues (default: ai-task)")
    parser.add_argument("--workdir", "-w", default="/tmp/ai-workspaces", help="Base directory for workspaces")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Show what would be done without doing it")
    parser.add_argument("--max-issues", "-m", type=int, default=3, help="Max issues to process in one run")
    
    args = parser.parse_args()
    
    base_dir = Path(args.workdir)
    base_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"🎯 GitHub Issue Orchestrator")
    print(f"   Repository: {args.repo}")
    print(f"   Workspace:  {base_dir}")
    print(f"   Dry run:    {args.dry_run}")
    
    if args.issue:
        # Process specific issue
        process_issue(args.repo, args.issue, base_dir, args.dry_run)
    else:
        # List and process issues with label
        print(f"\n🔍 Looking for issues with label '{args.label}'...")
        issues = list_issues(args.repo, args.label)
        
        if not issues:
            print("✅ No issues found to process")
            return
        
        print(f"📋 Found {len(issues)} issue(s)")
        
        for issue in issues[:args.max_issues]:
            process_issue(args.repo, issue['number'], base_dir, args.dry_run)
    
    print("\n🎉 Orchestration complete!")


if __name__ == "__main__":
    main()
