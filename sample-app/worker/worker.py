"""Background worker that polls api-service for data using a shared secret."""

import os
import time

import requests

API_URL = os.environ.get("API_URL", "http://api-service:9000")
AUTH_SECRET = os.environ.get("AUTH_SECRET", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))


def fetch_data() -> dict:
    headers = {"X-Auth-Token": AUTH_SECRET}
    response = requests.get(f"{API_URL}/data", headers=headers, timeout=5)
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    print(f"Worker starting — polling {API_URL} every {POLL_INTERVAL}s", flush=True)
    while True:
        try:
            data = fetch_data()
            print(f"Received: {data}", flush=True)
        except Exception as exc:
            print(f"Error fetching data: {exc}", flush=True)
        time.sleep(POLL_INTERVAL)
