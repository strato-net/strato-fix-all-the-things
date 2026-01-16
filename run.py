#!/usr/bin/env python3
"""
STRATO Fix All The Things - Main Entry Point

Usage:
    ./run.py <issue_numbers...>
    ./run.py 1234 5678 9012

Environment:
    GITHUB_TOKEN - GitHub personal access token
    GITHUB_REPO - Repository (default: blockapps/strato-platform)
    PROJECT_DIR - Path to local repository clone
    BASE_BRANCH - Base branch for PRs (default: develop)
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from src.config import Config
from src.git_ops import GitOps, GitError
from src.github_client import GitHubClient, GitHubError
from src.models import PipelineStatus
from src.pipeline import Pipeline


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Auto-fix GitHub issues using AI agents")
    parser.add_argument("issues", nargs="+", type=int, help="Issue numbers to process")
    parser.add_argument("--env", type=Path, help="Path to .env file")
    args = parser.parse_args()

    # Load configuration
    script_dir = Path(__file__).parent.resolve()
    env_file = args.env or script_dir / ".env"

    try:
        config = Config.load(env_file)
    except ValueError as e:
        print(f"[ERROR] Configuration error: {e}")
        return 1

    print("=" * 50)
    print("  STRATO Fix All The Things - Multi-Agent Pipeline")
    print("=" * 50)
    print(f"[INFO] Repository: {config.github_repo}")
    print(f"[INFO] Project: {config.project_dir}")
    print(f"[INFO] Issues to process: {len(args.issues)}")

    # Initialize clients
    github = GitHubClient(config.github_repo)
    git = GitOps(config.project_dir)

    # Ensure runs directory exists
    config.runs_dir.mkdir(exist_ok=True)

    # Track results
    results = {"success": [], "failed": [], "skipped": []}

    for i, issue_num in enumerate(args.issues, 1):
        print()
        print("=" * 50)
        print(f"  Issue #{issue_num} ({i}/{len(args.issues)})")
        print("=" * 50)

        try:
            result = process_issue(config, github, git, issue_num)
            if result == PipelineStatus.SUCCESS:
                results["success"].append(issue_num)
            elif result == PipelineStatus.SKIPPED:
                results["skipped"].append(issue_num)
            else:
                results["failed"].append(issue_num)
        except Exception as e:
            print(f"[ERROR] Unexpected error processing #{issue_num}: {e}")
            results["failed"].append(issue_num)

    # Print summary
    print()
    print("=" * 50)
    print("  Summary")
    print("=" * 50)

    if results["success"]:
        print(f"[SUCCESS] Completed ({len(results['success'])}): {', '.join(map(str, results['success']))}")
    if results["skipped"]:
        print(f"[WARNING] Skipped ({len(results['skipped'])}): {', '.join(map(str, results['skipped']))}")
    if results["failed"]:
        print(f"[ERROR] Failed ({len(results['failed'])}): {', '.join(map(str, results['failed']))}")

    print()
    print(f"[INFO] Total: {len(args.issues)} issues processed")
    print(f"[INFO] Run logs: {config.runs_dir}")

    return 0 if not results["failed"] else 1


def process_issue(config: Config, github: GitHubClient, git: GitOps, issue_num: int) -> PipelineStatus:
    """Process a single issue through the pipeline."""
    # Fetch issue details
    print(f"[INFO] Fetching issue #{issue_num}...")
    try:
        issue = github.get_issue(issue_num)
    except GitHubError as e:
        print(f"[ERROR] Failed to fetch issue: {e}")
        return PipelineStatus.FAILED

    print(f"[SUCCESS] Issue: {issue.title}")
    print(f"[INFO] Labels: {', '.join(issue.labels) if issue.labels else 'none'}")

    # Create run directory
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir = config.runs_dir / f"{timestamp}-issue-{issue_num}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Save issue data
    with open(run_dir / "issue.json", "w") as f:
        json.dump({
            "number": issue.number,
            "title": issue.title,
            "body": issue.body,
            "labels": issue.labels,
            "url": issue.url,
        }, f, indent=2)

    # Prepare git branch
    branch_name = f"claude-auto-fix-{issue_num}"
    print(f"[INFO] Preparing git branch...")

    try:
        # Check for dirty working tree
        if git.is_dirty():
            print("[ERROR] Working tree has uncommitted changes. Please commit or stash them.")
            return PipelineStatus.FAILED

        # Sync to base branch
        git.sync_to_remote("origin", config.base_branch)
        print(f"[SUCCESS] Synced to origin/{config.base_branch}")

        # Close existing PR if any
        existing_pr = github.find_open_pr(branch_name)
        if existing_pr:
            print(f"[WARNING] Closing existing PR #{existing_pr.number}...")
            github.close_pr(existing_pr.number)

        # Delete existing branch
        git.delete_branch(branch_name, force=True)
        git.delete_remote_branch(branch_name)

        # Create new branch
        git.create_branch(branch_name)
        print(f"[SUCCESS] Created branch {branch_name}")

    except GitError as e:
        print(f"[ERROR] Git error: {e}")
        return PipelineStatus.FAILED

    # Run pipeline
    print(f"[INFO] Starting multi-agent pipeline...")
    pipeline = Pipeline(config, issue, run_dir)
    state = pipeline.run()

    # Handle results
    if state.status == PipelineStatus.SUCCESS:
        return handle_success(config, github, git, issue, branch_name, state, run_dir)
    elif state.status == PipelineStatus.SKIPPED:
        return handle_skip(github, issue, state, run_dir)
    else:
        return handle_failure(github, git, issue, branch_name, state)


def handle_success(
    config: Config,
    github: GitHubClient,
    git: GitOps,
    issue,
    branch_name: str,
    state,
    run_dir: Path,
) -> PipelineStatus:
    """Handle successful pipeline completion."""
    print(f"[INFO] Pipeline succeeded, creating PR...")

    try:
        # Check if there are changes to commit
        if not git.has_changes():
            print("[WARNING] No changes to commit")
            github.add_issue_comment(
                issue.number,
                f"Pipeline completed but no code changes were made.\n\n"
                f"Aggregate confidence: {state.aggregate_confidence}"
            )
            return PipelineStatus.SKIPPED

        # Stage and commit
        git.add(exclude_patterns=[".env", "*.env"])
        git.commit(
            f"fix: {issue.title}\n\n"
            f"Fixes #{issue.number}\n\n"
            f"Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
        )

        # Push
        git.push("origin", branch_name, set_upstream=True)
        print(f"[SUCCESS] Pushed to origin/{branch_name}")

        # Create PR
        confidence = state.aggregate_confidence
        labels = []
        if confidence >= 0.8:
            labels.append("high-confidence")
        elif confidence >= 0.6:
            labels.append("medium-confidence")
        else:
            labels.append("low-confidence")

        pr_body = f"""## Summary
