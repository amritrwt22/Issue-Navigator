"""github.py: talks to GitHub's GraphQL API, and nothing else.

What it does:
  client()               an authenticated connection to GitHub, reused for every request
  graphql()              sends one query; raises if GitHub reports errors
  fetch_head_sha()       the commit the repo's default branch points at (its HEAD)
  fetch_closed_issues()  pages through a repo's closed issues, everything nested inside

It returns GitHub's data as plain dicts. It does NOT decide what to keep (ingest.py does
that), and it never touches the database.

Nothing runs on import. The caller opens the client once per run:
    with github.client() as gh:
        for issue in github.fetch_closed_issues(gh, "encode", "httpx"):
            ...
"""

import os

import httpx

API_URL = "https://api.github.com"

# --- GitHub's limits (GraphQL API, personal token) ----------------------------------
#
#   Per request
#     Items per list       every list must say first: (or last:), from 1 to 100. So nothing
#                          comes in one go: 1,200 issues need at least 12 requests (pages).
#     Items in total       at most 500,000, counting every nested list.
#                          One page of CLOSED_ISSUES_QUERY is at most ~28,000.
#     Time                 GitHub stops any request that takes more than 10 seconds.
#     Token                required: without one, GraphQL refuses the request (401).
#
#   At any moment
#     Parallel requests    at most 100. Navigator sends one at a time.
#
#   Per minute ("secondary" limits)
#     Points               at most 2,000. Here every read query costs 1 point, whatever its size.
#     GitHub's work        at most 90 seconds of processing per 60 seconds.
#
#   Per hour ("primary" limit)
#     Points               5,000 per user. Here a query costs by size:
#                          (the lists it could fetch, each assumed full) / 100, at least 1.
#                          One page of CLOSED_ISSUES_QUERY ~ 3 points; all of encode/httpx ~ 150.
#     Refill               when the hour is up; the x-ratelimit-reset header says when.
#
#   Per day
#     none                 in practice 24 x 5,000 = 120,000 points.
#
#   Going over
#     the hourly points             200 + "errors"   ->  graphql() raises GitHubError
#     per-minute or parallel        200 or 403       ->  graphql() raises
#   Points left: response headers x-ratelimit-remaining and x-ratelimit-reset.


def client():
    """An httpx client set up for GitHub: token, headers and timeout, in one place.

    Use it in a with-block, so its network connection closes when the run ends.
    The token is read here, at call time, after main() has run load_dotenv().
    """
    return httpx.Client(
        base_url=API_URL,
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",   # required: GraphQL refuses requests without a token
            "User-Agent": "issue-navigator",                            # GitHub rejects requests without one
        },
        timeout=30.0,   # httpx's default is 5s, too short for a big nested query; GitHub itself stops at 10s
    )


class GitHubError(Exception):
    """GitHub answered 200 OK, but the body lists errors: the query itself failed.

    Its own class, so the caller can tell "GitHub rejected the query" apart from
    any other crash, the way RepoLens has CloneError.
    """


