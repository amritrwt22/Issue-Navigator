"""fetch_history.py: fetches a repo's history from GitHub. The FETCH stage of ingest.

What it does:
  fetch_head_sha()        the commit the repo's default branch points at (its HEAD)
  fetch_closed_issues()   every closed issue, with its comments, linked PRs and their files

Pipeline:  fetch_history -> filter_history -> store_history, run by services/ingest.py.

It returns GitHub's data as plain dicts, unfiltered. It does NOT decide what to keep
(filter_history.py does that), and it never touches the database. Every query goes
through github_client.graphql().
"""

from navigator.pipeline.github_client import graphql


# The query for HEAD: the commit the repo's default branch points at right now.
# Ingest sends it first, before CLOSED_ISSUES_QUERY. If HEAD hasn't moved since the
# last successful run, nothing was merged, so there are no new fixed issues and the
# run is skipped. It costs 1 point.
HEAD_SHA_QUERY = """
query($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    defaultBranchRef {          # the default branch, e.g. "master"
      target { oid }            # the commit that branch points at: HEAD
    }
  }
}
"""


def fetch_head_sha(gh, owner, repo):
    """Return the sha of the commit the repo's default branch points at: its HEAD.

    IN   gh      the client from github_client.client()
         owner   "encode"
         repo    "httpx"

    OUT  str, the commit's 40-character sha, e.g. "4b8f6e8d0c...".
         Ingest compares it with the repo's synced_sha to decide whether to skip.

    STEPS
      1. Send HEAD_SHA_QUERY with owner and repo.
      2. Take the sha out of the response, four levels down.

    Raises whatever graphql() raises (an unknown repo: GitHubError). A repo with no
    commits at all has no default branch, so defaultBranchRef is None and step 2
    raises TypeError. Such a repo has no history to ingest anyway.
    """
    # 1. Send the query.
    #    data = {"repository": {"defaultBranchRef": {"target": {"oid": "4b8f6e8d0c..."}}}}
    data = graphql(gh, HEAD_SHA_QUERY, {"owner": owner, "repo": repo})

    # 2. Walk down to the sha:
    #      data["repository"]           the repo
    #          ["defaultBranchRef"]     its default branch
    #          ["target"]               the commit that branch points at
    #          ["oid"]                  that commit's sha
    return data["repository"]["defaultBranchRef"]["target"]["oid"]


# The main query ingest needs: a page of 25 closed issues, with everything Navigator
# stores nested inside each. Sent once per page; only $cursor changes.
# 25, not the maximum of 100: nested lists multiply (25 issues x 10 PRs x 100 files),
# and smaller pages stay well under GitHub's 10-second limit per request.
# The # comments inside are GraphQL comments: GitHub reads past them.
CLOSED_ISSUES_QUERY = """
query($owner: String!, $repo: String!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    issues(first: 25, after: $cursor, states: CLOSED) {
      pageInfo { hasNextPage endCursor }        # more pages? + the bookmark
      nodes {
        number  title  body  createdAt          # -> issues
        stateReason                             # COMPLETED / NOT_PLANNED / DUPLICATE: ingest keeps COMPLETED
        labels(first: 20) { nodes { name } }    # -> issues.labels
        comments(first: 100) {                  # the first 100 only: context, not the answer key
          nodes {
            fullDatabaseId  body  createdAt     # -> issue_comments (id as a string: too big for Int)
            author { __typename }               # "User" or "Bot": ingest skips bots
          }
        }
        closedByPullRequestsReferences(first: 10) {    # the PRs linked as closing it
          pageInfo { hasNextPage }              # true = over 10 PRs, list was cut: ingest drops the issue
          nodes {
            number  title  body  mergedAt       # -> pull_requests; mergedAt null = never merged
            mergeCommit { oid }                 # -> merge_commit_sha
            baseRepository { nameWithOwner }    # "encode/httpx"? ingest skips PRs from other repos
            files(first: 100) {
              pageInfo { hasNextPage }          # true = over 100 files, list was cut: ingest drops the issue
              nodes { path  changeType }        # -> pr_files: the answer key
            }
          }
        }
      }
    }
  }
}
"""

