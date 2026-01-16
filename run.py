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


def cleanup_git_state(git: GitOps, base_branch: str, feature_branch: str) -> None:
    """Clean up git state - discard changes and return to base branch."""
    try:
        # Discard any uncommitted changes
        git._run("checkout", "--", ".", check=False)
        git._run("clean", "-fd", check=False)
        # Return to base branch
        git._run("checkout", base_branch, check=False)
        # Delete feature branch
        git.delete_branch(feature_branch, force=True)
    except Exception:
        pass  # Best effort cleanup


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
        # Ensure clean state before starting
        if git.is_dirty():
            print("[ERROR] Working tree has uncommitted changes. Please commit or stash them.")
            return PipelineStatus.FAILED

        # Fetch latest and hard reset to ensure we're at latest base branch
        print(f"[INFO] Fetching latest from origin...")
        git.fetch("origin")

        # Force checkout to base branch (in case we're on a different branch)
        git._run("checkout", "-f", config.base_branch, check=True)

        # Hard reset to match remote exactly (discards any local commits)
        git.reset_hard(f"origin/{config.base_branch}")
        print(f"[SUCCESS] Reset to origin/{config.base_branch}")

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

    # Run pipeline with cleanup on any failure
    try:
        print(f"[INFO] Starting multi-agent pipeline...")
        pipeline = Pipeline(config, issue, run_dir)
        state = pipeline.run()

        # Handle results
        if state.status == PipelineStatus.SUCCESS:
            return handle_success(config, github, git, issue, branch_name, state, run_dir)
        elif state.status == PipelineStatus.SKIPPED:
            cleanup_git_state(git, config.base_branch, branch_name)
            return handle_skip(github, issue, state, run_dir)
        else:
            return handle_failure(github, git, issue, branch_name, state, config.base_branch)
    except Exception as e:
        # Unexpected error - clean up and re-raise
        print(f"[ERROR] Unexpected error: {e}")
        cleanup_git_state(git, config.base_branch, branch_name)
        raise


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
        # Commit any uncommitted changes (fix agent may or may not have committed)
        if git.has_changes():
            git.add(exclude_patterns=[".env", "*.env"])
            git.commit(
                f"fix: {issue.title}\n\n"
                f"Fixes #{issue.number}\n\n"
                f"Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
            )

        # Check if there are commits to push
        if not git.has_unpushed_commits("origin", branch_name):
            print("[WARNING] No commits to push")
            github.add_issue_comment(
                issue.number,
                f"Pipeline completed but no code changes were made.\n\n"
                f"Aggregate confidence: {state.aggregate_confidence}"
            )
            return PipelineStatus.SKIPPED

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

        # Build issue comment with fix summary
        fix_summary = ""
        files_changed = []

        # Load fix state to get details
        fix_state_file = run_dir / "fix.state.json"
        if fix_state_file.exists():
            try:
                with open(fix_state_file) as f:
                    fix_data = json.load(f)
                files_changed = fix_data.get("files_changed", [])
                full_result = fix_data.get("full_result", {})
                caveats = full_result.get("caveats", [])
                testing_notes = full_result.get("testing_notes", [])

                if caveats:
                    fix_summary += "\n**Caveats:**\n"
                    for caveat in caveats[:3]:  # Limit to 3
                        fix_summary += f"- {caveat}\n"

                if testing_notes:
                    fix_summary += "\n**Testing notes:**\n"
                    for note in testing_notes[:3]:  # Limit to 3
                        fix_summary += f"- {note}\n"
            except (json.JSONDecodeError, KeyError):
                pass

        # Load research state to get root cause
        root_cause = ""
        research_state_file = run_dir / "research.state.json"
        if research_state_file.exists():
            try:
                with open(research_state_file) as f:
                    research_data = json.load(f)
                rc = research_data.get("root_cause", {})
                if isinstance(rc, dict):
                    root_cause = rc.get("description", "")
                else:
                    root_cause = str(rc) if rc else ""
            except (json.JSONDecodeError, KeyError):
                pass

        # Build the comment
        files_list = ", ".join(f"`{f}`" for f in files_changed[:5]) if files_changed else "See PR"
        comment = f"""🤖 **Automated Fix Created**

**PR:** {pr.url}

**Files changed:** {files_list}
"""
        if root_cause:
            comment += f"\n**Root cause:** {root_cause}\n"

        if fix_summary:
            comment += fix_summary

        comment += f"""
**Confidence:** {confidence:.0%}

Please review the PR before merging.

---
*Generated by [STRATO Fix All The Things](https://github.com/strato-net/strato-fix-all-the-things)*"""

        github.add_issue_comment(issue.number, comment)

        return PipelineStatus.SUCCESS

    except (GitError, GitHubError) as e:
        print(f"[ERROR] Failed to create PR: {e}")
        return PipelineStatus.FAILED


