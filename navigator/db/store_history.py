# db/store_history.py : the only module that writes to Navigator's database.
#
# Takes the fixed-issue history that services/ingest.py decided to keep (GitHub's
# dicts, already filtered by the INGEST RULES) and puts it in the six history tables.
# It also records each ingest run in ingest_jobs.
# It makes no decisions and filters nothing: whatever it is handed, it stores.
#
# It never creates a connection and never commits. It is handed one, and the caller
# owns the transaction. That way the same code works from a script today and from
# an API request later.
#
# Writes only. Reads come later, one file per table (db/issues.py, ...), the way
# RepoLens splits them.
#
#
# FUNCTIONS, IN THE ORDER INGEST CALLS THEM, BY TRANSACTION
#
#   T1  open the run
#         get_or_create_repository   find or create the repo's row      -> (repo_id, synced_sha)
#         start_job                  a new ingest_jobs row for this run  -> job_id
#                                    (HEAD == synced_sha: mark_job_completed 'skipped', stop)
#
#   T2  store the history
#         store_issue                one fixed issue                     -> issue_id
#         store_comments             that issue's human comments
#         store_pull_request         one of its fix PRs                  -> pr_id
#         store_pr_files             the files that PR changed (the answer key)
#         store_fix                  the issue <-> PR link
#         mark_repository_synced     copy the job's HEAD and start time onto the repo
#         mark_job_completed         close the run as a success, with the counts
#
#   T3  record the failure
#         mark_job_completed         close the run as a failure or cancelled, with the reason
#
# Where each transaction begins and commits is decided in services/ingest.py.
#
#
# RE-RUNS: every history write is idempotent, so running ingest twice gives the same
# rows, never duplicates.
#
#   A plain INSERT of a new row inserts it, and RETURNING id gives its new id.
#   On a re-run the row already exists, so the INSERT hits the table's unique key
#   (a conflict). ON CONFLICT says what to do then. There are three options:
#
#   1. ON CONFLICT DO NOTHING
#        the existing row stays as it is, and no id comes back.
#        For rows we never change and never need the id of.
#
#   2. ON CONFLICT DO UPDATE ... RETURNING id
#        the existing row is updated with the new values, and its id comes back.
#        For rows that can be edited on GitHub and whose id the next table needs.
#        One statement does both: the update and the id.
#
#   3. ON CONFLICT DO NOTHING, then a separate SELECT id
#        the existing row stays as it is, and the SELECT reads its id.
#        For rows we never change but whose id we still need.
#
#   The catch, and the reason option 3 exists: with DO NOTHING, RETURNING id gives
#   back NOTHING when the row already exists. It only returns rows the statement
#   actually inserted or updated. So DO NOTHING + RETURNING id works on the first
#   run and returns None on every re-run.
#
#   table            conflict key                  option   why
#   repositories     (owner_norm, repo_norm)       3        the name never changes; its id is needed
#   issues           (repository_id, number)       2        title, body, labels can be edited; id needed
#   issue_comments   github_id                     2        a comment can be edited (its id isn't needed)
#   pull_requests    (repository_id, number)       2        title, body can be edited; id needed
#   pr_files         (pull_request_id, path)       1        a merged PR's files never change
#   issue_fixes      (issue_id, pull_request_id)   1        the link exists or it doesn't
#
#   The run records are the exception, on purpose. start_job adds a NEW row every
#   time: each run is an attempt of its own, and the rows are the run history
#   ("failed yesterday, succeeded today" is two rows). The two mark_* functions are
#   plain UPDATEs of one known row, so repeating one gives the same result.


