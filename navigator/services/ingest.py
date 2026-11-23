# services/ingest.py : the orchestrator for fixed-issue history.
#
# fetch -> filter -> store, for one GitHub repository, recorded as one ingest job.
# Each stage is its own module:
#
#   pipeline/fetch_history.py    FETCH    the repo's HEAD, and every closed issue, unfiltered
#   pipeline/filter_history.py   FILTER   the INGEST RULES: keep only issues with a real fix
#   db/store_history.py          STORE    every write, idempotent; never commits
#
# This file only orchestrates. It calls the stages in order, and it is the only
# module that commits: every decision about what succeeds or fails together lives here.
#
# ingest_repository() is handed its connections, so the API will call this same
# function later. Until then, run it from issue-navigator/:
#   python -m navigator.services.ingest encode/httpx            skipped if nothing was merged
#   python -m navigator.services.ingest encode/httpx --force    fetches anyway
#
#
# THE FLOW
#
#   __main__     reads "encode/httpx" (and --force) from the command line, loads .env,
#                opens the GitHub client and the database connection
#      |
#   ingest_repository(conn, gh, "encode", "httpx", force=False)
#
#         fetch_head_sha()                     the repo's HEAD now; 1 point, no transaction
#
#     T1  get_or_create_repository()           -> (repo_id, synced_sha)
#         start_job(head)                      -> job_id, status 'in_progress'
#         HEAD == synced_sha and not force?    -> job 'skipped', COMMIT, return
#         COMMIT                               the job row is visible from here on
#
#         fetch_closed_issues()                every closed issue, unfiltered;
#                                              1-2 minutes, no transaction open
#
#     T2  for each issue:
#           find_fix_prs()                     rules 2-5, 7, 8: [] means drop the issue
#           store_issue()                      -> issue_id
#           store_comments(human_comments())   rule 6: bot comments removed
#           for each fix PR:
#             store_pull_request()             -> pr_id
#             store_pr_files()                 the files that PR changed (the answer key)
#             store_fix()                      the issue <-> PR link
#         mark_repository_synced(job_id)       the repo's synced_sha and synced_at
#         mark_job_completed('success')        with the kept and dropped counts
#         COMMIT                               all of it, or none of it
#
#     T3  on any error:  ROLLBACK, job 'failure' with the error, COMMIT, raise
#         on Ctrl-C:     ROLLBACK, job 'cancelled',              COMMIT, raise
#
#
# FUNCTIONS
#
#   ingest_repository   the flow above; owns the three transactions
#
#
# WHY IT IS SHAPED THIS WAY
#
#   T1 commits on its own, before the long work. The job row then exists for the
#   API to poll, and it survives a failed run, so the failure can be recorded on it.
#
#   Both GitHub calls happen outside any transaction. The slow part is the network,
#   so it is over before the first write, and T2 itself takes only a few seconds.
#
#   T2 is all or nothing. A crash halfway rolls all of it back, so the database never
#   holds half a run, and a briefing asked during a run reads the previous history.
#
#   T3 rolls back first. After an error, Postgres refuses every statement in the
#   failed transaction until it is rolled back; only then can the failure be recorded.
#   Ctrl-C raises KeyboardInterrupt, which isn't an Exception, so it needs its own
#   except. Without it, the job would stay 'in_progress' forever.
#
#   Running again is the recovery. Every history write is idempotent (see
#   store_history.py), and a whole httpx run costs about 150 of the hourly 5,000
#   points and a minute or two.
#
#
# SKIP AND --force
#
#   Every stored fix is a merged PR (rule 3 in filter_history.py), and a merge moves
#   HEAD. So if HEAD is still the synced_sha of the last successful run, there are no
#   new fixed issues, and the run ends as 'skipped' without fetching anything.
#
#   Some changes don't move HEAD: new comments on old issues, relabels, a closing
#   link added after the merge. They wait for the next commit, or for --force, which
#   fetches anyway.

import os                         # os.environ: the process's environment variables
import sys                        # sys.argv: the command line, as a list of strings

from dotenv import load_dotenv    # reads .env into os.environ
import psycopg                    # Python to Postgres driver

from navigator.pipeline.github_client import client                                   # GitHub
from navigator.pipeline.fetch_history import fetch_head_sha, fetch_closed_issues      # FETCH
from navigator.pipeline.filter_history import find_fix_prs, human_comments            # FILTER
from navigator.db.store_history import (                                              # STORE
    get_or_create_repository, start_job,
    store_issue, store_comments, store_pull_request, store_pr_files, store_fix,
    mark_repository_synced, mark_job_completed,
)


