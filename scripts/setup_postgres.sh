#!/usr/bin/env bash
set -euo pipefail

ENGINE="${CONTAINER_ENGINE:-podman}"
DB_SERVICE="${DB_SERVICE:-postgres}"
DB_ADMIN_USER="${DB_ADMIN_USER:-postgres}"
VTS_DB_USER="${VTS_DB_USER:-vts}"
VTS_DB_PASSWORD="${VTS_DB_PASSWORD:-vts}"
VTS_DB_NAME="${VTS_DB_NAME:-vts}"

echo "Ensuring postgres service is running..."
"${ENGINE}" compose up -d "${DB_SERVICE}"

echo "Creating/updating PostgreSQL role and database (idempotent)..."
cat <<'SQL' | "${ENGINE}" compose exec -T "${DB_SERVICE}" psql -U "${DB_ADMIN_USER}" -d postgres \
  -v ON_ERROR_STOP=1 \
  -v vts_user="${VTS_DB_USER}" \
  -v vts_password="${VTS_DB_PASSWORD}" \
  -v vts_db="${VTS_DB_NAME}"
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'vts_user', :'vts_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'vts_user')\gexec

SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'vts_user', :'vts_password')\gexec

SELECT format('CREATE DATABASE %I OWNER %I', :'vts_db', :'vts_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'vts_db')\gexec

SELECT format('GRANT ALL PRIVILEGES ON DATABASE %I TO %I', :'vts_db', :'vts_user')\gexec
SQL

# The pgvector extension must be installed by a superuser: migration
# 0014_pgvector_extension runs `CREATE EXTENSION IF NOT EXISTS vector` as the
# application role, which is not a superuser and would fail with
# "permission denied to create extension". Doing it here (as the admin user)
# makes that migration a no-op.
echo "Installing pgvector extension (requires superuser)..."
"${ENGINE}" compose exec -T "${DB_SERVICE}" psql -U "${DB_ADMIN_USER}" -d "${VTS_DB_NAME}" \
  -v ON_ERROR_STOP=1 \
  -c "CREATE EXTENSION IF NOT EXISTS vector"

echo "PostgreSQL setup complete: user=${VTS_DB_USER}, db=${VTS_DB_NAME}"

