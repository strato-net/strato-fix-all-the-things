"""Git operations."""

import subprocess
from pathlib import Path


class GitError(Exception):
    """Git operation error."""
    pass


class GitOps:
    """Git operations for a repository."""

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path

    def _run(self, *args: str, check: bool = True) -> str:
        """Run git command."""
        cmd = ["git", *args]
        result = subprocess.run(
            cmd,
            cwd=self.repo_path,
            capture_output=True,
            text=True,
        )
        if check and result.returncode != 0:
            raise GitError(f"git {args[0]} failed: {result.stderr}")
        return result.stdout.strip()

    def fetch(self, remote: str = "origin") -> None:
        """Fetch from remote."""
        self._run("fetch", remote)

    def checkout(self, branch: str) -> None:
        """Checkout a branch."""
        self._run("checkout", branch)

    def reset_hard(self, ref: str) -> None:
        """Hard reset to a ref."""
        self._run("reset", "--hard", ref)

    def create_branch(self, branch: str, start_point: str | None = None) -> None:
        """Create and checkout a new branch."""
        if start_point:
            self._run("checkout", "-b", branch, start_point)
        else:
            self._run("checkout", "-b", branch)

    def delete_branch(self, branch: str, force: bool = False) -> None:
        """Delete a branch."""
        flag = "-D" if force else "-d"
        self._run("branch", flag, branch, check=False)

    def delete_remote_branch(self, branch: str, remote: str = "origin") -> None:
        """Delete a remote branch."""
        self._run("push", remote, "--delete", branch, check=False)

    def branch_exists(self, branch: str) -> bool:
        """Check if branch exists locally."""
        result = self._run("branch", "--list", branch, check=False)
        return bool(result)

    def current_branch(self) -> str:
        """Get current branch name."""
        return self._run("branch", "--show-current")

    def is_dirty(self) -> bool:
        """Check if working tree has uncommitted changes."""
        status = self._run("status", "--porcelain")
        return bool(status)

    def add(self, *paths: str, exclude_patterns: list[str] | None = None) -> None:
        """Add files to staging, optionally excluding patterns."""
        if exclude_patterns:
            # Use git add with pathspec magic to exclude
            pathspecs = list(paths) if paths else ["."]
            for pattern in exclude_patterns:
                pathspecs.append(f":!{pattern}")
            self._run("add", *pathspecs)
        else:
            self._run("add", *paths if paths else ["."])

    def commit(self, message: str) -> None:
        """Create a commit."""
        self._run("commit", "-m", message)

    def push(self, remote: str = "origin", branch: str | None = None, set_upstream: bool = False) -> None:
        """Push to remote."""
        args = ["push"]
        if set_upstream:
            args.append("-u")
        args.append(remote)
        if branch:
            args.append(branch)
        self._run(*args)

    def has_changes(self) -> bool:
        """Check if there are staged or unstaged changes."""
        # Check for staged changes
        staged = self._run("diff", "--cached", "--name-only", check=False)
        # Check for unstaged changes
        unstaged = self._run("diff", "--name-only", check=False)
        return bool(staged or unstaged)

    def has_unpushed_commits(self, remote: str = "origin", branch: str | None = None) -> bool:
        """Check if there are commits that haven't been pushed to remote."""
        if not branch:
            branch = self.current_branch()
        # Check if remote branch exists
        remote_ref = f"{remote}/{branch}"
        result = self._run("rev-parse", "--verify", remote_ref, check=False)
        if not result:
            # Remote branch doesn't exist, so we have unpushed commits if we have any commits
            return True
        # Compare local and remote
        ahead = self._run("rev-list", "--count", f"{remote_ref}..HEAD", check=False)
        return int(ahead or 0) > 0

    def sync_to_remote(self, remote: str, branch: str) -> None:
        """Sync local branch to match remote exactly."""
        self.fetch(remote)
        self.checkout(branch)
        self.reset_hard(f"{remote}/{branch}")
