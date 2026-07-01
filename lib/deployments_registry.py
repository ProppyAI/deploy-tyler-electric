"""Loader and path resolver for HARNESS deployments registry."""

import json
import os
from pathlib import Path
from typing import Optional


REGISTRY_REL_PATH = "deployments.json"
ENV_OVERRIDE_KEY = "HARNESS_DEPLOY_PATH"


class DeploymentNotFoundError(Exception):
    """Raised when no deploy_repo_path resolves for a client."""


def _harness_root() -> Path:
    """Return HARNESS repo root (the directory containing deployments.json)."""
    return Path(__file__).resolve().parent.parent


class RegistryError(Exception):
    """Raised when deployments.json exists but is unreadable or malformed."""


def _load_registry(registry_path: Optional[str] = None) -> dict:
    path = Path(registry_path) if registry_path else _harness_root() / REGISTRY_REL_PATH
    if not path.is_file():
        return {"version": 1, "deployments": {}}
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise RegistryError(f"deployments.json at {path} is malformed: {e}") from e


def list_clients(registry_path: Optional[str] = None) -> list[str]:
    """Return sorted list of registered client names."""
    registry = _load_registry(registry_path)
    return sorted(registry.get("deployments", {}).keys())


def _is_valid_deploy_dir(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / ".env.production").is_file()
        and (path / "harness.json").is_file()
    )


def resolve_deploy_path(client: str, registry_path: Optional[str] = None) -> str:
    """Resolve a deploy_repo_path for `client`.

    Resolution order:
    1. HARNESS_DEPLOY_PATH env var (if set and valid).
    2. registry lookup.
    3. Convention fallback: <HARNESS>/../deploy-<client>/.

    Raises DeploymentNotFoundError if none yield a valid deploy dir.
    """
    paths_checked = []

    env_override = os.environ.get(ENV_OVERRIDE_KEY)
    if env_override:
        candidate = Path(env_override).expanduser()
        paths_checked.append(f"{ENV_OVERRIDE_KEY}={candidate}")
        if _is_valid_deploy_dir(candidate):
            return str(candidate)

    registry = _load_registry(registry_path)
    entry = registry.get("deployments", {}).get(client)
    if entry:
        candidate = Path(entry["deploy_repo_path"]).expanduser()
        paths_checked.append(f"registry={candidate}")
        if _is_valid_deploy_dir(candidate):
            return str(candidate)

    allowed_root = _harness_root().parent.resolve()
    convention = (allowed_root / f"deploy-{client}").resolve()
    paths_checked.append(f"convention={convention}")
    # Containment check: convention must be a direct child of allowed_root
    # (defends against client='../escape' or client='..' traversal)
    if convention.parent == allowed_root and _is_valid_deploy_dir(convention):
        return str(convention)

    raise DeploymentNotFoundError(
        f"no valid deploy repo for client {client!r}. paths checked: "
        + "; ".join(paths_checked)
    )
