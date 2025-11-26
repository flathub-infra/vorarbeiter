#!/bin/bash
set -ex
alembic upgrade head
exec granian --interface asgi app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY:-1} --access-log $@