def ingest_repository(conn, gh, owner, repo, force=False):
    """Fetch, filter and store one repository's fixed-issue history, as one ingest job.

    IN   conn    an open psycopg connection. Handed in, so this function owns the
                 transactions (it commits and rolls back), but not the connection's lifetime.
         gh      the GitHub client from github_client.client(), also handed in
         owner   "encode"
         repo    "httpx"
         force   False: skip the run if HEAD hasn't moved since the last successful one.
                 True:  fetch anyway, for changes that don't move HEAD (new comments, relabels).

    OUT  nothing. The outcome is in the database: one ingest_jobs row, closed as
         'success' (with the kept and dropped counts), 'skipped', 'failure' (with the
         error) or 'cancelled'. After recording a failure or a Ctrl-C, it raises again.

    STEPS
      1. Read the repo's HEAD from GitHub. No transaction yet.
      2. T1: find or create the repo's row, and open a job row for this run.
      3. T1: HEAD unchanged and not forced? Close the job as 'skipped', commit, stop.
      4. T1: commit, so the job row survives whatever happens next.
      5. Fetch every closed issue. No transaction open.
      6. T2: for each issue, apply the rules, and store the issues that are kept.
      7. T2: mark the repo as synced, close the job as 'success', commit.
      8. T3: on any error, roll back, close the job as 'failure', commit, raise.
             On Ctrl-C, the same, as 'cancelled'.
    """
    # 1. HEAD is read before anything is written. If this fails (an unknown repo, a bad
    #    token), no repository row and no job row is created for a repo that may not
    #    exist; the error goes straight to the caller.
    head = fetch_head_sha(gh, owner, repo)

    # 2. T1 begins with the first statement below: psycopg opens a transaction by itself
    #    on the first execute, and it stays open until commit() or rollback().
    repo_id, synced_sha = get_or_create_repository(conn, owner, repo)
    job_id = start_job(conn, repo_id, head)

    # 3. Nothing was merged since the last successful run, so there are no new fixed
    #    issues and nothing to fetch. The job is closed as 'skipped' in the same T1, so
    #    the row still shows that a run happened, and why it did nothing.
    #    On a repo's first run synced_sha is None, which never equals a sha: no skip.
    if head == synced_sha and not force:
        mark_job_completed(conn, job_id, "skipped")
        conn.commit()
        print(f"{owner}/{repo}: HEAD {head[:7]} is already synced, skipped")
        return

    # 4. T1 ends here. From now on the job row exists, as 'in_progress', for whoever
    #    polls it, and it survives a failure, so T3 can record what went wrong.
    conn.commit()

    try:
        # 5. Fetch every closed issue: 1-2 minutes of GitHub requests, with no transaction
        #    open. issues is a list of issue dicts, unfiltered.
        issues = fetch_closed_issues(gh, owner, repo)

        # 6. T2 begins with the first write below. For each issue, the rules decide:
        #      find_fix_prs() returns []   -> dropped: nothing is stored for it
        #      it returns the fix PRs      -> kept: the issue, its human comments, and for
        #                                     each fix PR: the PR, all its files, the link
        kept = 0
        dropped = 0
        for issue in issues:
            fix_prs = find_fix_prs(issue, owner, repo)
            if not fix_prs:
                dropped += 1
                continue

            issue_id = store_issue(conn, repo_id, issue)
            store_comments(conn, issue_id, human_comments(issue))
            for pr in fix_prs:
                pr_id = store_pull_request(conn, repo_id, pr)
                store_pr_files(conn, pr_id, pr["files"]["nodes"])    # every file (rule 7)
                store_fix(conn, issue_id, pr_id)
            kept += 1

        # 7. T2 ends here. The history, the repo's new synced_sha and the closed job all
        #    become visible in the same instant, or none of them do.
        mark_repository_synced(conn, job_id)
        mark_job_completed(conn, job_id, "success", issues_kept=kept, issues_dropped=dropped)
        conn.commit()
        print(f"{owner}/{repo}: {kept} issues kept, {dropped} dropped, synced at {head[:7]}")

    # 8. T3: anything raised in steps 5-7, e.g. GitHub failing halfway through the fetch,
    #    or a write that breaks. The transaction is aborted, and Postgres refuses every
    #    statement in it until it is rolled back. So: roll back first (the half-written
    #    history is gone, the repo keeps its old synced_sha), then record the failure in a
    #    new transaction, commit it, and raise again so the caller sees the error too.
    #    (If the fetch failed, no transaction was open, and the rollback does nothing.)
    except Exception as e:
        conn.rollback()
        mark_job_completed(conn, job_id, "failure", error=f"{type(e).__name__}: {e}")
        conn.commit()
        raise

    # Ctrl-C raises KeyboardInterrupt, which isn't an Exception, so the block above never
    # sees it. Without this block the job would stay 'in_progress' forever.
    # 'cancelled', not 'failure': nothing broke, someone stopped it.
    except KeyboardInterrupt:
        conn.rollback()
        mark_job_completed(conn, job_id, "cancelled", error="interrupted")
        conn.commit()
        raise


if __name__ == "__main__":
    # 1. Read the command line. sys.argv[0] is always the script itself, so the real
    #    arguments start at sys.argv[1]:
    #      python -m navigator.services.ingest encode/httpx --force
    #      sys.argv[1:] = ["encode/httpx", "--force"]
    #    --force can come anywhere; whatever else is left must be one "owner/repo".
    args = sys.argv[1:]
    force = "--force" in args
    names = [arg for arg in args if arg != "--force"]
    if len(names) != 1 or names[0].count("/") != 1:
        print("usage: python -m navigator.services.ingest <owner>/<repo> [--force]")
        sys.exit(1)
    owner, repo = names[0].split("/")              # "encode/httpx" -> "encode", "httpx"

    # 2. .env -> os.environ, so the GitHub token and the database URL can be read.
    load_dotenv()

    # 3. Open both connections for the whole run, and hand them to ingest_repository().
    #    Both close when the with-block ends, whether the run succeeded or raised.
    #    ingest_repository() commits and rolls back by itself; the with-block only
    #    owns the connections' lifetime.
    with client() as gh, psycopg.connect(os.environ["NAVIGATOR_DATABASE_URL"]) as conn:
        ingest_repository(conn, gh, owner, repo, force=force)
