"""Turn approved changes into a Compose override file, show it as a diff, apply it and undo it.

A fix is never written into app/docker-compose.yml. It lives in its own override file and is applied
with `docker compose -f docker-compose.yml -f override.yml up -d --no-deps <services>`, so undoing
it is the same command without the override: Compose sees the config hash change and recreates the
container from the original definition. Container names do not change, so target.yaml still holds.
"""

from __future__ import annotations

import copy
import difflib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

from safety.limits import REPO_ROOT

from .models import ConfigChange

COMPOSE_FILE = REPO_ROOT / "app" / "docker-compose.yml"


def load_services(compose_file: Path = COMPOSE_FILE) -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
    return raw.get("services") or {}


def override_doc(changes: list[ConfigChange]) -> dict[str, Any]:
    services: dict[str, dict[str, Any]] = {}
    for change in changes:
        svc = services.setdefault(change.service, {})
        if change.setting == "environment":
            key, _, value = change.value.partition("=")
            svc.setdefault("environment", {})[key] = value
        else:
            svc[change.setting] = change.value
    return {"services": services}


def render_override(changes: list[ConfigChange], proposal_id: str) -> str:
    header = (
        f"# Remediation {proposal_id}: generated from proposal.json - do not edit by hand.\n"
        f"# Editing this file voids the review approval.\n"
    )
    return header + yaml.safe_dump(override_doc(changes), sort_keys=True)


def merged_services(
    services: dict[str, dict[str, Any]], changes: list[ConfigChange]
) -> dict[str, dict[str, Any]]:
    merged = copy.deepcopy(services)
    for name, patch in override_doc(changes)["services"].items():
        target = merged.setdefault(name, {})
        for key, value in patch.items():
            if key == "environment":
                target.setdefault("environment", {}).update(value)
            else:
                target[key] = value
    return merged


def config_diff(services: dict[str, dict[str, Any]], changes: list[ConfigChange]) -> str:
    """Unified diff of each touched service, before vs after - what the reviewer approves."""
    after = merged_services(services, changes)
    chunks = []
    for name in sorted({c.service for c in changes}):
        old = yaml.safe_dump({name: services.get(name, {})}, sort_keys=True).splitlines(keepends=True)
        new = yaml.safe_dump({name: after[name]}, sort_keys=True).splitlines(keepends=True)
        chunks.append("".join(difflib.unified_diff(old, new, f"original/{name}", f"proposed/{name}")))
    return "\n".join(chunks)


class Compose(Protocol):
    def apply(self, override: Path, services: list[str]) -> None: ...
    def restore(self, services: list[str]) -> None: ...


class ComposeError(RuntimeError):
    pass


class ComposeCli:
    def __init__(
        self, project: str | None, compose_file: Path = COMPOSE_FILE, timeout: float = 180.0
    ) -> None:
        self.project = project
        self.compose_file = compose_file
        self.timeout = timeout

    def _up(self, files: list[Path], services: list[str]) -> None:
        cmd = ["docker", "compose"]
        if self.project:
            cmd += ["-p", self.project]
        for f in files:
            cmd += ["-f", str(f)]
        cmd += ["up", "-d", "--no-deps", *services]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, check=False)
        if proc.returncode != 0:
            raise ComposeError(f"{' '.join(cmd)} failed: {(proc.stderr or proc.stdout).strip()[:500]}")

    def apply(self, override: Path, services: list[str]) -> None:
        self._up([self.compose_file, override], services)

    def restore(self, services: list[str]) -> None:
        self._up([self.compose_file], services)


class FixJournal:
    """Written before a fix is applied, cleared after it is undone - like safety.journal."""

    def __init__(self, results_dir: Path) -> None:
        self.path = Path(results_dir) / "remediation" / "active_fix.json"

    def write(self, proposal_id: str, services: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "proposal_id": proposal_id,
                    "services": services,
                    "applied_at": datetime.now().astimezone().isoformat(),
                }
            ),
            encoding="utf-8",
        )

    def read(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
