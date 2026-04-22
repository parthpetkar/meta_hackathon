FROM python:3.11-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Install dependencies first for layer caching
COPY services/api/requirements.txt /app/requirements.txt
RUN uv pip install --system --no-cache -r requirements.txt

# Copy application code and pipeline scripts
COPY . /app/

# Create the logs directory with open permissions so any runtime user can write logs.
# The host volume mount (./logs:/app/logs) overlays this at runtime; the directory
# must exist in the image so the container starts cleanly even without the mount.
RUN mkdir -p /app/logs && chmod 777 /app/logs

EXPOSE 5000

CMD ["python", "-m", "services.api.app"]