# --- How to read CLOSED_ISSUES_QUERY ---------------------------------------------------
#
# SYNTAX: each kind of line in the query, and what it becomes in the response
#
#                     in the query                  in the response (a Python dict)
#
#   a field           title                    ->   "title": "Timeout not applied ..."
#
#   an object         mergeCommit { oid }      ->   "mergeCommit": {"oid": "3f2a..."}
#
#   a list            labels(first: 20) {      ->   "labels": {
#                       nodes { name }                "nodes": [{"name": "bug"}, ...]
#                     }                             }
#
#   a list, with      files(first: 100) {      ->   "files": {
#   info about it       pageInfo { hasNextPage }      "pageInfo": {"hasNextPage": False},
#                       nodes { path }                "nodes": [{"path": "..."}, ...]
#                     }                             }
#
#   arguments         issues(first: 25, after: $cursor, states: CLOSED)
#                     pick and limit the list; $cursor is filled from "variables"
#
#   - A list is never a bare list. It is a dict, and the list itself is under "nodes".
#   - Each item in "nodes" is a dict: one issue, label, comment, PR or file.
#   - "pageInfo" sits next to "nodes", not inside it, and only if the query asks for it.
#   - first: N is the most GitHub sends, not an exact count: 3 comments give 3 items.
#
#   Reading it in Python:
#     issue["title"]                                   "Timeout not applied ..."
#     issue["labels"]["nodes"][0]["name"]              "bug"
#     issue["comments"]["nodes"]                       the list of comments
#     pr = issue["closedByPullRequestsReferences"]["nodes"][0]  the first closing PR
#     pr["files"]["pageInfo"]["hasNextPage"]           True if over 100 files (the list was cut)
#
# FIELDS: what we worked out about them
#
#   states: CLOSED
#     Short for states: [CLOSED], because "states" takes a list. An issue is only ever
#     OPEN or CLOSED; the reason it was closed is a separate field, stateReason.
#
#   stateReason
#     Why the issue was closed: COMPLETED, NOT_PLANNED, DUPLICATE or REOPENED.
#     Ingest keeps only COMPLETED issues. Every saved issue would have the same value,
#     so the issues table has no column for it.
#     Issues closed before 2022 all show COMPLETED, even ones never fixed, so ingest
#     also requires a merged PR.
#
#   author { __typename }
#     __typename is a field every GraphQL object has: it says what kind of object it
#     is, e.g. "User" or "Bot". Ingest skips comments written by a "Bot".
#
#   closedByPullRequestsReferences
#     - The PRs in the issue's "Development" sidebar, linked by "Fixes #2413" or by hand.
#     - GitHub returns merged PRs and open PRs; it leaves out PRs closed without merging.
#     - Ingest keeps a PR only if mergedAt has a date. null = still open, never merged.
#     - e.g. #2413 links PR #2415 (merged: kept) and PR #2430 (open, mergedAt null: skipped).
#
#   mergeCommit { oid }
#     The SHA of the commit that merged the PR: 40 hex characters.
#
#   baseRepository
#     The repo the PR was merged into. A PR in another repo can close our issue, e.g.
#     an encode/httpcore PR saying "Fixes encode/httpx#2413". Its files don't exist in
#     httpx, so no search could ever find them; ingest skips PRs whose baseRepository
#     is not ours.
#     Not headRepository: that is where the change was written, usually the
#     contributor's fork (someuser/httpx).
#
#   Fetched but never saved: stateReason, author { __typename }, baseRepository and
#   every pageInfo. Ingest uses them only to decide what to keep, or to get the next page.


# Safety limit for fetch_closed_issues(). GitHub decides when paging ends, so the loop
# never trusts that alone: if it isn't done after MAX_PAGES pages, something is stuck.
# 400 pages x 25 = 10,000 issues: far above encode/httpx (~50 pages), and far below the
# ~1,600 pages that would use up the hourly 5,000 points.
MAX_PAGES = 400