def handle_fix_no_changes(github: GitHubClient, issue, run_dir: Path) -> PipelineStatus:
    """Handle case where fix agent completed but made no changes."""
    print(f"[WARNING] Fix agent made no code changes")

    # Load research and triage data for context
    research_summary = ""
    triage_summary = ""

    triage_state_file = run_dir / "triage.state.json"
    if triage_state_file.exists():
        try:
            with open(triage_state_file) as f:
                triage_data = json.load(f)
            full_analysis = triage_data.get("full_analysis", {})
            triage_summary = full_analysis.get("summary", triage_data.get("summary", ""))
        except (json.JSONDecodeError, KeyError):
            pass

    research_state_file = run_dir / "research.state.json"
    if research_state_file.exists():
        try:
            with open(research_state_file) as f:
                research_data = json.load(f)
            research_summary = research_data.get("summary", "")
        except (json.JSONDecodeError, KeyError):
            pass

    # Build informative comment
    comment_parts = [
        "🤖 **Auto-Fix Analysis Complete**\n",
        "The issue was analyzed and deemed fixable, but the fix agent was unable to make any code changes.\n",
    ]

    if triage_summary:
        comment_parts.append(f"\n## Triage Analysis\n{triage_summary}\n")

    if research_summary:
        comment_parts.append(f"\n## Research Findings\n{research_summary}\n")

    comment_parts.append(
        "\n## Next Steps\n"
        "- A human developer should review this issue\n"
        "- The automated analysis above may provide useful context\n"
        "- Consider if the issue requires architectural changes beyond simple fixes\n"
    )

    comment_parts.append(
        "\n---\n"
        "*Generated by [STRATO Fix All The Things](https://github.com/strato-net/strato-fix-all-the-things)*"
    )

    try:
        github.add_issue_comment(issue.number, "".join(comment_parts))
    except GitHubError as e:
        print(f"[WARNING] Failed to comment on issue: {e}")

    return PipelineStatus.SKIPPED


def handle_skip(github: GitHubClient, issue, state, run_dir: Path) -> PipelineStatus:
    """Handle skipped pipeline."""
    print(f"[WARNING] Pipeline skipped: {state.failure_reason}")

    # Check if skip happened at fix stage (no changes made)
    fix_state_file = run_dir / "fix.state.json"
    if fix_state_file.exists() and "no changes" in state.failure_reason.lower():
        return handle_fix_no_changes(github, issue, run_dir)

    # Load triage analysis for detailed comment
    triage_state_file = run_dir / "triage.state.json"
    classification = ""
    analysis_summary = ""

    if triage_state_file.exists():
        try:
            with open(triage_state_file) as f:
                triage_data = json.load(f)

            classification = triage_data.get("classification", "")
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
"""
            # Add classification-specific sections
            if classification == "NEEDS_HUMAN":
                if risks:
                    analysis_summary += f"""
**Risks:**
{chr(10).join(f"- {r}" for r in risks)}
"""
                if suggested_approach:
                    analysis_summary += f"""
**Suggested Approach:** {suggested_approach}
"""
                if questions:
                    analysis_summary += f"""
**Questions for Clarification:**
{chr(10).join(f"- {q}" for q in questions)}
"""

            elif classification == "NEEDS_CLARIFICATION":
                if questions:
                    analysis_summary += f"""
**Please provide clarification on:**
{chr(10).join(f"- {q}" for q in questions)}
"""

            elif classification == "OUT_OF_SCOPE":
                analysis_summary += """
**Why this is out of scope:** This issue does not appear to be a bug or configuration issue that can be addressed through code changes. It may be a feature request, documentation issue, or external dependency problem.
"""

            elif classification == "DUPLICATE":
                analysis_summary += """
**Note:** This issue appears to be a duplicate. Please check for related issues that may already address this problem.
"""

        except (json.JSONDecodeError, KeyError):
            pass

    # Build classification-specific intro message
    intro_messages = {
        "NEEDS_HUMAN": "This issue requires human review due to its complexity or risk level.",
        "NEEDS_CLARIFICATION": "This issue needs more information before it can be addressed.",
        "OUT_OF_SCOPE": "This issue is outside the scope of automated fixes.",
        "DUPLICATE": "This issue appears to be a duplicate of an existing issue.",
    }
    intro = intro_messages.get(classification, "This issue was analyzed but cannot be auto-fixed.")

    try:
        github.add_issue_comment(
            issue.number,
            f"🤖 **Auto-Fix Analysis Complete**\n\n"
            f"{intro}\n\n"
            f"**Classification:** `{classification}`\n"
            f"{analysis_summary}\n"
            f"---\n"
            f"*Generated by [STRATO Fix All The Things](https://github.com/strato-net/strato-fix-all-the-things)*"
        )
    except GitHubError as e:
        print(f"[WARNING] Failed to comment on issue: {e}")

    return PipelineStatus.SKIPPED


def handle_failure(github: GitHubClient, git: GitOps, issue, branch_name: str, state, base_branch: str) -> PipelineStatus:
    """Handle failed or blocked pipeline."""
    print(f"[ERROR] Pipeline failed: {state.failure_reason}")
    cleanup_git_state(git, base_branch, branch_name)
    return PipelineStatus.FAILED


if __name__ == "__main__":
    sys.exit(main())
