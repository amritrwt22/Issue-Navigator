-- Navigator schema — seven tables: repositories, ingest_jobs, issues,
-- issue_comments, pull_requests, issue_fixes, pr_files.
--
-- What it holds: the history of FIXED issues. For each one, the issue text and
-- its comments, the merged PR that fixed it, and the files that PR changed. That
-- last list is the answer key: the files Navigator should have pointed at.
-- Plus one row per ingest run, so a run can be followed and its failure recorded.
--
-- Shape: `repositories` is the root. Everything else points back to it, and
-- issue ↔ PR is many-to-many through `issue_fixes`.
--
--   repositories 1 ──< ingest_jobs
--                1 ──< issues         ─┐
--                1 ──< pull_requests  ─┴── issue_fixes
--                      issues        1 ──< issue_comments
--                      pull_requests 1 ──< pr_files
--
-- Apply, as the account navigator, into its own database (from issue-navigator/):
--   docker exec -i repolens-db psql -U navigator -d navigator -v ON_ERROR_STOP=1 < sql/02_schema.sql
--
-- This file drops and recreates. That's safe while the data is throwaway: every row
-- comes from GitHub, so ingest can rebuild it. Delete the DROP before the data
-- becomes something that can't be fetched again, and move to numbered migrations.


-- Wipe the tables first, so running this file always rebuilds them from scratch.
DROP TABLE IF EXISTS pr_files, issue_fixes, issue_comments, pull_requests, issues,
                     ingest_jobs, repositories CASCADE;


-- One row per GitHub repository Navigator tracks. The root — everything else
-- points back here.
--
-- There is no foreign key to RepoLens: it lives in another database, and Postgres
-- can't reference across databases. The normalised names are the link instead.
-- RepoLens keys its repositories on the same lowercased pair, so the same
-- WHERE owner_norm = ... AND repo_norm = ... finds the repo in both databases.
--
-- synced_sha = is the history ready for briefings yet. It plays the role of
-- RepoLens's commit_sha:
--   first ingest:  NULL until a run succeeds, so nothing is briefed from half
--                  a history
--   later runs:    keeps the old value the whole time, and moves only if the new
--                  run succeeds, so a failed run leaves the old history usable
--   skipping:      a run that finds HEAD equal to synced_sha ends as 'skipped'.
--                  Every stored fix is a merged PR, and a merge moves HEAD, so an
--                  unmoved HEAD means there are no new fixed issues
--
-- Run state (in progress, failed) lives on ingest_jobs, because one column could
-- not say "usable" and "the last attempt failed" at once.

CREATE TABLE repositories (
    id          serial primary key,
    owner       text not null,        -- as GitHub spells it, for API calls and display
    repo        text not null,
    owner_norm  text not null,        -- lowercased; GitHub treats names as
    repo_norm   text not null,        -- case-insensitive, so the key must too

    synced_sha  text,                 -- HEAD at the last successful ingest; NULL = never
    synced_at   timestamptz,          -- when that run STARTED, so a fix merged during
                                      -- the run is fetched by the next one. Later it is
                                      -- GitHub's since: filter for incremental sync

    UNIQUE (owner_norm, repo_norm)    -- one row per repo, so nvidia/Garak and
                                      -- NVIDIA/garak can't become two rows
);


-- One row per ingest run. status = where the run is, conclusion = how it ended.
-- conclusion stays NULL until status reaches 'completed'.
--   success    the history was fetched and stored
--   skipped    HEAD hadn't moved since the last successful run, so nothing was fetched
--   failure    something broke; error holds the reason
--   cancelled  stopped by Ctrl-C, or swept up after a hard kill
--
-- Navigator is used through an API: a run is started, the client gets the job's
-- id at once, and it polls this row. Nobody is watching a terminal, so the
-- outcome, the counts and the reason for a failure are stored here, not printed.
--
-- The row is written and committed before the long fetch starts, so it survives
-- a failed run and can say why. The full flow is in navigator/services/ingest.py.

