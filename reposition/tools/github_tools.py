"""GitHub API wrapper with rate-limit-aware backoff."""

from __future__ import annotations

import os
import time
from typing import Any

from github import Github, GithubException, InputGitTreeElement, RateLimitExceededException

from reposition.sandbox import E2BSandboxManager


class GitHubClient:
    """Thin wrapper around PyGithub with automatic backoff and rate-limit handling."""

    _MAX_RETRIES = 3
    _BACKOFF_DELAYS = (1, 2, 4)
    _RATE_LIMIT_FLOOR = 10

    def __init__(
        self,
        github_token: str | None = None,
        repo_full_name: str = "",
    ) -> None:
        token = github_token or os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise ValueError(
                "GitHub token is required. Pass github_token or set GITHUB_TOKEN."
            )
        self._gh = Github(token)
        self._repo = self._gh.get_repo(repo_full_name) if repo_full_name else None

    # ------------------------------------------------------------------
    # Backoff helper
    # ------------------------------------------------------------------

    def _with_backoff(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Call *fn* with retry + rate-limit awareness."""
        last_exc: Exception | None = None
        for attempt in range(self._MAX_RETRIES):
            # Pre-flight rate-limit check
            try:
                rate = self._repo.get_rate_limit().core if self._repo else self._gh.get_rate_limit().core
                if rate.remaining < self._RATE_LIMIT_FLOOR:
                    sleep_seconds = max(0, (rate.reset - __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    )).total_seconds()) + 1
                    time.sleep(min(sleep_seconds, 120))
            except Exception:
                pass

            try:
                return fn(*args, **kwargs)
            except (GithubException, RateLimitExceededException) as exc:
                last_exc = exc
                if attempt < self._MAX_RETRIES - 1:
                    time.sleep(self._BACKOFF_DELAYS[attempt])
        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def find_existing_reposition_pr(self) -> dict | None:
        """Return the first open PR whose head branch starts with ``reposition/``."""
        assert self._repo is not None

        def _find() -> dict | None:
            for pr in self._repo.get_pulls(state="open"):
                if pr.head.ref.startswith("reposition/"):
                    return {
                        "number": pr.number,
                        "html_url": pr.html_url,
                        "head_branch": pr.head.ref,
                    }
            return None

        return self._with_backoff(_find)

    def create_branch(self, branch_name: str, base_branch: str) -> bool:
        """Create *branch_name* from the HEAD of *base_branch*.

        Returns ``True`` on success. Existing branch is treated as success.
        """
        assert self._repo is not None

        def _create() -> bool:
            base_ref = self._repo.get_branch(base_branch)
            try:
                self._repo.create_git_ref(
                    ref=f"refs/heads/{branch_name}",
                    sha=base_ref.commit.sha,
                )
                return True
            except GithubException as exc:
                if exc.status == 422:  # already exists
                    return True
                raise

        return self._with_backoff(_create)

    async def push_files_from_sandbox(
        self,
        sandbox_manager: E2BSandboxManager,
        sandbox_id: str,
        branch_name: str,
        commit_message: str,
    ) -> bool:
        """Push changed files from the sandbox to the GitHub branch.

        Uses the Git Trees API to create one atomic commit for all modified files.
        """
        assert self._repo is not None

        async def _read_sandbox_files() -> tuple[list[str], dict[str, str]]:
            diff_result = await sandbox_manager.run_command(
                sandbox_id,
                (
                    "cd /home/user/repo && "
                    "BASE=$(git rev-list --max-parents=0 HEAD | tail -n 1) && "
                    "git diff --name-only --diff-filter=ACMRT ${BASE} HEAD"
                ),
            )
            files = [
                f.strip()
                for f in diff_result["stdout"].splitlines()
                if f.strip()
            ]
            files = sorted(set(files))
            contents: dict[str, str] = {}
            for f in files:
                remote = f"/home/user/repo/{f}"
                contents[f] = await sandbox_manager.read_file(sandbox_id, remote)
            return files, contents

        files, contents = await _read_sandbox_files()

        def _push() -> bool:
            if not files:
                raise RuntimeError(
                    "No modified files detected in sandbox diff; refusing empty branch push."
                )

            ref = self._repo.get_git_ref(f"heads/{branch_name}")
            parent_commit = self._repo.get_git_commit(ref.object.sha)
            base_tree = parent_commit.tree

            elements: list[InputGitTreeElement] = []
            for file_path in files:
                content = contents[file_path]
                blob = self._repo.create_git_blob(content, "utf-8")
                elements.append(
                    InputGitTreeElement(
                        path=file_path,
                        mode="100644",
                        type="blob",
                        sha=blob.sha,
                    )
                )

            new_tree = self._repo.create_git_tree(elements, base_tree)
            new_commit = self._repo.create_git_commit(
                commit_message,
                new_tree,
                [parent_commit],
            )
            ref.edit(new_commit.sha)
            return True

        return self._with_backoff(_push)

    def create_pull_request(
        self,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
    ) -> dict:
        """Create a PR and return ``{number, html_url}``."""
        assert self._repo is not None
        try:
            pr = self._with_backoff(
                self._repo.create_pull,
                title=title,
                body=body,
                head=head,
                base=base,
                draft=draft,
            )
            return {"number": pr.number, "html_url": pr.html_url}
        except GithubException as exc:
            message = str(getattr(exc, "data", "")).lower()
            if exc.status == 422 and "already exists" in message:
                existing = self.find_existing_reposition_pr()
                if existing:
                    return {
                        "number": existing["number"],
                        "html_url": existing["html_url"],
                        "already_existed": True,
                    }
            raise

    def add_pr_comment(self, pr_number: int, body: str) -> bool:
        """Add a comment to PR *pr_number*."""
        assert self._repo is not None

        def _comment() -> bool:
            pr = self._repo.get_pull(pr_number)
            pr.create_issue_comment(body)
            return True

        return self._with_backoff(_comment)

    def get_diff_stats(self, head_branch: str, base_branch: str) -> dict:
        """Compare two branches and return diff statistics."""
        assert self._repo is not None

        def _compare() -> dict:
            comparison = self._repo.compare(base_branch, head_branch)
            lines_added = sum(f.additions for f in comparison.files)
            lines_deleted = sum(f.deletions for f in comparison.files)
            return {
                "files_changed": len(comparison.files),
                "lines_added": lines_added,
                "lines_deleted": lines_deleted,
            }

        return self._with_backoff(_compare)
