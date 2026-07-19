#!/usr/bin/env bash
# Provision the VTS application role and database on first cluster init.
#
# Runs from docker-entrypoint-initdb.d as the bootstrap superuser (`postgres`).
# The application role is created WITHOUT superuser so local and CI databases
# have the same privilege shape as production. That parity matters: when the
# app role was the superuser here, migration 0014's `CREATE EXTENSION vector`
# succeeded locally and in CI but could never succeed in prod, where the role
# is unprivileged (vts-e1p).
#
# Extensions needing superuser are installed here, for the same reason
# scripts/setup_postgres.sh does it on a real deployment.
set -euo pipefail

APP_USER="${VTS_DB_USER:-vts}"
APP_PASSWORD="${VTS_DB_PASSWORD:-vts}"
APP_DB="${VTS_DB_NAME:-vts}"

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname postgres \
  -v app_user="${APP_USER}" \
  -v app_password="${APP_PASSWORD}" \
  -v app_db="${APP_DB}" <<-'SQL'
	SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password')
	WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user')\gexec

	SELECT format('CREATE DATABASE %I OWNER %I', :'app_db', :'app_user')
	WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'app_db')\gexec

	SELECT format('GRANT ALL PRIVILEGES ON DATABASE %I TO %I', :'app_db', :'app_user')\gexec
SQL

# Superuser-only DDL: the app role cannot do this itself, which is the whole
# point of doing it here rather than in a migration.
psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${APP_DB}" \
  -c "CREATE EXTENSION IF NOT EXISTS vector"

echo "Provisioned application role '${APP_USER}' (no superuser) and database '${APP_DB}' with pgvector."
