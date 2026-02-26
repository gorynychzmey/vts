#!/usr/bin/env sh
set -eu

alembic upgrade head
exec "$@"