def get_or_create_repository(conn, owner, repo):
    """Find or create the row for one repository; return its id and its synced_sha.

    IN   conn    an open psycopg connection; the caller commits
         owner   "encode"   as GitHub spells it
         repo    "httpx"

    OUT  (repo_id, synced_sha)
           repo_id      int, e.g. 1. The same id on every run for the same repo.
           synced_sha   str, the HEAD at the last successful ingest, e.g. "7cff5f79bc...".
                        None if the repo has never been ingested successfully.
                        Ingest compares it with today's HEAD to decide whether to skip.

    STEPS
      1. INSERT the row, ON CONFLICT DO NOTHING.
         First run: the row is created. Re-run: it already exists, so nothing happens.
      2. SELECT the row's id and synced_sha. The row exists by now either way.

    This is option 3 from RE-RUNS above: the name never changes, so there is nothing
    to update, but the id is needed by every other table.

    The key is the lowercased pair, because GitHub treats names as case-insensitive:
    Encode/HTTPX and encode/httpx are the same repo, so they must be one row.
    owner and repo keep GitHub's spelling, for API calls and display.

    Why not SELECT first, then INSERT if nothing is found? Two runs starting together would
    both SELECT, both find nothing, and both INSERT; the second INSERT crashes on the UNIQUE
    key. ON CONFLICT checks and inserts in one statement, so the second run skips instead.
    """
    with conn.cursor() as cur:
        # 1. Create the row the first time this repo is seen. On a re-run the lowercased
        #    pair is already there (a conflict), so the INSERT does nothing.
        #      first run   ("encode", "httpx", "encode", "httpx")   -> 1 row created
        #      re-run      the same values, conflict                -> nothing happens
        cur.execute(
            """
            INSERT INTO repositories (owner, repo, owner_norm, repo_norm)
            VALUES (%s, %s, lower(%s), lower(%s))
            ON CONFLICT (owner_norm, repo_norm) DO NOTHING
            """,
            (owner, repo, owner, repo),
        )

        # 2. Read the id and synced_sha. The row exists by now either way: just created,
        #    or there from before. fetchone() gives the one matching row as a tuple, and
        #    it is returned as it is, for the caller to unpack:
        #      a new repo              (1, None)
        #      ingested before         (1, "7cff5f79bc...")
        cur.execute(
            """
            SELECT id, synced_sha
            FROM repositories
            WHERE owner_norm = lower(%s) AND repo_norm = lower(%s)
            """,
            (owner, repo),
        )
        return cur.fetchone()


def start_job(conn, repo_id, commit_sha):
    """Record that an ingest run has started, and return the job's id.

    IN   conn         an open psycopg connection; the caller commits
         repo_id      int, from get_or_create_repository()
         commit_sha   str, the HEAD this run saw, from github.fetch_head_sha()

    OUT  job_id   int, e.g. 12. The caller keeps it, to close this same row when the
                  run ends.

    STEPS
      1. INSERT a new row: status 'in_progress', started_at now. RETURNING id.

    Not idempotent, on purpose: every run gets its own row (see RE-RUNS above).

    status is 'in_progress' because the run starts right away. 'queued' is for
    later, when the API writes the row and a separate worker picks it up.

    conclusion, the counts, error and finished_at stay NULL here.
    mark_job_completed() writes them when the run ends.
    """
    with conn.cursor() as cur:
        # 1. A new row for this run. now() is the time this transaction (T1) began, which
        #    is the start of the run. If the run succeeds, it becomes the repo's synced_at.
        #    RETURNING id gives the new row's id: a plain INSERT always returns it.
        cur.execute(
            """
            INSERT INTO ingest_jobs (repository_id, commit_sha, status, started_at)
            VALUES (%s, %s, 'in_progress', now())
            RETURNING id
            """,
            (repo_id, commit_sha),
        )
        return cur.fetchone()[0]


