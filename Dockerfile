FROM debian:stable AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    python3 python3-dev python3-venv python-is-python3 \
    build-essential pkg-config \
    libcairo2-dev libgirepository-2.0-dev && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock /
RUN uv venv && uv sync

FROM debian:stable-slim
ENV PATH="/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    python3 python-is-python3 python3-venv ca-certificates \
    libcairo2 libgirepository-2.0-0 gir1.2-json-1.0 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY --from=builder /.venv /.venv
COPY . /app
WORKDIR /app

EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
