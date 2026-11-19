-- =============================================================================
-- 00_navigator_db.sql — Navigator's own account and database
-- =============================================================================
--
-- WHAT THIS FILE DOES
--   Creates the account `navigator` and the database `navigator` on the existing
--   Postgres server repolens-db, so that Navigator has its own place to store data
--   (the issue history: issue -> the PR that fixed it -> the files it changed).
--   RepoLens's database and tables are not touched.
--
-- THE SERVER, AFTER ALL SETUP FILES HAVE RUN
--
--   repolens-db  (Postgres server, Docker container, port 5433)
--   |
--   +-- database repolens    owner: repolens    RepoLens's code index (already exists)
--   +-- database navigator   owner: navigator   Navigator's issue history (THIS FILE)
--   |
--   +-- accounts
--         repolens       superuser (admin): can do anything on the whole server.
--                        Created by Docker from POSTGRES_USER in RepoLens's
--                        docker-compose.yml. Used to run these setup files.
--         navigator      owner of the navigator database: full control inside it,
--                        no access to anything else. (THIS FILE)
--         navigator_ro   may only READ 3 tables in the repolens database.
--                        (next setup file)
--
-- OWNER vs SUPERUSER
--   Owner      = a label on each database: the account with full control over it.
--                Each database has exactly one owner.
--   Superuser  = a power on an account: it skips every permission check on the
--                server. repolens can manage the navigator database without owning it.
--
-- PERMISSIONS
--   Reading a table needs CONNECT (database) + USAGE (schema "public") + SELECT (table).
--   An owner has all three in its own database; navigator_ro gets them in the next file.
--
-- RUN (once, as superuser repolens, from issue-navigator/)
--   docker exec -i repolens-db psql -U repolens -d repolens -v ON_ERROR_STOP=1 < sql/00_navigator_db.sql
--   Idempotent: safe to re-run. Anything that already exists is skipped.
--
-- CHECK
--   docker exec -it repolens-db psql -U navigator -d navigator   -> prompt: navigator=>
-- =============================================================================


-- 1. Create the account Navigator logs in with to reach its own database.
--      CREATE ROLE navigator   adds a new account named "navigator" to the server
--      LOGIN                   lets it connect (without this, a role can't log in at all)
--      PASSWORD 'navigator'    the password it connects with (local Docker database only)
--    It starts with no permissions; owning the database in step 2 gives it full
--    control there and nowhere else.
--    Idempotent: Postgres has no "CREATE ROLE IF NOT EXISTS", so the check is written
--    by hand. DO $$ ... $$ runs a small block of code inside Postgres; it creates the
--    account only if pg_roles (Postgres's list of accounts) doesn't have it yet.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'navigator') THEN
        CREATE ROLE navigator LOGIN PASSWORD 'navigator';
    END IF;
END
$$;

-- 2. Navigator's database, owned by that account.
--    The account has to exist before it can own a database, so this comes second.
--
--    What we want:  "CREATE DATABASE navigator OWNER navigator, but only if it doesn't exist."
--    Problem:       there's no CREATE DATABASE IF NOT EXISTS, and the DO $$ ... $$ trick
--                   from step 1 is not allowed for CREATE DATABASE.
--    Solution:      ask a question whose ANSWER is the command, then run the answer.
--
--    Read the two lines below in three parts:
--
--    a) SELECT 'CREATE DATABASE navigator OWNER navigator'
--         Selects a piece of TEXT, the same way  SELECT 'hello'  returns the text hello.
--         Nothing is created yet. It's just a string that happens to be a command.
--
--    b) WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'navigator')
--         pg_database is Postgres's built-in list of all databases.
--         The inner SELECT looks for one named "navigator".
--         NOT EXISTS = true only if it is NOT found.
--         So:  database missing  -> the WHERE is true  -> 1 row comes back (the text)
--              database exists   -> the WHERE is false -> 0 rows come back
--
--    c) \gexec
--         Not SQL. It's a psql command (like \q), so it ends the line instead of ";".
--         It means: "run the query, then EXECUTE each value it returned as a command."
--         1 row back  -> psql runs  CREATE DATABASE navigator OWNER navigator
--         0 rows back -> psql runs nothing -> no error, the database is left alone
--
--    Same idea in Python:
--         if "navigator" not in existing_databases:
--             run("CREATE DATABASE navigator OWNER navigator")
SELECT 'CREATE DATABASE navigator OWNER navigator'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'navigator')\gexec
