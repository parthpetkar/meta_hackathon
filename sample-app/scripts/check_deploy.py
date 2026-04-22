#!/usr/bin/env python3
"""Post-deploy health and integration probe for the CI/CD pipeline deploy stage.

Runs after `docker compose up -d` to verify that services are actually reachable
and behaving correctly. Catches faults that docker-compose itself cannot detect
because it only checks whether containers *start*, not whether they *work*.

Checks performed:
  1. api-service /health endpoint responds 200 within timeout
  2. Worker auth probe: /data with the correct AUTH_SECRET returns 200
     (catches shared_secret_rotation — rotated secret in .env means the
      worker's baked-in old secret is rejected with 401)
  3. Port binding sanity: api-service is reachable on the expected port
     (catches infra_port_conflict)

Exit codes:
    0   All checks passed.
    1   One or more checks failed.

Usage:
    python scripts/check_deploy.py [--host HOST] [--port PORT] [--timeout TIMEOUT]
    python scripts/check_deploy.py --env-file shared-infra/.env
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _load_env_file(path: str) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file, ignoring comments and blank lines."""
    env: dict[str, str] = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            # Strip inline comments (e.g. "value  # comment")
            val = val.split("#")[0].strip()
            env[key.strip()] = val
    except OSError:
        pass
    return env


def _http_get(url: str, headers: dict[str, str] | None = None, timeout: float = 5.0) -> tuple[int, str]:
    """Perform a GET request. Returns (status_code, body)."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, str(exc.reason)
    except Exception as exc:
        return 0, str(exc)


def wait_for_health(base_url: str, timeout: float, interval: float = 1.0) -> bool:
    """Poll /health until it returns 200 or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, _ = _http_get(f"{base_url}/health", timeout=2.0)
        if status == 200:
            return True
        time.sleep(interval)
    return False


def check_auth(base_url: str, secret: str) -> tuple[int, str]:
    """Call /data with the given secret. Returns (status_code, body)."""
    return _http_get(
        f"{base_url}/data",
        headers={"X-Auth-Token": secret},
        timeout=5.0,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Post-deploy integration probe.")
    parser.add_argument("--host", default=os.environ.get("API_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("API_PORT", "9000")))
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="Seconds to wait for api-service to become healthy.")
    parser.add_argument("--env-file", default="",
                        help="Path to .env file to read AUTH_SECRET from.")
    args = parser.parse_args()

    failures: list[str] = []

    # Resolve AUTH_SECRET: env-file > environment variable > empty
    env_vars: dict[str, str] = {}
    if args.env_file:
        env_vars = _load_env_file(args.env_file)
    auth_secret = env_vars.get("AUTH_SECRET") or os.environ.get("AUTH_SECRET", "")

    base_url = f"http://{args.host}:{args.port}"

    # ── Check 1: api-service reachable and healthy ─────────────────────────
    print(f"[check_deploy] Waiting for api-service at {base_url}/health ...", file=sys.stderr)
    healthy = wait_for_health(base_url, timeout=args.timeout)
    if not healthy:
        failures.append(
            f"api-service did not become healthy at {base_url}/health within {args.timeout}s — "
            "check port bindings and container startup logs"
        )
        # No point running auth check if service is unreachable
        print(f"check_deploy: {len(failures)} issue(s) found.", file=sys.stderr)
        for f in failures:
            print(f"  FAIL: {f}", file=sys.stderr)
        return 1

    print("[check_deploy] api-service /health: OK", file=sys.stderr)

    # ── Check 2: auth probe ────────────────────────────────────────────────
    if auth_secret:
        status, body = check_auth(base_url, auth_secret)
        if status == 200:
            print("[check_deploy] Auth probe (/data): OK", file=sys.stderr)
        elif status == 401:
            failures.append(
                f"Auth probe failed: /data returned 401 Unauthorized with the current AUTH_SECRET — "
                "shared secret may have been rotated without updating all services "
                f"(body: {body[:120]})"
            )
        else:
            failures.append(
                f"Auth probe failed: /data returned unexpected status {status} "
                f"(body: {body[:120]})"
            )
    else:
        print("[check_deploy] AUTH_SECRET not set — skipping auth probe.", file=sys.stderr)

    if failures:
        print(f"\nDEPLOY VALIDATION FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  FAIL: {f}", file=sys.stderr)
        print(f"\ncheck_deploy: {len(failures)} issue(s) found.", file=sys.stderr)
        return 1

    print("check_deploy: all deploy checks passed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
