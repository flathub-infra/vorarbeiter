#!/bin/bash
set -ex
alembic upgrade head
exec "$@"
