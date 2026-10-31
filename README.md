# ci-status

Prints only what changed since the last run across all non-archived repos of `synthalorian` and `Blackshield-Company`
(skipping daedric-ledger, daedric-ledger-backups, synth-server-backups, Greymarch). Python stdlib + `gh`; no model calls.

    ./ci-status                 # print changes, update state.json; silent (exit 0) if nothing changed
    ./ci-status --json          # same, as JSON
    ./ci-status --no-update     # peek without touching state.json
    ./ci-status --baseline      # accept current state as known; prints counts only
    python3 -m unittest discover -s tests

Reports: newly red / newly green default-branch CI (latest completed run), new failing `dependabot/*` branches
(branch must still exist), new open issues/PRs from non-owners (owner/member/collaborator and bots are ignored).
`known.txt` is the ACK list (`repo#N`, `repo:ci`, `repo:dependabot[/branch]`). First run with no state.json acts as a baseline.
Sequential calls with small sleeps, `nice 19`, load guard (>8 skips, exit 75), flock against overlap, ~2 API calls per repo (~8 min).
Exit codes: 0 ok, 2 errors (stderr), 75 skipped (load / rate limit / already running).

## org-dash

Read-only curses dashboard of every non-archived repo of the owners (same defaults and skip list as above):
open PRs, latest completed default-branch CI run, open Dependabot PRs / alerts, and stale branches.

    ./ci-status dash                      # TUI (or ./org_dash.py)
    ./ci-status dash --owners synthalorian --stale-days 30
    ./ci-status dash --json --filter failing --sort ci   # non-interactive report, details included
    ./ci-status dash --offline            # cached data only, no API calls

Columns: `PRS` open PRs (`+` = first page of 100 only), `CI` conclusion (`*` = newer run in progress, `none` = no runs),
`DEPENDABOT` open Dependabot PRs / open alerts (`n/a` when the token lacks the alerts scope or alerts are disabled),
`STALE` non-default branches with no commits in `--stale-days` (default 90). Row prefix `!` = fetch errors, `~` = cached past TTL.

Keys: `j/k`/arrows, `PgUp/PgDn`, `g/G` move; `Enter` drills into a repo (PR list, failing workflow and failed jobs,
alerts, stale branches), `Esc` back; `s`/`S` next sort key (name, prs, ci, deps, stale) / reverse; `f`/`F` cycle filters
(all, attention, failing, prs, dependabot, stale, errors); `/` search; `r` refresh all; `R` refresh selected repo; `?` help; `q` quit.

Cache: `~/.cache/ci-status/org-dash.json` (`--cache`), entries reused for `--ttl` seconds (default 900); `--refresh` or `r`
ignores the TTL. Branch-head commit dates are cached by SHA, so stale-branch checks only cost calls for new heads
(`--max-branch-lookups` per repo per refresh, default 30). It never reads or writes state.json or known.txt.

API use: GET requests only via `gh api` (~4 per repo plus jobs for red runs and new branch heads); the repo list reuses
`gh repo list` (a read-only GraphQL query, cached with the same TTL). Before refreshing it checks `rate_limit` and stops,
keeping cached data, once fewer than `--reserve` calls (default 600, the same floor ci-status uses) would remain or GitHub
answers "rate limited"; remaining calls and reset time are shown in the header and in the JSON `rate_limit` field.
`--json` exits 0, or 2 if any repo had fetch errors (listed in `errors` and on stderr).