CREATE TABLE ingest_jobs (
    id              serial primary key,
    repository_id   integer not null references repositories(id) on delete cascade,

    commit_sha      text not null,          -- the HEAD this run saw; it becomes the
                                            -- repo's synced_sha if the run succeeds

    status          text not null
                    CHECK (status IN ('queued','in_progress','completed')),
    conclusion      text                    -- NULL until completed
                    CHECK (conclusion IN ('success','skipped','failure','cancelled')),

    issues_kept     integer,                -- set on success only: the issues stored,
    issues_dropped  integer,                -- and the ones the INGEST RULES dropped

    error           text,                   -- why the run didn't succeed: set on failure
                                            -- and cancelled, NULL otherwise
    started_at      timestamptz,            -- NULL while queued
    finished_at     timestamptz             -- NULL until completed
);


-- One row per FIXED issue: closed as completed, with a merged PR linked to it.
-- Issues closed as "not planned" have no fix to learn from, and open issues are
-- fetched live when someone asks for a briefing, so neither is stored.
--
-- title + body are what Navigator searches the code with. The discussion under the
-- issue lives in issue_comments.
--
-- created_at is the cutoff when this issue is the one being answered: the history
-- Navigator may use (fixes merged, comments posted) must be older than it.

CREATE TABLE issues (
    id             serial primary key,
    repository_id  integer not null references repositories(id) on delete cascade,

    number         integer not null,      -- the #2413; GitHub's id for it within the repo
    title          text not null,
    body           text not null default '',   -- '' when the reporter wrote nothing
    labels         text[] not null default '{}',   -- bug / enhancement / ...; later phases
                                                    -- treat bugs and features differently
    created_at     timestamptz not null,  -- when the issue was opened

    UNIQUE (repository_id, number)        -- one row per issue, and the upsert key:
                                          -- re-running ingest updates, never duplicates
);


-- One row per human comment on a stored issue. Its own table, because one issue
-- has many comments.
--
-- Comments are often where the cause is found ("the bug is in detect(), it looks
-- names up verbatim"), which is exactly what a similar new issue needs. That's
-- safe for the same reason PR descriptions are: history only includes what
-- existed before the issue being answered, and created_at is how that's checked.
--
-- Bot comments (github-actions adding labels, stale-issue reminders) are skipped
-- at ingest. They carry no information about the code.
--
-- No serial id: nothing points at a comment, and GitHub's own comment id is
-- already unique across all of GitHub, so it serves as the key directly.

CREATE TABLE issue_comments (
    github_id   bigint primary key,       -- GitHub's id for the comment; the upsert key
    issue_id    integer not null references issues(id) on delete cascade,

    body        text not null,
    created_at  timestamptz not null      -- when it was posted; the time filter
);


-- One row per merged PR that fixed at least one stored issue.
--
-- merged_at, not the issue's close time, is when the fix became KNOWN. History
-- filters on it: a past issue counts only if its fix was merged before the new
-- issue was opened.
--
-- merge_commit_sha is the commit where the fix entered the code. Its parent is
-- the code right BEFORE the fix, which is what a fair test has to search.
--
-- title + body say HOW the fix was done, in the author's words. The LLM uses them
-- to explain similar past fixes. That's safe: history only includes PRs merged
-- before the issue being answered, so an issue's own fix is never visible.

CREATE TABLE pull_requests (
    id                serial primary key,
    repository_id     integer not null references repositories(id) on delete cascade,

    number            integer not null,   -- the #2415
    title             text not null,      -- "fix(detectors): accept underscore import names ..."
    body              text not null default '',   -- the PR description; '' when empty
    merged_at         timestamptz not null,
    merge_commit_sha  text not null,      -- 40 hex characters

    UNIQUE (repository_id, number)        -- one row per PR, and the upsert key
);


-- Which PR fixed which issue. Its own table because the link is many-to-many:
-- one issue can be closed by several PRs, and one PR can fix several issues.
-- A fix_pr column on issues could hold only one.
--
-- The only source is GitHub's own closing link (an issue's "Development" sidebar).
-- GitHub already turns "Fixes #2413" in a merged PR into that link, so there's no
-- second source, and no column saying which source a row came from.