def store_issue(conn, repo_id, issue):
    """Insert one fixed issue, or update it if it's already stored, and return its id.

    IN   conn      an open psycopg connection; the caller commits
         repo_id   int, from get_or_create_repository()
         issue     one issue dict from fetch_closed_issues(). Only these keys are used:
                     {"number":    2413,
                      "title":     "Timeout not applied to ...",
                      "body":      "When I set ...",       ("" when the reporter wrote nothing)
                      "createdAt": "2023-05-02T10:15:00Z",
                      "labels":    {"nodes": [{"name": "bug"}, {"name": "http/2"}]}}

    OUT  issue_id   int, e.g. 57. The same id on every run for the same issue.

    STEPS
      1. Flatten the labels: {"nodes": [{"name": "bug"}, ...]} becomes ["bug", ...].
      2. INSERT the row. If the issue is already stored, DO UPDATE the columns that can
         change. RETURNING id gives the id either way.

    This is option 2 from RE-RUNS above: title, body and labels can be edited on GitHub
    after the last run, and the id is needed by issue_comments and issue_fixes.
    """
    # 1. GitHub wraps each label as {"name": ...} inside "nodes". The labels column is a
    #    plain list of names, and psycopg sends a Python list as a Postgres text[] array.
    #      {"nodes": [{"name": "bug"}, {"name": "http/2"}]}   ->   ["bug", "http/2"]
    #      {"nodes": []}                                       ->   []  (stored as '{}')
    labels = [label["name"] for label in issue["labels"]["nodes"]]

    with conn.cursor() as cur:
        # 2. First run: the row is inserted. Re-run: (repository_id, number) already exists,
        #    so DO UPDATE overwrites title, body and labels with what GitHub says now.
        #      EXCLUDED     the row this INSERT tried to write but failed due to conflict is named as excluded, i.e. the new values
        #      created_at   not updated: when an issue was opened never changes
        #      RETURNING    gives the row's id in both cases, inserted or updated
        #    createdAt goes in as GitHub's string; Postgres reads it into a timestamptz.
        cur.execute(
            """
            INSERT INTO issues (repository_id, number, title, body, labels, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (repository_id, number) DO UPDATE
               SET title  = EXCLUDED.title,
                   body   = EXCLUDED.body,
                   labels = EXCLUDED.labels
            RETURNING id
            """,
            (repo_id, issue["number"], issue["title"], issue["body"], labels, issue["createdAt"]),
        )
        return cur.fetchone()[0]
    
    
def store_comments(conn, issue_id, comments):
    """Insert one issue's comments, or update the ones already stored.

    IN   conn       an open psycopg connection; the caller commits
         issue_id   int, from store_issue()
         comments   list of comment dicts, already filtered by ingest (no bots).
                    Only these keys are used:
                      [{"fullDatabaseId": "1859302231",
                        "body":           "The bug is in ...",
                        "createdAt":      "2023-05-03T08:00:00Z"},
                       ...]

    OUT  nothing. Nothing points at a comment, so no id is needed.

    STEPS
      1. Turn each comment dict into one tuple of values, in the INSERT's column order.
      2. executemany(): run the same INSERT once per tuple. If a comment is already
         stored, DO UPDATE its body.

    This is option 2 from RE-RUNS above, without RETURNING: a comment can be edited
    on GitHub, so a re-run overwrites its body, but its id isn't needed.
    """
    # 1. One tuple per comment, in the INSERT's column order.
    #    fullDatabaseId arrives as a string ("1859302231"); int() turns it into the
    #    number the bigint column holds.
    #      {"fullDatabaseId": "1859302231", "body": "...", "createdAt": "2023-..."}
    #        ->  (1859302231, 57, "...", "2023-...")
    rows = [
        (int(comment["fullDatabaseId"]), issue_id, comment["body"], comment["createdAt"])
        for comment in comments
    ]

    with conn.cursor() as cur:
        # 2. executemany() runs this INSERT once per tuple in rows; an empty list runs nothing.
        #    On a re-run the github_id exists, so the INSERT conflicts and inserts nothing.
        #    EXCLUDED = the row it couldn't insert; SET copies its body into the stored row.
        cur.executemany(
            """
            INSERT INTO issue_comments (github_id, issue_id, body, created_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (github_id) DO UPDATE
               SET body = EXCLUDED.body
            """,
            rows,
        )


