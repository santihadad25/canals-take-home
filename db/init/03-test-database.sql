-- The tests get their own database, with the same schema and no data, so they never touch the demo
-- data.
CREATE DATABASE canals_test;
\connect canals_test
\i /docker-entrypoint-initdb.d/01-schema.sql