CREATE TABLE issue_fixes (
    issue_id         integer not null references issues(id) on delete cascade,
    pull_request_id  integer not null references pull_requests(id) on delete cascade,

    PRIMARY KEY (issue_id, pull_request_id)   -- each link once
);


-- One row per file a fix PR changed. This is the answer key.
--
-- change_type decides whether a file was FINDABLE. A file the fix added did not
-- exist in the code before the fix, so no search could have found it, and scoring
-- leaves it out. A renamed file is treated the same way: GitHub's GraphQL API
-- doesn't return the old name, and renames are rare in fixes.
--
-- Whether a file is source, test or docs is not stored. It follows from the path
-- (tests/..., *.md) and is worked out when it's needed.

CREATE TABLE pr_files (
    pull_request_id  integer not null references pull_requests(id) on delete cascade,

    path             text not null,       -- relative to the repo root, e.g. "garak/detectors/packagehallucination.py"
    change_type      text not null
                     CHECK (change_type IN ('added','modified','deleted','renamed','copied','changed')),

    PRIMARY KEY (pull_request_id, path)   -- one row per file per PR
);


-- ---------------------------------------------------------------------------
-- NOT STORED, AND WHY
-- ---------------------------------------------------------------------------
--   bot comments             labels and reminders from bots; skipped at ingest
--   not-planned issues       no fix to learn from
--   open issues              fetched live when briefed; history is fixed issues only
--   diffs                    large, and Navigator predicts files, not lines
--   old names of renames     not in GitHub's GraphQL API; renames treated as unfindable
--   file kind                derived from the path when needed
--   authors, assignees,      Navigator never uses them
--     reactions, milestones
--   issue closed_at          the PR's merged_at is the moment that matters
--   URLs                     built from owner + repo + number; derive, don't copy
--   embeddings               arrive with history search, as a new column on issues


-- ---------------------------------------------------------------------------
-- INDEXES
-- ---------------------------------------------------------------------------
-- Postgres builds an index by itself for every PRIMARY KEY and UNIQUE, because
-- rejecting a duplicate means searching the whole table. Those cover every
-- lookup ingest makes:
--
--   repositories (id)                      repositories (owner_norm, repo_norm)
--   ingest_jobs (id)
--   issues (id)                            issues (repository_id, number)
--   issue_comments (github_id)
--   pull_requests (id)                     pull_requests (repository_id, number)
--   issue_fixes (issue_id, pull_request_id)
--   pr_files (pull_request_id, path)
--
-- By the leftmost rule, a composite index also serves its leading columns alone,
-- so those also cover:
--
--   issues (repository_id)                 pull_requests (repository_id)
--   issue_fixes (issue_id)                 pr_files (pull_request_id)
--
-- Not covered by anything above. Nothing is made yet; each waits for the query
-- that needs it:
--
--   ingest_jobs (repository_id)            DEFERRED: a repo's latest run. The query
--                                          arrives with the API's polling, and a few
--                                          dozen rows scan faster than an index anyway
--
--   issue_fixes (pull_request_id)          DEFERRED: PR -> its issues. Nothing
--                                          asks that yet
--
--   issue_comments (issue_id)              DEFERRED: an issue -> its comments.
--                                          History search will ask this; add it
--                                          then, measured with EXPLAIN ANALYZE
--
--   pull_requests (repository_id,          DEFERRED: the time filter in history
--                  merged_at)              search (fixes merged before the cutoff).
--                                          A few hundred rows scan faster than an
--                                          index; measure with EXPLAIN ANALYZE first
-- ---------------------------------------------------------------------------