def store_pull_request(conn, repo_id, pr):
    """Insert one fix PR, or update it if it's already stored, and return its id.

    IN   conn      an open psycopg connection; the caller commits
         repo_id   int, from get_or_create_repository()
         pr        one PR dict from an issue's closedByPullRequestsReferences, already
                   checked by ingest (merged, and from this repo). Only these keys are used:
                     {"number":      2415,
                      "title":       "Apply pool timeout ...",
                      "body":        "Fixes #2413 ...",      ("" when the author wrote nothing)
                      "mergedAt":    "2023-05-04T12:00:00Z",
                      "mergeCommit": {"oid": "a1b2c3d4..."}}   (the SHA, 40 hex characters)

    OUT  pr_id   int, e.g. 31. The same id on every run for the same PR.

    STEPS
      1. Take the SHA out of mergeCommit: {"oid": "a1b2..."} becomes "a1b2...".
      2. INSERT the row. If the PR is already stored, DO UPDATE the columns that can
         change. RETURNING id gives the id either way.

    This is option 2 from RE-RUNS above: title and body can be edited on GitHub after
    the last run, and the id is needed by pr_files and issue_fixes.

    One PR can fix several issues, so ingest may call this twice for the same PR in one
    run. The second call hits the conflict too, and gets back the same id.
    """
    # 1. GitHub wraps the SHA as {"oid": ...} inside mergeCommit; the column holds just the SHA.
    #      {"oid": "a1b2c3d4..."}   ->   "a1b2c3d4..."
    merge_commit_sha = pr["mergeCommit"]["oid"]

    with conn.cursor() as cur:
        # 2. First run: the row is inserted. Re-run: (repository_id, number) already exists,
        #    so the INSERT conflicts and inserts nothing.
        #    EXCLUDED = the row it couldn't insert; its title and body replace the old PR's.
        #    merged_at and merge_commit_sha aren't updated: once merged, they never change.
        cur.execute(
            """
            INSERT INTO pull_requests
                   (repository_id, number, title, body, merged_at, merge_commit_sha)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (repository_id, number) DO UPDATE
               SET title = EXCLUDED.title,
                   body  = EXCLUDED.body
            RETURNING id
            """,
            (repo_id, pr["number"], pr["title"], pr["body"], pr["mergedAt"], merge_commit_sha),
        )
        return cur.fetchone()[0]


def store_pr_files(conn, pr_id, files):
    """Insert the files one fix PR changed, skipping the ones already stored.

    IN   conn    an open psycopg connection; the caller commits
         pr_id   int, from store_pull_request()
         files   the PR's list of file dicts, pr["files"]["nodes"]:
                   [{"path": "httpx/_client.py",       "changeType": "MODIFIED"},
                    {"path": "tests/test_timeouts.py", "changeType": "ADDED"},
                    ...]

    OUT  nothing. Nothing points at a pr_files row, so no id is needed.

    STEPS
      1. Turn each file dict into one tuple of values, with changeType lowercased.
      2. executemany(): run the same INSERT once per tuple. If a file is already
         stored, DO NOTHING.

    This is option 1 from RE-RUNS above: a merged PR's files never change, so there's
    nothing to update, and no id is needed.

    One PR can fix several issues, so ingest may call this twice for the same PR in one
    run. The second call conflicts on every file and inserts nothing.
    """
    # 1. One tuple per file, in the INSERT's column order.
    #    GitHub sends changeType in capitals; the column's CHECK allows only lowercase.
    #      {"path": "httpx/_client.py", "changeType": "MODIFIED"}
    #        ->  (31, "httpx/_client.py", "modified")
    rows = [(pr_id, file["path"], file["changeType"].lower()) for file in files]

    with conn.cursor() as cur:
        # 2. executemany() runs this INSERT once per tuple in rows (no files = no INSERTs).
        #    On a re-run (pull_request_id, path) exists, so the INSERT conflicts, and
        #    DO NOTHING leaves the old row as it is.
        cur.executemany(
            """
            INSERT INTO pr_files (pull_request_id, path, change_type)
            VALUES (%s, %s, %s)
            ON CONFLICT (pull_request_id, path) DO NOTHING
            """,
            rows,
        )


def store_fix(conn, issue_id, pr_id):
    """Record that one PR fixed one issue, unless that link is already stored.

    IN   conn       an open psycopg connection; the caller commits
         issue_id   int, from store_issue()
         pr_id      int, from store_pull_request()

    OUT  nothing. Nothing points at a link, so no id is needed.

    STEPS
      1. INSERT the (issue_id, pr_id) pair. If it's already stored, DO NOTHING.

    This is option 1 from RE-RUNS above: a link has no columns to edit. It either
    exists or it doesn't.

    Its own table because the link is many-to-many: issue #2413 can be fixed by two
    PRs, and PR #2415 can fix two issues. Each pair is one row:
        issue_id   pull_request_id
        57         31                 PR #2415 fixed issue #2413
        57         32                 PR #2420 also fixed issue #2413
        58         31                 PR #2415 also fixed issue #2414
    """
    with conn.cursor() as cur:
        # 1. First run: the pair is inserted. Re-run: (issue_id, pull_request_id) already
        #    exists, so the INSERT conflicts, and DO NOTHING leaves the old row as it is.
        cur.execute(
            """
            INSERT INTO issue_fixes (issue_id, pull_request_id)
            VALUES (%s, %s)
            ON CONFLICT (issue_id, pull_request_id) DO NOTHING
            """,
            (issue_id, pr_id),
        )


