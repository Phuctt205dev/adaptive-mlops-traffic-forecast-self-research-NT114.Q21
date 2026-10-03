#!/bin/sh
set -eu

for database in "$APP_DB_NAME" "$MLFLOW_DB_NAME" "$AIRFLOW_DB_NAME"; do
    if ! psql --username "$POSTGRES_USER" --dbname postgres --tuples-only \
        --command "SELECT 1 FROM pg_database WHERE datname = '$database'" \
        | grep -q 1; then
        psql --username "$POSTGRES_USER" --dbname postgres \
            --command "CREATE DATABASE \"$database\" OWNER \"$POSTGRES_USER\""
    fi
done