def graphql(gh, query, variables):
    """Send one GraphQL query to GitHub and return the "data" part of its answer.

    Every GraphQL call is the same HTTP request. Only the BODY changes:

        POST https://api.github.com/graphql      ┐
        Authorization: Bearer <token>            ├ same for every call: set once
        Content-Type: application/json           ┘ by client() and json= below

        {
          "query":     "<GraphQL text with $blanks>",    what we want
          "variables": {<a value for each $blank>}       the values, like %s in SQL
        }

    So this one function sends every query. It doesn't know or care what the
    query asks for; the caller passes the two parts that change.
    
    (note- we write the after:$cursor (to process issues from here) from endCursor written by github in prev response.)

    Example: the first 2 closed issues of encode/httpx (made-up values)

        query       '''query($owner: String!, $repo: String!, $cursor: String) {
                         repository(owner: $owner, name: $repo) {
                           issues(first: 2, after: $cursor, states: CLOSED) {
                             nodes { number title }
                             pageInfo { hasNextPage endCursor }
                           }
                         }
                       }'''

        variables   {"owner": "encode", "repo": "httpx", "cursor": None}
        
        GitHub sends back this response body (JSON), shaped exactly like the query:

            {
              "data": {
                "repository": {
                  "issues": {
                    "nodes": [                              <- the 2 issues, with only
                      {"number": 1, "title": "..."},           the fields we asked for
                      {"number": 4, "title": "..."}
                    ],
                    "pageInfo": {                           <- is there more, and the
                      "hasNextPage": true,                     bookmark to continue from
                      "endCursor": "Y3Vyc29yOjI="
                    }
                  }
                }
              }
            }

        this graphql() returns only what's inside "data", as a Python dict:

            {"repository": {"issues": {"nodes": [...], "pageInfo": {...}}}}

        The next page sends the SAME query; only the variables change:
                    {"owner": "encode", "repo": "httpx", "cursor": "Y3Vyc29yOjI="}

    IN   gh         the client from client()
         query      str, the GraphQL text
         variables  dict, a value for every $blank in it

    OUT  dict, the "data" part of GitHub's answer, shaped like the query

    Raises
      httpx.HTTPStatusError  the request failed, so GitHub never read the query:
                             401 bad or missing token, 403 too many requests too fast,
                             5xx GitHub down
      GitHubError            status 200, but the response body lists "errors":
                             misspelled field, unknown repo, hourly rate limit used up

    No retries: ingest is safe to re-run, so a failed run is simply run again.
    """
    # 1. POST the body {"query": ..., "variables": ...} to api.github.com/graphql.
    #    json= converts the dict to JSON; response = GitHub's status code + response body.
    response = gh.post("/graphql", json={"query": query, "variables": variables})

    # 2. raise_for_status() is an httpx method that checks the response's status code:
    #    200 does nothing and moves on; 401/403/5xx raises httpx.HTTPStatusError.
    response.raise_for_status()

    # 3. json() is an httpx method: it turns the response body (JSON text) into a Python dict.
    #    GitHub sends text (a str):
    #      '{"data": {"repository": {"issue": {"title": "Fix timeout", "closed": true}}}}'
    #    json() gives a dict:
    #       {"data": {"repository": {"issue": {"title": "Fix timeout", "closed": True}}}}
    #    so body["data"] works, and JSON true/false/null become Python True/False/None.
    body = response.json()

    # 4. The query failed: the status code is still 200, and the body has "errors". E.g.
    #      misspelled field   200   {"errors": [{"message": "Field 'titel' doesn't exist ..."}]}
    #      unknown repo       200   {"data": {"repository": None}, "errors": [{"type": "NOT_FOUND", ...}]}
    #    Raise even if some "data" came back too (the unknown repo): storing partial
    #    data would mean history silently missing from the database.
    if body.get("errors"):
        raise GitHubError(body["errors"])

    # 5. The query worked: return only what's inside "data", so the caller skips that level.
    #      body      {"data": {"repository": {"issue": {"title": "Fix timeout", "closed": True}}}}
    #      returns           {"repository": {"issue": {"title": "Fix timeout", "closed": True}}}
    return body["data"]


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

    IN   gh      the client from client()
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
#
# INGEST RULES: what ingest.py keeps from this query's response, and the value that decides
#
#   Rule 1: only closed issues
#     Decided by:  states: CLOSED in the query. GitHub never sends open issues.
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
#     Decided by:  pr["baseRepository"]["nameWithOwner"].lower() == "encode/httpx" (our repo)
#                  Lowercase on both sides, because GitHub names ignore case.
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


# Safety limit for fetch_closed_issues(). GitHub decides when paging ends, so the loop
# never trusts that alone: if it isn't done after MAX_PAGES pages, something is stuck.
# 400 pages x 25 = 10,000 issues: far above encode/httpx (~50 pages), and far below the
# ~1,600 pages that would use up the hourly 5,000 points.
MAX_PAGES = 400


def fetch_closed_issues(gh, owner, repo):
    """Fetch every closed issue of a repo, with everything nested inside each.

    IN   gh     the client from client()
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

    Unfiltered: every closed issue comes back, and ingest.py applies the INGEST RULES.
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



