def mark_repository_synced(conn, job_id):
    """Record that the repo's history is now synced, as of the given run.

    IN   conn     an open psycopg connection; the caller commits
         job_id   int, from start_job(): the run that just stored the history

    OUT  nothing

    STEPS
      1. Copy the job's commit_sha and started_at onto its repository, as synced_sha
         and synced_at.

    Called only on success, in T2, in the same transaction as the history itself. So
    synced_sha and the stored history can never disagree: they commit together, or
    neither does. A failed run never gets here, and the repo keeps its old values.

    Takes only the job's id: the job row already holds the repo, the HEAD and the
    start time, so nothing has to be passed twice, and nothing can be mismatched.
    """
    with conn.cursor() as cur:
        # 1. FROM ingest_jobs brings this run's job row into the UPDATE, so its values
        #    can be copied. The WHERE picks that job row (by its id), and the repo it
        #    belongs to (the one whose id is the job's repository_id).
        #      job 12:   repository_id=1   commit_sha="7cff5f79bc..."   started_at=10:00
        #      repo 1:   synced_sha -> "7cff5f79bc..."                  synced_at -> 10:00
        #    started_at, not the time now: a fix merged while this run was fetching is
        #    newer than started_at, so a later sync since synced_at will still find it.
        cur.execute(
            """
            UPDATE repositories
               SET synced_sha = ingest_jobs.commit_sha,
                   synced_at  = ingest_jobs.started_at
              FROM ingest_jobs
             WHERE ingest_jobs.id = %s
               AND repositories.id = ingest_jobs.repository_id
            """,
            (job_id,),
        )


def mark_job_completed(conn, job_id, conclusion, issues_kept=None, issues_dropped=None,
                       error=None):
    """Record that one ingest run has ended, and how it ended.

    IN   conn             an open psycopg connection; the caller commits
         job_id           int, from start_job()
         conclusion       'success'    the history was fetched and stored
                          'skipped'    HEAD hadn't moved since the last success
                          'failure'    something broke; error says what
                          'cancelled'  stopped by Ctrl-C
         issues_kept      int, on success only: the issues stored
         issues_dropped   int, on success only: the issues the INGEST RULES dropped
         error            str, on failure and cancelled only, e.g.
                          "GitHubError: [{'type': 'NOT_FOUND', ...}]"

    OUT  nothing

    STEPS
      1. UPDATE the job row: status 'completed', the conclusion, the counts, the
         error, and finished_at.

    Called on every way out of a run, so status is always 'completed'; conclusion is
    what differs:
      skipped     in T1, before anything was fetched
      success     in T2, in the same transaction as the history
      failure     in T3, after the rollback
      cancelled   in T3, after the rollback

    In T3 it MUST run after the rollback. The failed transaction is aborted, and
    Postgres refuses every statement in it, this one included, until it is rolled back.
    """
    with conn.cursor() as cur:
        # 1. Close the row. Arguments that weren't passed stay None, which is stored as NULL.
        #    error is cut to 2,000 characters: it is a short line for whoever polls the
        #    job, not a full traceback.
        #    clock_timestamp(), not now(): now() is when this transaction began, which in
        #    T2 is before the history writes. clock_timestamp() is the actual time.
        cur.execute(
            """
            UPDATE ingest_jobs
               SET status         = 'completed',
                   conclusion     = %s,
                   issues_kept    = %s,
                   issues_dropped = %s,
                   error          = %s,
                   finished_at    = clock_timestamp()
             WHERE id = %s
            """,
            (conclusion, issues_kept, issues_dropped, error[:2000] if error else None, job_id),
        )

