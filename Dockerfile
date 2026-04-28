FROM debian:stable AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2-dev libgirepository1.0-dev pkg-config && \
    rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock /
ENV UV_PYTHON_INSTALL_DIR=/python
RUN uv python install 3.13
RUN uv venv && uv sync

FROM debian:stable-slim
ENV PATH="/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 libgirepository-1.0-1 gir1.2-json-1.0 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY --from=builder /.venv /.venv
COPY --from=builder /python /python
COPY . /app
WORKDIR /app

EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
