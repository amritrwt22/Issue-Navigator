-- 01_repolens_readonly.sql — a read-only account for RepoLens's code index.
--
-- Creates the account navigator_ro and lets it READ 3 tables in the repolens database.
-- Navigator uses it to search code; Postgres refuses any write from it.
--
-- Run as superuser repolens, from issue-navigator/:
--   docker exec -i repolens-db psql -U repolens -d repolens -v ON_ERROR_STOP=1 < sql/01_repolens_readonly.sql
-- Idempotent: safe to re-run. Re-run it after RepoLens re-applies its schema.sql
-- (that recreates RepoLens's tables, which drops the SELECT grant).


-- 1. The account. Same pattern as 00: create it only if it doesn't exist yet.
--    It starts with no permissions at all.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'navigator_ro') THEN
        CREATE ROLE navigator_ro LOGIN PASSWORD 'navigator_ro';
    END IF;
END
$$;

-- 2. Three permissions, one per level. All three are needed to read a table.
--    GRANT is idempotent by itself: granting something already granted does nothing.
GRANT CONNECT ON DATABASE repolens TO navigator_ro;                 -- may connect to the database
GRANT USAGE ON SCHEMA public TO navigator_ro;                       -- may see inside schema "public"
GRANT SELECT ON repositories, files, code_chunks TO navigator_ro;   -- may READ these 3 tables
