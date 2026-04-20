"""Fix application engine — translates agent fix instructions into real file mutations.

Two strategies (tried in order):
  A) Structured JSON — agent emits {"file": ..., "action": "replace|delete_lines|write", ...}
  B) Heuristic       — keyword-based dispatch to pre-built fix functions

Every successful fix is committed to git.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class FixResult:
    success: bool
    files_modified: List[str]
    commit_sha: str = ""
    strategy_used: str = ""
    error: str = ""
    description: str = ""


# ── Git helpers ────────────────────────────────────────────────────────────

def _git_cmd(workspace: str, args: List[str]) -> tuple[int, str, str]:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "CI Agent", "GIT_AUTHOR_EMAIL": "agent@ci.local",
        "GIT_COMMITTER_NAME": "CI Agent", "GIT_COMMITTER_EMAIL": "agent@ci.local",
    })
    result = subprocess.run(
        ["git"] + args, cwd=workspace, env=env,
        capture_output=True, text=True, timeout=30,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _head_sha(workspace: str) -> str:
    code, stdout, _ = _git_cmd(workspace, ["rev-parse", "HEAD"])
    return stdout if code == 0 else ""


def _commit_fix(workspace: str, message: str) -> str:
    _git_cmd(workspace, ["add", "."])
    _git_cmd(workspace, ["commit", "-m", message])
    return _head_sha(workspace)


# ── Public API ─────────────────────────────────────────────────────────────

def apply_fix(workspace: str, fix_text: str, target: str = "") -> FixResult:
    """Apply a fix to the workspace. Tries JSON first, falls back to heuristics."""
    result = _try_structured_fix(workspace, fix_text)
    if result is not None:
        return result
    return _apply_heuristic_fix(workspace, fix_text, target)


# ── Strategy A: Structured JSON ────────────────────────────────────────────

def _try_structured_fix(workspace: str, fix_text: str) -> Optional[FixResult]:
    """Parse a JSON fix object (or list) and apply it to real files."""
    json_str = fix_text.strip()
    list_match = re.search(r'\[[^[\]]*\{[^{}]+\}[^[\]]*\]', json_str, re.DOTALL)
    obj_match = re.search(r'\{[^{}]+\}', json_str, re.DOTALL)

    if list_match:
        json_str = list_match.group(0)
    elif obj_match:
        json_str = obj_match.group(0)
    else:
        return None

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        return None

    fixes = [parsed] if isinstance(parsed, dict) else (parsed if isinstance(parsed, list) else None)
    if fixes is None:
        return None

    modified: List[str] = []
    last_obj = {}
    for fix_obj in fixes:
        if not isinstance(fix_obj, dict) or "file" not in fix_obj:
            continue
        last_obj = fix_obj
        filepath = os.path.join(workspace, fix_obj["file"])
        action = fix_obj.get("action", "replace")
        try:
            if action == "replace":
                old, new = fix_obj.get("old", ""), fix_obj.get("new", "")
                if not old:
                    continue
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                if old not in content:
                    continue
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content.replace(old, new))
                modified.append(fix_obj["file"])

            elif action == "delete_lines":
                pattern = fix_obj.get("pattern", fix_obj.get("old", ""))
                if not pattern:
                    continue
                with open(filepath, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                with open(filepath, "w", encoding="utf-8") as f:
                    f.writelines(l for l in lines if pattern not in l)
                modified.append(fix_obj["file"])

            elif action == "write":
                content = fix_obj.get("new", fix_obj.get("content", ""))
                if not content:
                    continue
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                modified.append(fix_obj["file"])

        except OSError as e:
            return FixResult(success=False, files_modified=[], strategy_used="structured_json", error=str(e))

    if not modified:
        return None

    sha = _commit_fix(workspace, f"agent fix: {last_obj.get('action', 'update')} {', '.join(modified)}"[:72])
    return FixResult(
        success=True, files_modified=modified, commit_sha=sha,
        strategy_used="structured_json",
        description=f"Applied structured fix to {', '.join(modified)}",
    )


# ── Strategy B: Heuristic keyword dispatch ─────────────────────────────────

def _apply_heuristic_fix(workspace: str, fix_text: str, target: str = "") -> FixResult:
    fix_lower = fix_text.lower()
    modified: List[str] = []
    description = ""

    if any(kw in fix_lower for kw in ["merge conflict", "conflict marker", "resolve conflict", "sync branch"]):
        modified = _fix_merge_conflict(workspace)
        description = "Resolved merge conflict markers"

    elif any(kw in fix_lower for kw in ["pin", "dependency", "requests", "urllib3", "compatible", "version"]):
        modified = _fix_dependency_conflict(workspace)
        description = "Fixed dependency version pins"

    elif any(kw in fix_lower for kw in ["flaky", "retry", "test isolation", "timing", "intermittent"]):
        modified = _fix_flaky_test(workspace)
        description = "Removed flaky timing-sensitive test"

    elif any(kw in fix_lower for kw in ["permission", "network", "external", "compose", "volume"]):
        modified = _fix_missing_permission(workspace)
        description = "Fixed docker-compose network/volume configuration"

    elif any(kw in fix_lower for kw in ["dockerfile", "reorder", "install order", "layer", "copy before"]):
        modified = _fix_docker_order(workspace)
        description = "Fixed Dockerfile instruction order"

    elif any(kw in fix_lower for kw in ["secret", "credential", "api_key", "hardcoded", "exposed", "scan"]):
        modified = _fix_secret_exposure(workspace)
        description = "Removed hardcoded secrets"

    if not modified:
        return FixResult(
            success=False, files_modified=[], strategy_used="heuristic",
            error="Could not determine how to apply fix from text",
            description=fix_text[:100],
        )

    sha = _commit_fix(workspace, f"agent fix: {description}"[:72])
    return FixResult(success=True, files_modified=modified, commit_sha=sha,
                     strategy_used="heuristic", description=description)


# ── Per-fault fix implementations ──────────────────────────────────────────

def _fix_merge_conflict(workspace: str) -> List[str]:
    modified = []
    for root, _dirs, files in os.walk(workspace):
        if ".git" in root:
            continue
        for fname in files:
            if not fname.endswith((".py", ".yml", ".yaml", ".json", ".txt")):
                continue
            filepath = os.path.join(root, fname)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                if "<<<<<<< " not in content:
                    continue
                cleaned = _resolve_conflict_markers(content)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(cleaned)
                modified.append(os.path.relpath(filepath, workspace))
            except OSError:
                continue
    return modified


def _resolve_conflict_markers(content: str, keep: str = "head") -> str:
    """Strip git conflict markers, keeping HEAD version by default."""
    result, in_conflict, in_theirs = [], False, False
    for line in content.split("\n"):
        if line.startswith("<<<<<<< "):
            in_conflict, in_theirs = True, False
        elif line.startswith("======="):
            in_theirs = True
        elif line.startswith(">>>>>>> "):
            in_conflict, in_theirs = False, False
        elif in_conflict:
            if (keep == "head" and not in_theirs) or (keep == "theirs" and in_theirs):
                result.append(line)
        else:
            result.append(line)
    return "\n".join(result)


def _fix_dependency_conflict(workspace: str) -> List[str]:
    path = os.path.join(workspace, "services", "api", "requirements.txt")
    if not os.path.exists(path):
        return []
    with open(path, "w", encoding="utf-8") as f:
        f.write("flask>=3.0.0\nrequests>=2.31.0\nurllib3>=2.0.0\ngunicorn>=21.2.0\npytest>=8.0.0\n")
    return ["services/api/requirements.txt"]


def _fix_docker_order(workspace: str) -> List[str]:
    path = os.path.join(workspace, "Dockerfile")
    if not os.path.exists(path):
        return []
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent("""\
            FROM python:3.11-slim

            WORKDIR /app

            # Install uv
            COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

            # Install dependencies first for layer caching
            COPY services/api/requirements.txt /app/requirements.txt
            RUN uv pip install --system --no-cache -r requirements.txt

            # Copy application code
            COPY . /app/

            EXPOSE 5000

            CMD ["python", "-m", "services.api.app"]
        """))
    return ["Dockerfile"]


def _fix_flaky_test(workspace: str) -> List[str]:
    path = os.path.join(workspace, "tests", "test_api.py")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    cleaned = re.sub(r'\ndef test_response_time_health\(.*?\n(?=\ndef |\Z)', '\n', content, flags=re.DOTALL)
    if cleaned == content:
        lines, out, skip = content.split("\n"), [], False
        for line in lines:
            if "def test_response_time" in line:
                skip = True
                continue
            if skip and line.strip() and not line[0].isspace():
                skip = False
            if not skip:
                out.append(line)
        cleaned = "\n".join(out)
    with open(path, "w", encoding="utf-8") as f:
        f.write(cleaned)
    return ["tests/test_api.py"]


def _fix_missing_permission(workspace: str) -> List[str]:
    path = os.path.join(workspace, "docker-compose.yml")
    if not os.path.exists(path):
        return []
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent("""\
            version: "3.8"

            services:
              api:
                build:
                  context: .
                  dockerfile: Dockerfile
                ports:
                  - "5000:5000"
                environment:
                  - FLASK_ENV=production
                healthcheck:
                  test: ["CMD", "curl", "-f", "http://localhost:5000/health"]
                  interval: 30s
                  timeout: 5s
                  retries: 3
        """))
    return ["docker-compose.yml"]


def _fix_secret_exposure(workspace: str) -> List[str]:
    patterns = [
        re.compile(r'^.*API_KEY\s*=\s*"[^"]*".*$', re.MULTILINE),
        re.compile(r'^.*DATABASE_PASSWORD\s*=\s*"[^"]*".*$', re.MULTILINE),
        re.compile(r'^.*WEBHOOK_SECRET\s*=\s*"[^"]*".*$', re.MULTILINE),
        re.compile(r'^.*SECRET_KEY\s*=\s*"[^"]*".*$', re.MULTILINE),
        re.compile(r'^# Third-party API integration credentials\s*$', re.MULTILINE),
    ]
    modified = []
    for root, _dirs, files in os.walk(workspace):
        if ".git" in root:
            continue
        for fname in files:
            if not fname.endswith(".py"):
                continue
            filepath = os.path.join(root, fname)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    original = f.read()
                content = original
                for pat in patterns:
                    content = pat.sub("", content)
                content = re.sub(r'\n{3,}', '\n\n', content)
                if content != original:
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(content)
                    modified.append(os.path.relpath(filepath, workspace))
            except OSError:
                continue
    return modified
