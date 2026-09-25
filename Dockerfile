FROM python:3.12-slim-bookworm AS builder

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.11.27

COPY pyproject.toml uv.lock ./
COPY internal ./internal
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv ./.venv
COPY internal ./internal
COPY cmd ./cmd
RUN mkdir -p private/cookies downloads logs

CMD ["python", "cmd/main.py"]
