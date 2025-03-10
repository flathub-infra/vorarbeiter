FROM debian:testing AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY pyproject.toml uv.lock /
ENV UV_PYTHON_INSTALL_DIR=/python
RUN uv venv && uv sync

FROM debian:testing-slim
ENV PATH="/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=builder /.venv /.venv
COPY --from=builder /python /python
COPY . /app
WORKDIR /app

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "vorarbeiter.wsgi:application", "--bind", "0.0.0.0:8000"]
