# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""
FastAPI application for the Meta Hackathon Environment.

This module creates an HTTP server that exposes the MetaHackathonEnvironment
over HTTP and WebSocket endpoints, compatible with EnvClient.

Automatically starts the CI/CD Dynamic API server on port 8001 for Theme #3
compliance (API-based dynamic system interactions).

Endpoints:
    - POST /reset: Reset the environment
    - POST /step: Execute an action
    - GET /state: Get current environment state
    - GET /schema: Get action/observation schemas
    - WS /ws: WebSocket endpoint for persistent sessions

Usage:
    # Development (with auto-reload):
    uvicorn server.app:app --reload --host 0.0.0.0 --port 8000

    # Production:
    uvicorn server.app:app --host 0.0.0.0 --port 8000 --workers 4

    # Or run directly:
    python -m server.app
    
    # The CI/CD API will be available at:
    # - http://localhost:8001 (API endpoints)
    # - http://localhost:8001/docs (Interactive documentation)
"""

import atexit
import logging
import multiprocessing
import os
import sys
import time

try:
    from openenv.core.env_server.http_server import create_app
except Exception as e:  # pragma: no cover
    raise ImportError(
        "openenv is required for the web interface. Install dependencies with '\n    uv sync\n'"
    ) from e

try:
    from ..models import MetaHackathonAction, MetaHackathonObservation
    from .environment import RealCICDRepairEnvironment
except (ImportError, ModuleNotFoundError):
    from models import MetaHackathonAction, MetaHackathonObservation
    from server.environment import RealCICDRepairEnvironment

logger = logging.getLogger(__name__)


_SHARED_REST_ENV = RealCICDRepairEnvironment()
_SHARED_REST_ENV.close = lambda: None  # prevent REST handlers from nuking _episode between requests

# Global reference to CI/CD API server process
_CICD_API_PROCESS = None


def _run_cicd_api_server(host: str, port: int) -> None:
    """Module-level target for multiprocessing.Process (must be picklable on Windows)."""
    try:
        import uvicorn
        from server.cicd_api import app as cicd_app

        logging.basicConfig(
            level=logging.INFO,
            format="[CI/CD API] %(levelname)s: %(message)s",
        )
        uvicorn.run(cicd_app, host=host, port=port, log_level="info", access_log=False)
    except Exception as exc:
        logging.error("CI/CD API server failed to start: %s", exc)
        sys.exit(1)


def _start_cicd_api_server(port: int = 8001, host: str = "0.0.0.0"):
    """Start the CI/CD Dynamic API server in a separate process"""
    global _CICD_API_PROCESS

    if _CICD_API_PROCESS is not None:
        logger.info("CI/CD API server already running")
        return

    # Start in separate process
    _CICD_API_PROCESS = multiprocessing.Process(
        target=_run_cicd_api_server,
        args=(host, port),
        daemon=True,
        name="CICD-API-Server",
    )
    _CICD_API_PROCESS.start()

    # Poll until the subprocess is accepting connections (up to 20 s).
    # Always probe 127.0.0.1 — 0.0.0.0 is not a valid destination on Windows.
    import requests as _req
    deadline = time.time() + 20
    healthy = False
    while time.time() < deadline:
        time.sleep(1)
        try:
            r = _req.get(f"http://127.0.0.1:{port}/health", timeout=2)
            if r.status_code == 200:
                healthy = True
                break
        except Exception:
            pass

    if healthy:
        logger.info(f"✅ CI/CD API server started successfully on http://0.0.0.0:{port}")
        logger.info(f"   API documentation: http://127.0.0.1:{port}/docs")
    else:
        logger.warning(f"⚠️  CI/CD API server did not become healthy within 20 s on port {port}")

    # Register cleanup handler
    atexit.register(_stop_cicd_api_server)


def _stop_cicd_api_server():
    """Stop the CI/CD API server process"""
    global _CICD_API_PROCESS
    
    if _CICD_API_PROCESS is not None and _CICD_API_PROCESS.is_alive():
        logger.info("Stopping CI/CD API server...")
        _CICD_API_PROCESS.terminate()
        _CICD_API_PROCESS.join(timeout=5)
        
        if _CICD_API_PROCESS.is_alive():
            logger.warning("CI/CD API server did not stop gracefully, forcing...")
            _CICD_API_PROCESS.kill()
        
        _CICD_API_PROCESS = None
        logger.info("CI/CD API server stopped")


def _shared_env_factory() -> RealCICDRepairEnvironment:
    # OpenEnv HTTP handlers call close() after every request; the no-op close above
    # preserves episode state across /reset and /step calls on the shared instance.
    return _SHARED_REST_ENV


# Create the app with web interface and README integration
app = create_app(
    _shared_env_factory,
    MetaHackathonAction,
    MetaHackathonObservation,
    env_name="meta_hackathon",
    max_concurrent_envs=1,  # increase this number to allow more concurrent WebSocket sessions
)


# Add startup event to launch CI/CD API server
@app.on_event("startup")
async def startup_event():
    """Start the CI/CD API server when main server starts"""
    cicd_api_port = int(os.getenv("CICD_API_PORT", "8001"))
    cicd_api_host = os.getenv("CICD_API_HOST", "0.0.0.0")
    
    logger.info("=" * 60)
    logger.info("Starting Meta Hackathon Environment Server")
    logger.info("=" * 60)
    
    # Start CI/CD API server
    _start_cicd_api_server(port=cicd_api_port, host=cicd_api_host)
    
    logger.info("=" * 60)
    logger.info("All services started successfully")
    logger.info("=" * 60)


@app.on_event("shutdown")
async def shutdown_event():
    """Stop the CI/CD API server when main server stops"""
    logger.info("Shutting down services...")
    _stop_cicd_api_server()


def main():
    """
    Entry point for direct execution via uv run or python -m.

    This function enables running the server without Docker:
        uv run --project . server
        uv run --project . server --port 8001
        python -m meta_hackathon.server.app

    For production deployments, consider using uvicorn directly with
    multiple workers:
        uvicorn meta_hackathon.server.app:app --workers 4
        
    The CI/CD Dynamic API server will automatically start on port 8001
    (or CICD_API_PORT environment variable).
    """
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--cicd-api-port", type=int, default=None,
                       help="Port for CI/CD API server (default: 8001)")
    args = parser.parse_args()
    
    # Set CI/CD API port from args if provided
    if args.cicd_api_port:
        os.environ["CICD_API_PORT"] = str(args.cicd_api_port)

    print("\n" + "=" * 60)
    print("Meta Hackathon CI/CD Environment - Theme #3 Compliant")
    print("=" * 60)
    print(f"Main Server: http://{args.host}:{args.port}")
    print(f"CI/CD API: http://{args.host}:{os.getenv('CICD_API_PORT', '8001')}")
    print(f"API Docs: http://{args.host}:{os.getenv('CICD_API_PORT', '8001')}/docs")
    print("=" * 60 + "\n")

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
