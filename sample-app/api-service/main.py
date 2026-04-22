"""Minimal FastAPI service that authenticates using a shared secret."""

import os
from typing import Optional

from fastapi import FastAPI, Header, HTTPException

app = FastAPI(title="api-service")

AUTH_SECRET = os.environ.get("AUTH_SECRET", "")


@app.get("/health")
def health():
    return {"status": "healthy", "service": "api-service"}


@app.get("/data")
def get_data(x_auth_token: Optional[str] = Header(None)):
    secret = os.environ.get("AUTH_SECRET", AUTH_SECRET)
    if not secret or x_auth_token != secret:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing auth token")
    return {"data": "sample payload from api-service"}
