FROM quay.io/hummingbird/python:3.13-builder AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

USER root
COPY pyproject.toml uv.lock /
RUN uv venv && uv sync

FROM quay.io/hummingbird/python:3.13
ENV PATH="/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    PORT=8000

COPY --from=builder /usr/bin/bash /usr/bin/bash
COPY --from=builder /.venv /.venv
COPY . /app
WORKDIR /app

EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
