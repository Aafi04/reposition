"""E2B sandbox management for Reposition pipeline runs."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Any

from e2b_code_interpreter import AsyncSandbox

# Module-level registry so sandbox objects can be retrieved by ID.
_sandbox_instances: dict[str, Any] = {}


class SandboxError(Exception):
    """Raised when an E2B sandbox operation fails."""


class E2BSandboxManager:
    """Manages E2B CodeInterpreter sandboxes for isolated code execution."""

    def __init__(self, e2b_api_key: str | None = None) -> None:
        self._api_key = e2b_api_key or os.environ.get("E2B_API_KEY", "")
        self._sandbox_timeout_seconds = int(os.environ.get("REPOSITION_SANDBOX_TIMEOUT_SECONDS", "3600"))
        if not self._api_key:
            raise SandboxError(
                "E2B API key is required. Pass e2b_api_key or set the "
                "E2B_API_KEY environment variable."
            )

    # ------------------------------------------------------------------
    # Sandbox lifecycle
    # ------------------------------------------------------------------

    async def create_sandbox(
        self,
        repo_path: str,
        excluded_files: list[str],
    ) -> str:
        """Create a sandbox, upload the repo, install dependencies, and return sandbox_id."""
        try:
            sbx = await AsyncSandbox.create(
                api_key=self._api_key,
                timeout=self._sandbox_timeout_seconds,
            )
        except Exception as exc:
            raise SandboxError(f"Failed to create sandbox: {exc}") from exc

        # Keep long-running coding/validation loops alive on larger repositories.
        try:
            await sbx.set_timeout(self._sandbox_timeout_seconds)
        except Exception:
            pass

        sandbox_id = sbx.sandbox_id
        _sandbox_instances[sandbox_id] = sbx

        # Upload repo files
        excluded = set(excluded_files)
        repo = Path(repo_path)

        try:
            for file_path in repo.rglob("*"):
                if not file_path.is_file():
                    continue
                rel = file_path.relative_to(repo).as_posix()

                # Skip .git directory and excluded files
                if rel.startswith(".git/") or rel == ".git" or rel in excluded:
                    continue

                remote_path = str(PurePosixPath("/home/user/repo") / rel)

                # Ensure parent directory exists
                parent = str(PurePosixPath(remote_path).parent)
                await sbx.files.make_dir(parent)

                content = file_path.read_bytes()
                await sbx.files.write(remote_path, content)
        except Exception as exc:
            raise SandboxError(f"Failed to upload repo files: {exc}") from exc

        # Bootstrap git metadata in sandbox so validator/PR steps can commit and diff.
        try:
            await sbx.commands.run("mkdir -p /home/user/repo")
            await sbx.commands.run("cd /home/user/repo && git init")
            await sbx.commands.run(
                "cd /home/user/repo && git config user.email 'reposition-bot@local'"
            )
            await sbx.commands.run(
                "cd /home/user/repo && git config user.name 'reposition-bot'"
            )
            await sbx.commands.run("cd /home/user/repo && git add -A")
            await sbx.commands.run(
                "cd /home/user/repo && git commit -m 'reposition: baseline' || true"
            )
        except Exception as exc:
            raise SandboxError(f"Failed to initialize git metadata in sandbox: {exc}") from exc

        # Install dependencies
        try:
            requirements = PurePosixPath("/home/user/repo/requirements.txt")
            pyproject = PurePosixPath("/home/user/repo/pyproject.toml")

            result = await sbx.commands.run(
                "test -f /home/user/repo/requirements.txt && echo EXISTS || echo MISSING",
            )
            has_requirements = "EXISTS" in (result.stdout or "")

            result = await sbx.commands.run(
                "test -f /home/user/repo/pyproject.toml && echo EXISTS || echo MISSING",
            )
            has_pyproject = "EXISTS" in (result.stdout or "")

            if has_requirements:
                await sbx.commands.run(
                    "pip install -r /home/user/repo/requirements.txt",
                    timeout=300,
                )
            elif has_pyproject:
                await sbx.commands.run(
                    "pip install -e /home/user/repo",
                    timeout=300,
                )
        except Exception as exc:
            raise SandboxError(f"Failed to install dependencies: {exc}") from exc

        return sandbox_id

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    async def run_command(
        self,
        sandbox_id: str,
        command: str,
        timeout: int = 300,
    ) -> dict:
        """Run a shell command and return {stdout, stderr, exit_code}."""
        sbx = self._get_sandbox(sandbox_id)
        try:
            result = await sbx.commands.run(command, timeout=timeout)
            return {
                "stdout": result.stdout or "",
                "stderr": result.stderr or "",
                "exit_code": result.exit_code,
            }
        except Exception as exc:
            raise SandboxError(
                f"Command execution failed in sandbox {sandbox_id}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Patch operations
    # ------------------------------------------------------------------

    async def apply_patch(
        self,
        sandbox_id: str,
        patch_content: str,
        dry_run: bool = False,
    ) -> dict:
        """Apply a unified diff patch inside the sandbox."""
        sbx = self._get_sandbox(sandbox_id)
        patch_path = "/tmp/patch.diff"

        try:
            await sbx.files.write(patch_path, patch_content)

            if dry_run:
                cmd = f"cd /home/user/repo && patch --dry-run -p1 < {patch_path}"
            else:
                cmd = f"cd /home/user/repo && patch -p1 < {patch_path}"

            result = await sbx.commands.run(cmd)
            success = result.exit_code == 0
            output = (result.stdout or "") + (result.stderr or "")

            return {"success": success, "output": output}
        except Exception as exc:
            raise SandboxError(
                f"Patch operation failed in sandbox {sandbox_id}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    async def write_file(
        self,
        sandbox_id: str,
        path: str,
        content: str,
    ) -> bool:
        """Write content to a file inside the sandbox. Returns True on success."""
        sbx = self._get_sandbox(sandbox_id)
        try:
            await sbx.files.write(path, content)
            return True
        except Exception as exc:
            raise SandboxError(
                f"Failed to write file {path} in sandbox {sandbox_id}: {exc}"
            ) from exc

    async def read_file(self, sandbox_id: str, path: str) -> str:
        """Read a file from the sandbox and return its content."""
        sbx = self._get_sandbox(sandbox_id)
        try:
            content = await sbx.files.read(path)
            return content
        except Exception as exc:
            raise SandboxError(
                f"Failed to read file {path} in sandbox {sandbox_id}: {exc}"
            ) from exc

    async def close_sandbox(self, sandbox_id: str) -> None:
        """Shut down a sandbox and remove it from the registry."""
        sbx = self._get_sandbox(sandbox_id)
        try:
            await sbx.kill()
        except Exception as exc:
            raise SandboxError(
                f"Failed to close sandbox {sandbox_id}: {exc}"
            ) from exc
        finally:
            _sandbox_instances.pop(sandbox_id, None)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_sandbox(sandbox_id: str) -> Any:
        """Retrieve a live sandbox instance or raise."""
        sbx = _sandbox_instances.get(sandbox_id)
        if sbx is None:
            raise SandboxError(
                f"Sandbox {sandbox_id} not found. It may have been closed or "
                "was never created by this manager."
            )
        return sbx
