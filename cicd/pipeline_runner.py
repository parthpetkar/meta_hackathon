"""Real CI/CD pipeline runner using subprocess for Git, Docker, and Docker Compose.

Executes four stages (clone, build, test, deploy) as real subprocesses,
capturing stdout/stderr, exit codes, and wall-clock durations.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PipelineStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"


STAGE_ORDER = ["clone", "build", "test", "deploy"]


@dataclass
class StageResult:
    """Result of a single pipeline stage execution."""
    name: str
    status: StageStatus = StageStatus.PENDING
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    command: str = ""


@dataclass
class PipelineResult:
    """Result of a full pipeline execution."""
    pipeline_id: str = ""
    status: PipelineStatus = PipelineStatus.PENDING
    stages: Dict[str, StageResult] = field(default_factory=dict)
    failed_stage: str = ""
    total_duration_seconds: float = 0.0
    workspace_dir: str = ""
    image_tag: str = ""

    def __post_init__(self):
        if not self.stages:
            self.stages = {name: StageResult(name=name) for name in STAGE_ORDER}

    def get_stage_logs(self, stage_name: str) -> str:
        stage = self.stages.get(stage_name)
        if not stage:
            return f"No logs available for stage '{stage_name}'"
        parts = []
        if stage.command:
            parts.append(f"$ {stage.command}")
        if stage.stdout:
            parts.append(stage.stdout)
        if stage.stderr:
            parts.append(stage.stderr)
        return "\n".join(parts) if parts else f"No output captured for stage '{stage_name}'"

    def get_stage_statuses(self) -> Dict[str, str]:
        return {name: stage.status.value for name, stage in self.stages.items()}

    def get_stage_durations(self) -> Dict[str, float]:
        return {name: round(stage.duration_seconds, 2) for name, stage in self.stages.items()}


def _run_subprocess(
    command: List[str],
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: int = 300,
) -> tuple[int, str, str]:
    """Run a subprocess and capture stdout, stderr, and exit code."""
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=merged_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out after {timeout}s: {' '.join(command)}"
    except FileNotFoundError as e:
        return 127, "", f"Command not found: {e}"
    except Exception as e:
        return 1, "", f"Subprocess error: {e}"


class PipelineRunner:
    """Runs a real CI/CD pipeline as subprocesses.

    Stages:
        1. CLONE  — git clone / skip if already in source repo
        2. BUILD  — secret scan + docker build
        3. TEST   — docker run pytest
        4. DEPLOY — docker compose up -d
    """

    def __init__(
        self,
        repo_path: str,
        workspace_base: str = "",
        image_tag: str = "",
        timeout_per_stage: int = 300,
        secret_scan_enabled: bool = True,
        log_config_check_enabled: bool = True,
    ):
        self.repo_path = os.path.abspath(repo_path)
        self.workspace_base = workspace_base or tempfile.mkdtemp(prefix="pipeline-ws-")
        self.image_tag = image_tag or f"sample-app-pipeline-{uuid.uuid4().hex[:8]}"
        self.timeout = timeout_per_stage
        self.secret_scan_enabled = secret_scan_enabled
        self.log_config_check_enabled = log_config_check_enabled
        self._result: Optional[PipelineResult] = None
        self._lock = threading.Lock()
        self._running = False

    @property
    def result(self) -> Optional[PipelineResult]:
        with self._lock:
            return self._result

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def run(self, workspace_dir: Optional[str] = None) -> PipelineResult:
        """Run the full pipeline synchronously and return a PipelineResult."""
        pipeline_id = uuid.uuid4().hex[:12]
        ws_dir = workspace_dir or os.path.join(self.workspace_base, f"build-{pipeline_id}")

        result = PipelineResult(
            pipeline_id=pipeline_id,
            status=PipelineStatus.RUNNING,
            workspace_dir=ws_dir,
            # Unique tag per run so Docker always builds a fresh image
            image_tag=f"{self.image_tag}-{pipeline_id}",
        )

        with self._lock:
            self._result = result
            self._running = True

        start_time = time.time()
        try:
            for stage_name in STAGE_ORDER:
                stage = result.stages[stage_name]
                stage.status = StageStatus.RUNNING

                stage_start = time.time()
                exit_code, stdout, stderr = self._execute_stage(stage_name, ws_dir, result)
                stage.duration_seconds = time.time() - stage_start
                stage.exit_code = exit_code
                stage.stdout = stdout
                stage.stderr = stderr

                if exit_code == 0:
                    stage.status = StageStatus.PASSED
                else:
                    stage.status = StageStatus.FAILED
                    result.status = PipelineStatus.FAILED
                    result.failed_stage = stage_name
                    for remaining in STAGE_ORDER[STAGE_ORDER.index(stage_name) + 1:]:
                        result.stages[remaining].status = StageStatus.SKIPPED
                    break
            else:
                result.status = PipelineStatus.PASSED
        except Exception:
            result.status = PipelineStatus.FAILED
            if not result.failed_stage:
                result.failed_stage = "unknown"
        finally:
            result.total_duration_seconds = time.time() - start_time
            with self._lock:
                self._running = False

        return result

    def run_async(
        self,
        workspace_dir: Optional[str] = None,
        callback: Optional[Callable[[PipelineResult], None]] = None,
    ) -> threading.Thread:
        """Run the pipeline in a background thread."""
        def _run():
            r = self.run(workspace_dir=workspace_dir)
            if callback:
                callback(r)
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        return thread

    # ── Stage executors ────────────────────────────────────────────────────

    def _execute_stage(self, stage_name: str, workspace_dir: str, result: PipelineResult) -> tuple[int, str, str]:
        if stage_name == "clone":
            return self._stage_clone(workspace_dir, result)
        elif stage_name == "build":
            return self._stage_build(workspace_dir, result)
        elif stage_name == "test":
            return self._stage_test(workspace_dir, result)
        elif stage_name == "deploy":
            return self._stage_deploy(workspace_dir, result)
        return 1, "", f"Unknown stage: {stage_name}"

    def _stage_clone(self, workspace_dir: str, result: PipelineResult) -> tuple[int, str, str]:
        if os.path.abspath(workspace_dir) == os.path.abspath(self.repo_path):
            result.stages["clone"].command = "skip (running in source repo)"
            return 0, "Already checked out locally.\n", ""

        if os.path.exists(workspace_dir) and os.path.isdir(os.path.join(workspace_dir, ".git")):
            cmd = ["git", "-C", workspace_dir, "pull", "--rebase"]
        else:
            cmd = ["git", "clone", self.repo_path, workspace_dir]

        result.stages["clone"].command = " ".join(cmd)
        return _run_subprocess(cmd, timeout=self.timeout)

    def _stage_build(self, workspace_dir: str, result: PipelineResult) -> tuple[int, str, str]:
        all_stdout, all_stderr = "", ""

        if self.secret_scan_enabled:
            scan_exit, scan_stdout, scan_stderr = self._secret_scan(workspace_dir)
            all_stdout += scan_stdout
            all_stderr += scan_stderr
            if scan_exit != 0:
                result.stages["build"].command = "secret-scan"
                return scan_exit, all_stdout, all_stderr

        cmd = ["docker", "build", "-t", result.image_tag, workspace_dir]
        result.stages["build"].command = " ".join(cmd)
        exit_code, stdout, stderr = _run_subprocess(cmd, cwd=workspace_dir, timeout=self.timeout)
        all_stdout += stdout
        all_stderr += stderr
        if exit_code != 0:
            return exit_code, all_stdout, all_stderr

        if self.log_config_check_enabled:
            chk_exit, chk_out, chk_err = self._log_config_check(workspace_dir, result.image_tag)
            all_stdout += chk_out
            all_stderr += chk_err
            if chk_exit != 0:
                return chk_exit, all_stdout, all_stderr

        return 0, all_stdout, all_stderr

    def _log_config_check(self, workspace_dir: str, image_tag: str) -> tuple[int, str, str]:
        """Run check_logs.py --config-only inside the freshly built image.

        Validates logging_config.py and routes.py for structural correctness and
        PII-logging patterns without needing a live container or log file.
        Skipped gracefully when the workspace has no scripts/check_logs.py.
        """
        script = os.path.join(workspace_dir, "scripts", "check_logs.py")
        if not os.path.exists(script):
            return 0, "Log config check skipped: scripts/check_logs.py not in workspace.\n", ""

        cmd = [
            "docker", "run", "--rm", image_tag,
            "python", "scripts/check_logs.py", "--config-only",
        ]
        exit_code, stdout, stderr = _run_subprocess(cmd, timeout=self.timeout)
        if exit_code != 0:
            header = "LOG CONFIG CHECK FAILED\n"
            return exit_code, stdout, header + stderr
        return 0, stdout, stderr

    def _secret_scan(self, workspace_dir: str) -> tuple[int, str, str]:
        """Scan source files for hardcoded secrets before docker build."""
        import re
        secrets_found = []
        token_patterns = ["sk-live-", "sk-test-", "sk_live_", "sk_test_", "AKIA", "ghp_", "gho_", "github_pat_"]
        assign_patterns = [
            re.compile(r"API_KEY\s*=\s*['\"]"),
            re.compile(r"SECRET_KEY\s*=\s*['\"]"),
            re.compile(r"PASSWORD\s*=\s*['\"]"),
        ]

        for root, _dirs, files in os.walk(workspace_dir):
            if ".git" in root:
                continue
            # Skip scripts/ — those files define scanning patterns and contain
            # the same token prefixes (e.g. "AKIA", "sk-live") as literals inside
            # regex strings, which causes false positives on every episode.
            rel_root = os.path.relpath(root, workspace_dir).replace("\\", "/")
            if rel_root.startswith("scripts"):
                continue
            for fname in files:
                ext = os.path.splitext(fname)[1]
                if ext not in (".py", ".yml", ".yaml", ".json", ".env", ".cfg"):
                    continue
                filepath = os.path.join(root, fname)
                rel = os.path.relpath(filepath, workspace_dir)
                try:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    for i, line in enumerate(content.splitlines(), 1):
                        if any(p in line for p in token_patterns):
                            secrets_found.append(f"{rel}:{i}: {line.strip()}")
                    if fname.endswith(".py"):
                        for pat in assign_patterns:
                            for m in pat.finditer(content):
                                line_num = content[: m.start()].count("\n") + 1
                                line = content.splitlines()[line_num - 1].strip()
                                entry = f"{rel}:{line_num}: {line}"
                                if entry not in secrets_found:
                                    secrets_found.append(entry)
                except OSError:
                    continue

        if secrets_found:
            stderr = "SECRET SCAN FAILED\nThe following hardcoded secrets were detected:\n"
            stderr += "".join(f"  ERROR: {f}\n" for f in secrets_found)
            stderr += "\nPolicy check failed: plaintext credential found in source code.\n"
            return 1, "", stderr

        return 0, "Secret scan passed: no hardcoded secrets detected.\n", ""

    def _stage_test(self, workspace_dir: str, result: PipelineResult) -> tuple[int, str, str]:
        cmd = ["docker", "run", "--rm", result.image_tag, "python", "-m", "pytest", "tests/", "-v"]
        result.stages["test"].command = " ".join(cmd)
        return _run_subprocess(cmd, timeout=self.timeout)

    def _stage_deploy(self, workspace_dir: str, result: PipelineResult) -> tuple[int, str, str]:
        # Prefer the multi-service compose file under shared-infra/ when present,
        # as that is where multi-app faults (log_volume_missing, infra_port_conflict,
        # shared_secret_rotation) are injected.
        shared_infra_compose = os.path.join(workspace_dir, "shared-infra", "docker-compose.yml")
        root_compose = os.path.join(workspace_dir, "docker-compose.yml")

        if os.path.exists(shared_infra_compose):
            compose_file = shared_infra_compose
            cwd = os.path.join(workspace_dir, "shared-infra")
        elif os.path.exists(root_compose):
            compose_file = root_compose
            cwd = workspace_dir
        else:
            return 1, "", f"docker-compose.yml not found in {workspace_dir} or {workspace_dir}/shared-infra"

        cmd = ["docker", "compose", "-f", compose_file, "up", "-d"]
        result.stages["deploy"].command = " ".join(cmd)
        return _run_subprocess(cmd, cwd=cwd, timeout=self.timeout)


def cleanup_pipeline(result: PipelineResult) -> None:
    """Remove Docker image and stop compose services created by a pipeline run."""
    if result.image_tag:
        try:
            subprocess.run(["docker", "rmi", "-f", result.image_tag], capture_output=True, timeout=30)
        except Exception:
            pass

    # Mirror the same compose-file selection logic used in _stage_deploy
    shared_infra_compose = os.path.join(result.workspace_dir, "shared-infra", "docker-compose.yml")
    root_compose = os.path.join(result.workspace_dir, "docker-compose.yml")
    compose_file = shared_infra_compose if os.path.exists(shared_infra_compose) else root_compose
    if os.path.exists(compose_file):
        try:
            subprocess.run(
                ["docker", "compose", "-f", compose_file, "down", "--remove-orphans"],
                capture_output=True, timeout=30,
            )
        except Exception:
            pass


def setup_repo_from_template(template_dir: str, target_dir: str) -> str:
    """Copy template into target_dir, init a git repo, and make an initial commit."""
    import shutil

    if os.path.exists(target_dir):
        shutil.rmtree(target_dir)
    shutil.copytree(template_dir, target_dir, dirs_exist_ok=False)

    ci_env = {
        "GIT_AUTHOR_NAME": "CI Bot", "GIT_AUTHOR_EMAIL": "ci@example.com",
        "GIT_COMMITTER_NAME": "CI Bot", "GIT_COMMITTER_EMAIL": "ci@example.com",
    }
    _run_subprocess(["git", "init"], cwd=target_dir)
    _run_subprocess(["git", "checkout", "-b", "main"], cwd=target_dir)
    _run_subprocess(["git", "add", "."], cwd=target_dir)
    _run_subprocess(["git", "commit", "-m", "Initial commit: sample API service"], cwd=target_dir, env=ci_env)

    return target_dir