def fetch_closed_issues(gh, owner, repo):
    """Fetch every closed issue of a repo, with everything nested inside each.

    IN   gh     the client from github_client.client()
         owner  str, the repo's owner, e.g. "encode"
         repo   str, the repo's name,  e.g. "httpx"

    OUT  list of dicts, one per closed issue, in exactly the shape of
         CLOSED_ISSUES_QUERY (made-up values):

         [
           {
             "number":      2413,                              <- the issue's number
             "title":       "Timeout not applied ...",         <- the issue's title
             "body":        "When I set timeout=5 ...",        <- the issue's text; "" if it has none
             "createdAt":   "2022-10-12T09:30:00Z",            <- when the issue was opened, UTC
             "stateReason": "COMPLETED",                       <- why it was closed: COMPLETED,
                                                                  NOT_PLANNED or DUPLICATE
             "labels": {
               "nodes": [{"name": "bug"}]                      <- 0 to 20 labels
             },
             "comments": {
               "nodes": [                                      <- 0 to 100 comments: GitHub sends
                                                                  only the first 100
                 {
                   "fullDatabaseId": "1276543210",             <- GitHub's id for the comment, as a
                                                                  string (too big for an Int)
                   "body":           "I can reproduce ...",    <- the comment's text
                   "createdAt":      "2022-10-12T11:02:00Z",   <- when it was posted, UTC
                   "author":         {"__typename": "User"}    <- who wrote it: "User", "Bot"
                                                                  or "Organization"
                 }
               ]
             },
             "closedByPullRequestsReferences": {
               "pageInfo": {"hasNextPage": False},             <- True if over 10 PRs are linked
                                                                  (the list was cut): ingest then
                                                                  drops the issue
               "nodes": [                                      <- 0 to 10 PRs that closed the issue
                 {
                   "number":         2415,                     <- the PR's number
                   "title":          "Fix timeout ...",        <- the PR's title
                   "body":           "Fixes #2413 ...",        <- the PR's description
                   "mergedAt":       "2022-10-15T08:00:00Z",   <- when it was merged;
                                                                  None if it was never merged
                   "mergeCommit":    {"oid": "3f2a9c..."},     <- the merge commit's SHA;
                                                                  None if it was never merged
                   "baseRepository": {"nameWithOwner": "encode/httpx"},
                                                               <- the repo it was merged into
                   "files": {
                     "pageInfo": {"hasNextPage": False},       <- True if it changed over 100 files
                                                                  (the list was cut): ingest then
                                                                  drops the issue
                     "nodes": [                                <- 0 to 100 files the PR changed
                       {"path": "httpx/_client.py", "changeType": "MODIFIED"}
                                                               <- changeType: ADDED, MODIFIED,
                                                                  DELETED, RENAMED, COPIED or CHANGED
                     ]
                   }
                 }
               ]
             }
           },
           ...                                                 <- one dict like this per closed issue
         ]

    STEPS
      1. Start with an empty list, and no cursor (page 1 has no bookmark yet).
      2. Send CLOSED_ISSUES_QUERY with owner, repo and the cursor.
      3. Take the page out of the response: {"pageInfo": {...}, "nodes": [up to 25 issues]}.
      4. Add the page's issues to the list.
      5. If hasNextPage is False, this was the last page: return the list.
      6. Otherwise set the cursor to endCursor, and go back to step 2.
      7. If MAX_PAGES pages ran and the last page never came, paging is stuck: raise.

    Unfiltered: every closed issue comes back, and filter_history.py applies the INGEST RULES.
    Raises whatever graphql() raises, so one failed request fails the whole fetch, and
    RuntimeError if paging never ends (step 7).
    """
    # 1. Start with an empty list, and no cursor.
    issues = []     # every closed issue, collected page by page
    cursor = None   # the bookmark. None = page 1: GitHub starts from the first issue

    # Steps 2-6 repeat once per page, at most MAX_PAGES times. Normally the loop ends in
    # step 5, which returns the list. (_ is the loop's counter; nothing uses it.)
    for _ in range(MAX_PAGES):
        # 2. Send CLOSED_ISSUES_QUERY with owner, repo and the cursor.
        #    data = {"repository": {"issues": {"pageInfo": {...}, "nodes": [...]}}}
        data = graphql(gh, CLOSED_ISSUES_QUERY, {"owner": owner, "repo": repo, "cursor": cursor})

        # 3. Take the page out of the response: two levels down, under repository -> issues.
        #    page = {"pageInfo": {"hasNextPage": True, "endCursor": "Y3Vy..."},
        #            "nodes": [up to 25 issue dicts]}
        page = data["repository"]["issues"]

        # 4. Add the page's issues to the list. extend() adds each item of page["nodes"]
        #    to issues, so issues stays one flat list: 25 after page 1, 50 after page 2, ...
        issues.extend(page["nodes"])

        # 5. hasNextPage False = this was the last page: every closed issue is in the list.
        #    return hands the list back to the caller, and also ends the loop.
        if not page["pageInfo"]["hasNextPage"]:
            return issues

        # 6. More pages left: move the bookmark to the end of this page. The loop goes back
        #    to step 2, which sends the same query with this cursor and gets the next page.
        cursor = page["pageInfo"]["endCursor"]

    # 7. Only reached if all MAX_PAGES pages ran and GitHub never said "last page": paging
    #    is stuck (a GitHub bug, or ours). Stop with a clear error, instead of fetching
    #    forever and using up the hourly points.
    raise RuntimeError(f"{owner}/{repo}: more than {MAX_PAGES} pages, paging never ended")
