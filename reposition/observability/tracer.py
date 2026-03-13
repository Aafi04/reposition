"""Append-only JSONL tracer for Reposition pipeline runs."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path


class RunTracer:
    """Writes one JSON line per agent decision to a trace file.

    The trace is intended to be PR-public, so raw outputs are never persisted —
    only their SHA-256 hashes.
    """

    def __init__(self, run_id: str, trace_path: str) -> None:
        self._run_id = run_id
        self._trace_path = Path(trace_path)
        self._lock = threading.Lock()
        self._trace_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def log(
        self,
        agent_name: str,
        decision: str,
        output: dict,
        token_usage: dict | None = None,
    ) -> None:
        """Append a single-line JSON record to the trace file."""
        output_hash = hashlib.sha256(
            json.dumps(output, sort_keys=True).encode()
        ).hexdigest()

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "agent_name": agent_name,
            "decision": decision,
            "output_hash": output_hash,
            "token_usage": token_usage,
        }

        line = json.dumps(record, separators=(",", ":")) + "\n"

        with self._lock:
            with self._trace_path.open("a", encoding="utf-8") as fh:
                fh.write(line)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        """Parse the trace file and return aggregate statistics."""
        total_agents_run = 0
        total_tokens_used = 0
        packages_attempted = 0
        packages_passed = 0
        packages_failed = 0

        if not self._trace_path.exists():
            return {
                "total_agents_run": total_agents_run,
                "total_tokens_used": total_tokens_used,
                "packages_attempted": packages_attempted,
                "packages_passed": packages_passed,
                "packages_failed": packages_failed,
            }

        with self._trace_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                total_agents_run += 1

                usage = record.get("token_usage")
                if usage:
                    total_tokens_used += usage.get("total_tokens", 0)

                decision = record.get("decision", "")
                if decision == "package_attempted":
                    packages_attempted += 1
                elif decision == "package_passed":
                    packages_passed += 1
                elif decision == "package_failed":
                    packages_failed += 1

        return {
            "total_agents_run": total_agents_run,
            "total_tokens_used": total_tokens_used,
            "packages_attempted": packages_attempted,
            "packages_passed": packages_passed,
            "packages_failed": packages_failed,
        }
