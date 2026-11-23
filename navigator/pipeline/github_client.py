"""github_client.py: the connection to GitHub's GraphQL API, and nothing else.

What it does:
  client()      an authenticated connection to GitHub, reused for every request
  graphql()     sends one query and returns its data; raises if GitHub reports errors
  GitHubError   what graphql() raises when GitHub rejects the query

It knows nothing about issues, or about what any query asks for. The queries live with
the stage that needs them: fetch_history.py for ingest, and later the briefing's own.
It never touches the database.

Nothing runs on import. The caller opens the client once per run, and hands it to every
function that sends a query:
    with github_client.client() as gh:
        head = fetch_history.fetch_head_sha(gh, "encode", "httpx")
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
#                          One page of CLOSED_ISSUES_QUERY (fetch_history.py) is at
#                          most ~28,000.
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
