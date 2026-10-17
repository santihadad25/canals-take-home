#!/bin/sh
# The tests get their own database, with the same schema and no data, so they never touch the demo
# data. Postgres runs this script on the first start, with the environment of the container.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -c "CREATE DATABASE \"$POSTGRES_TEST_DB\""

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_TEST_DB" \
  -f /docker-entrypoint-initdb.d/01-schema.sql
