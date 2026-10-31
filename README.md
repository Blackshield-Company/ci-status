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

## License

MIT. See [LICENSE](LICENSE).

## Support

Donations are optional. Nothing in this tool is gated on them.

- GitHub Sponsors: https://github.com/sponsors/synthalorian
- Ko-fi: https://ko-fi.com/synthalorian
