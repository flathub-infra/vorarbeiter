FROM debian:stable AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY pyproject.toml uv.lock /
ENV UV_PYTHON_INSTALL_DIR=/python
RUN uv python install 3.13
RUN uv venv && uv sync

FROM debian:stable-slim
ENV PATH="/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

COPY --from=builder /.venv /.venv
COPY --from=builder /python /python
COPY . /app
WORKDIR /app

EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["granian", "--interface", "asgi", "--host", "0.0.0.0", "--port", "8000", "app.main:app"]
