# pipeline/filter_history.py : decides which closed issues were really fixed, and by
# which PRs. The FILTER stage of ingest.
#
# Pipeline:  fetch_history -> filter_history -> store_history, run by services/ingest.py.
#
# Takes the issue dicts that fetch_history.py returns, unfiltered, and applies the
# INGEST RULES below: which issues count as fixed, which PRs fixed them, and which
# comments are kept. This is the only module that knows the rules.
#
# Pure: dicts in, lists out. No GitHub, no database, and no imports from Navigator,
# so every rule can be tested with a dict written by hand.
#
#
# FUNCTIONS
#
#   find_fix_prs     one issue -> the PRs that count as its fix, or [] to drop it
#   human_comments   one issue -> its comments, without the bots'   (next to write)
#
#
# INGEST RULES: what ingest keeps from fetch_closed_issues(), and the value that decides
#
#   Rule 1: only closed issues
#     Decided by:  states: CLOSED in fetch_history.py's query. GitHub never sends open issues.
#                  (Comments on PRs are never asked for either.)
#
#   Rule 2: only issues closed as completed
#     Decided by:  issue["stateReason"] == "COMPLETED"
#     Dropped:     "NOT_PLANNED" (won't fix), "DUPLICATE"
#
#   Rule 3: a PR counts as a fix only if it was merged
#     Decided by:  pr["mergedAt"] is not None
#     Dropped:     open PRs (mergedAt is None)
#
#   Rule 4: a PR counts as a fix only if it was merged into THIS repo
#     Decided by:  pr["baseRepository"]["nameWithOwner"].lower() == f"{owner}/{repo}".lower()
#                  e.g. "encode/httpx". Lowercase on both sides, because GitHub names
#                  ignore case.
#     Dropped:     PRs from other repos, e.g. encode/httpcore
#
#   Rule 5: an issue is kept only if it has at least one fix PR (a PR passing rules 3 and 4)
#     Decided by:  the PRs in issue["closedByPullRequestsReferences"]["nodes"] that pass
#                  rules 3 and 4. At least one must be left.
#     Dropped:     COMPLETED issues closed by hand, fixed by a commit pushed without a PR,
#                  or fixed only in another repo. Issues closed before 2022 all say
#                  COMPLETED, so this rule is what proves a real fix.
#
#   Rule 6: comments written by bots are skipped
#     Decided by:  comment["author"]["__typename"] == "Bot"
#     Kept:        comments whose author is None. GitHub sends None when the account
#                  has since been deleted; it is treated as a person's.
#
#   Rule 7: every file of a fix PR is saved, whatever its changeType
#     Decided by:  nothing is filtered: all of pr["files"]["nodes"]. Added files too;
#                  scoring, not ingest, decides which files were findable.
#
#   Rule 8: an issue is dropped if its PR list, or a fix PR's file list, was cut
#     Decided by:  issue["closedByPullRequestsReferences"]["pageInfo"]["hasNextPage"] == True
#                      (over 10 linked PRs: a fix PR could be among the missing ones)
#                  pr["files"]["pageInfo"]["hasNextPage"] == True
#                      (a fix PR changed over 100 files: some of its files are missing)
#     Why:         either way the answer key would be incomplete, so the issue can never
#                  be a fair test. Dropping it means nothing has to remember it later.
#                  Both cases are rare.
#     Comments are different: only the first 100 are fetched, and that is accepted.
#     They are context, not the answer key, so the query doesn't check if they were cut.
#
#   Rule 9: no time filter at ingest
#     Everything that passes rules 1-8 is saved. "Use only history older than the issue
#     being answered" is applied later, when history is read.
#
#   What gets saved for each kept issue:
#     issues          number, title, body, labels, createdAt
#     pull_requests   each fix PR: number, title, body, mergedAt, mergeCommit.oid
#                     (a PR that fixed several issues is saved once)
#     issue_fixes     one row per (issue, fix PR) pair
#     pr_files        every file each fix PR changed: path, changeType
#     issue_comments  every non-bot comment (of the first 100): fullDatabaseId, body, createdAt


def find_fix_prs(issue, owner, repo):
    """Return the PRs that count as this issue's fix, or [] to drop the issue.

    IN   issue   one issue dict from fetch_closed_issues(). Only these keys are used:
                   {"stateReason": "COMPLETED",
                    "closedByPullRequestsReferences": {
                      "pageInfo": {"hasNextPage": False},
                      "nodes": [
                        {"number":         2415,
                         "mergedAt":       "2023-05-04T12:00:00Z",   (None = never merged)
                         "baseRepository": {"nameWithOwner": "encode/httpx"},
                         "files":          {"pageInfo": {"hasNextPage": False},
                                            "nodes": [...]},
                         ...},
                        ...]}}
         owner   "encode"
         repo    "httpx"

    OUT  list of the fix PRs' dicts, exactly as GitHub sent them, e.g. [{"number": 2415, ...}]
         [] means: drop this issue and store nothing for it.

    STEPS
      1. Rule 2: not closed as completed                         -> []
      2. Rule 8: the issue's PR list was cut (over 10 PRs)       -> []
      3. Rules 3 and 4: keep the PRs that were merged, into this repo.
      4. Rule 8: a fix PR's file list was cut (over 100 files)   -> []
      5. Rule 5: return what's left. Empty means no real fix, so the issue is dropped.

    Pure: it reads one dict and returns a list. No GitHub, no database, so it can be
    tested with a dict written by hand.
    """
    # 1. Rule 2: only issues closed as completed. "NOT_PLANNED" (won't fix) and
    #    "DUPLICATE" have no fix to learn from.
    if issue["stateReason"] != "COMPLETED":
        return []

    # 2. Rule 8: more than 10 PRs are linked, so GitHub sent only the first 10. The real
    #    fix could be among the missing ones, and the answer key would be incomplete.
    linked = issue["closedByPullRequestsReferences"]     # the PRs linked as closing it
    if linked["pageInfo"]["hasNextPage"]:
        return []

    # 3. Rules 3 and 4: a PR counts as a fix only if it was merged, and merged into THIS
    #    repo. Lowercase on both sides, because GitHub names ignore case.
    #    baseRepository is None if that repo no longer exists: not this repo, so skipped.
    this_repo = f"{owner}/{repo}".lower()                # "encode/httpx"
    fix_prs = []
    for pr in linked["nodes"]:
        merged = pr["mergedAt"] is not None
        into_this_repo = (pr["baseRepository"] is not None
                          and pr["baseRepository"]["nameWithOwner"].lower() == this_repo)
        if merged and into_this_repo:
            fix_prs.append(pr)

    # 4. Rule 8: a fix PR changed more than 100 files, so GitHub sent only the first 100.
    #    Part of the answer key is missing, so the issue could never be a fair test.
    #    Only fix PRs are checked: a cut list on a PR that isn't a fix doesn't matter.
    for pr in fix_prs:
        if pr["files"]["pageInfo"]["hasNextPage"]:
            return []

    # 5. Rule 5: what's left are the fix PRs. [] means no real fix (closed by hand, fixed
    #    by a commit pushed without a PR, or fixed only in another repo): drop the issue.
    return fix_prs
