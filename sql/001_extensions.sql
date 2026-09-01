-- Idempotent: safe to re-run against a warm volume.

CREATE EXTENSION IF NOT EXISTS pg_parquet;

CREATE SCHEMA IF NOT EXISTS procurement;