Auto-generated fix for issue #{issue.number}

## Confidence
- Aggregate: {confidence}
- Breakdown: {json.dumps(state.confidence_breakdown, indent=2)}

## Test Plan
- [ ] Review the changes
- [ ] Run tests
- [ ] Verify fix addresses the issue

---
Generated with [Claude Code](https://claude.com/claude-code)
"""

        pr = github.create_pr(
            title=f"fix: {issue.title}",
            body=pr_body,
            head=branch_name,
            base=config.base_branch,
            draft=True,
            labels=labels,
        )
        print(f"[SUCCESS] Created PR: {pr.url}")

        # Comment on issue
        github.add_issue_comment(
            issue.number,
            f"Created auto-fix PR: {pr.url}\n\n"
            f"Aggregate confidence: {confidence}\n\n"
            f"Please review before merging."
        )

        return PipelineStatus.SUCCESS

    except (GitError, GitHubError) as e:
        print(f"[ERROR] Failed to create PR: {e}")
        return PipelineStatus.FAILED


def handle_skip(github: GitHubClient, issue, state, run_dir: Path) -> PipelineStatus:
    """Handle skipped pipeline."""
    print(f"[WARNING] Pipeline skipped: {state.failure_reason}")

    # Load triage analysis for detailed comment
    triage_state_file = run_dir / "triage.state.json"
    analysis_summary = ""
    if triage_state_file.exists():
        try:
            with open(triage_state_file) as f:
                triage_data = json.load(f)

            full_analysis = triage_data.get("full_analysis", {})
            summary = full_analysis.get("summary", triage_data.get("summary", ""))
            reasoning = full_analysis.get("reasoning", "")
            risks = full_analysis.get("risks", [])
            suggested_approach = full_analysis.get("suggested_approach", "")
            questions = full_analysis.get("questions_if_unclear", [])

            analysis_summary = f"""
## Analysis Summary

**Summary:** {summary}

**Reasoning:** {reasoning}

**Risks:**
{chr(10).join(f"- {r}" for r in risks) if risks else "- None identified"}

**Suggested Approach:** {suggested_approach}

**Questions for Clarification:**
{chr(10).join(f"- {q}" for q in questions) if questions else "- None"}
"""
        except (json.JSONDecodeError, KeyError):
            pass

    try:
        github.add_issue_comment(
            issue.number,
            f"This issue was analyzed but cannot be auto-fixed.\n\n"
            f"**Classification:** {state.failure_reason}\n"
            f"{analysis_summary}\n"
            f"---\n"
            f"*Manual review is required.*"
        )
    except GitHubError as e:
        print(f"[WARNING] Failed to comment on issue: {e}")

    return PipelineStatus.SKIPPED


def handle_failure(github: GitHubClient, git: GitOps, issue, branch_name: str, state) -> PipelineStatus:
    """Handle failed pipeline."""
    print(f"[ERROR] Pipeline failed: {state.failure_reason}")

    # Clean up branch
    try:
        git.checkout(git._run("config", "--get", "init.defaultBranch", check=False) or "main")
        git.delete_branch(branch_name, force=True)
    except GitError:
        pass

    return PipelineStatus.FAILED


if __name__ == "__main__":
    sys.exit(main())
