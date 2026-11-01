import calendar, curses, io, json, os, subprocess, sys, tempfile, time, unittest, unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import org_dash as od  # noqa: E402

cs = od.cs
NOW = calendar.timegm((2026, 10, 31, 12, 0, 0, 0, 0, 0))


def iso(days_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - days_ago * 86400))


def run_obj(branch, conclusion, name="CI", status="completed", repo=None, rid=1):
    return {"id": rid, "name": name, "head_branch": branch, "status": status, "conclusion": conclusion,
            "html_url": "https://x/%s" % rid, "head_sha": "abcdef1234567", "repo": repo}


def pr(n, user="alice", head="feat", draft=False):
    return {"number": n, "title": "pr %d" % n, "user": user, "draft": draft, "head": head,
            "url": "https://x/pull/%d" % n, "updated_at": iso(1)}


class FakeGh:
    """Mocks ci-status gh_api for the dashboard endpoints (GET paths only)."""

    def __init__(self, runs=None, pulls=None, alerts=None, branches=None, commits=None, jobs=None,
                 rate=(5000, 5000), fail=None):
        self.runs, self.pulls, self.alerts = runs or {}, pulls or {}, alerts or {}
        self.branches, self.commits, self.jobs = branches or {}, commits or {}, jobs or {}
        self.rate, self.fail = rate, fail or {}
        self.calls = []

    def __call__(self, path, jq=None, paginate=False, fatal=cs.FATAL_STATUSES):
        self.calls.append(path)
        for prefix, exc in self.fail.items():
            if path.startswith(prefix):
                raise exc
        if path == "rate_limit":
            used = len([c for c in self.calls if c != "rate_limit"])
            return {"remaining": self.rate[0] - used, "limit": self.rate[1], "reset": NOW + 3600}
        full = "/".join(path.split("/")[1:3])
        if "/actions/runs/" in path and path.endswith("per_page=100"):
            return self.jobs.get(full, [])
        if "/actions/runs" in path:
            branch = path.split("branch=")[1].split("&")[0]
            rs = [r for r in self.runs.get(full, []) if r["head_branch"] == branch]
            return {"total": len(rs), "runs": rs}
        if "/pulls" in path:
            return self.pulls.get(full, [])
        if "/dependabot/alerts" in path:
            a = self.alerts.get(full, [])
            if isinstance(a, Exception):
                raise a
            return a
        if "/branches" in path:
            return self.branches.get(full, [{"name": "main", "sha": "m0"}])
        if "/commits/" in path:
            return self.commits[path.rsplit("/", 1)[1]]
        raise AssertionError("unexpected path " + path)


def repos(*names, owner="o"):
    return [{"owner": owner, "name": n, "archived": False, "branch": "main"} for n in names]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = os.path.join(self.tmp.name, "cache", "dash.json")
        self.clock = [NOW]
        # Nothing in these tests may reach the real gh / GitHub.
        orig_run = subprocess.run
        self.addCleanup(setattr, subprocess, "run", orig_run)

        def no_subprocess(*a, **k):
            raise AssertionError("subprocess.run called: %r" % (a,))
        subprocess.run = no_subprocess
        self.list_calls = []

    def lister(self, table):
        def fn(acct):
            self.list_calls.append(acct)
            return table.get(acct, [])
        return fn

    def args(self, *extra):
        return od.parse_args(["--owners", "o", "--cache", self.cache, "--sleep", "0", *extra])

    def dash(self, gh, table=None, extra=()):
        table = table if table is not None else {"o": repos("a", "b")}
        return od.Dash(self.args(*extra), gh=gh, list_fn=self.lister(table), now=lambda: self.clock[0])

    def json_main(self, gh, table=None, extra=()):
        out, err = io.StringIO(), io.StringIO()
        table = table if table is not None else {"o": repos("a", "b")}
        rc = od.main(["--owners", "o", "--cache", self.cache, "--sleep", "0", "--json", *extra],
                     gh=gh, list_fn=self.lister(table), out=out, err=err, now=lambda: self.clock[0])
        return rc, (json.loads(out.getvalue()) if out.getvalue() else None), err.getvalue()


class FetchTests(Base):
    def test_full_entry(self):
        gh = FakeGh(
            runs={"o/a": [run_obj("main", None, status="in_progress", rid=3),
                          run_obj("main", "failure", repo="evil/a", rid=2),
                          run_obj("main", "failure", rid=1)]},
            jobs={"o/a": [{"name": "build", "conclusion": "success", "url": "u1"},
                          {"name": "test", "conclusion": "failure", "url": "u2"},
                          {"name": "lint", "conclusion": "skipped", "url": "u3"}]},
            pulls={"o/a": [pr(1), pr(2, "dependabot[bot]", "dependabot/npm/x"), pr(3, draft=True)]},
            alerts={"o/a": [{"number": 1, "severity": "high", "package": "lodash", "ecosystem": "npm",
                             "summary": "proto pollution", "url": "u"}]},
            branches={"o/a": [{"name": "main", "sha": "m"}, {"name": "old", "sha": "s1"},
                              {"name": "new", "sha": "s2"}]},
            commits={"s1": iso(200), "s2": iso(3)})
        dates = {}
        e = od.fetch_dash_repo(repos("a")[0], gh, commit_dates=dates, now=lambda: NOW)
        self.assertEqual(e["errors"], [])
        self.assertEqual(e["ci"]["conclusion"], "failure")
        self.assertEqual(e["ci"]["run_id"], 1)  # fork run ignored
        self.assertTrue(e["ci"]["in_progress"])
        self.assertEqual([j["name"] for j in e["ci"]["failed_jobs"]], ["test"])
        self.assertEqual(set(dates), {"o/a@s1", "o/a@s2"})
        row = od.summarize_entry(e, 90, NOW)
        self.assertEqual((row["open_prs"], row["dependabot_prs"], row["dependabot_alerts"],
                          row["stale_branches"], row["ci"], row["ci_red"]), (3, 1, 1, 1, "failure", True))
        self.assertEqual(od.summarize_entry(e, 2, NOW)["stale_branches"], 2)
        # second fetch reuses the commit-date cache
        gh.calls.clear()
        od.fetch_dash_repo(repos("a")[0], gh, commit_dates=dates, now=lambda: NOW)
        self.assertFalse([c for c in gh.calls if "/commits/" in c])

    def test_alerts_without_scope_are_na(self):
        gh = FakeGh(alerts={"o/a": cs.GhError("HTTP 403: Resource not accessible", 403)})
        e = od.fetch_dash_repo(repos("a")[0], gh, now=lambda: NOW)
        self.assertIsNone(e["alerts"])
        self.assertIn("n/a", e["alerts_note"])
        self.assertEqual(e["errors"], [])
        self.assertIsNone(od.summarize_entry(e, 90, NOW)["dependabot_alerts"])

    def test_alert_server_error_is_an_error_not_na(self):
        gh = FakeGh(alerts={"o/a": cs.GhError("HTTP 500: boom", 500)})
        e = od.fetch_dash_repo(repos("a")[0], gh, now=lambda: NOW)
        self.assertIsNone(e["alerts"])
        self.assertNotIn("n/a", e["alerts_note"])
        self.assertEqual(len(e["errors"]), 1)
        self.assertIn("alerts: HTTP 500", e["errors"][0])
        for status in (401, 404, 410):
            e = od.fetch_dash_repo(repos("a")[0], FakeGh(alerts={"o/a": cs.GhError("HTTP %d" % status, status)}),
                                   now=lambda: NOW)
            self.assertIn("n/a", e["alerts_note"])
            self.assertEqual(e["errors"], [])

    def test_alert_fetch_uses_no_retry_for_403(self):
        seen = {}

        def gh(path, jq=None, paginate=False, fatal=cs.FATAL_STATUSES):
            if "alerts" in path:
                seen["fatal"] = fatal
            return [] if "runs" not in path else {"runs": []}
        od.fetch_dash_repo(repos("a")[0], gh, now=lambda: NOW)
        self.assertIn(403, seen["fatal"])

    def test_no_runs_and_empty_repo(self):
        e = od.fetch_dash_repo({"owner": "o", "name": "e", "archived": False, "branch": None},
                               FakeGh(), now=lambda: NOW)
        self.assertIsNone(e["ci"])
        self.assertEqual(od.summarize_entry(e, 90, NOW)["ci"], "none")

    def test_section_error_recorded_rate_limit_raised(self):
        gh = FakeGh(fail={"repos/o/a/pulls": cs.GhError("HTTP 502", 502)})
        e = od.fetch_dash_repo(repos("a")[0], gh, now=lambda: NOW)
        self.assertEqual(len(e["errors"]), 1)
        self.assertIn("prs", e["errors"][0])
        gh = FakeGh(fail={"repos/o/a/pulls": cs.GhError("rate limited", 403, rate_limited=True)})
        with self.assertRaises(cs.GhError):
            od.fetch_dash_repo(repos("a")[0], gh, now=lambda: NOW)

    def test_branch_lookup_cap(self):
        br = [{"name": "main", "sha": "m"}] + [{"name": "b%d" % i, "sha": "s%d" % i} for i in range(5)]
        gh = FakeGh(branches={"o/a": br}, commits={"s%d" % i: iso(400) for i in range(5)})
        e = od.fetch_dash_repo(repos("a")[0], gh, max_lookups=2, now=lambda: NOW)
        row = od.summarize_entry(e, 90, NOW)
        self.assertEqual((row["stale_branches"], row["unknown_branch_dates"]), (2, 3))

    def test_only_get_paths(self):
        gh = FakeGh(runs={"o/a": [run_obj("main", "failure")]})
        od.fetch_dash_repo(repos("a")[0], gh, now=lambda: NOW)
        for c in gh.calls:
            self.assertTrue(c.startswith("repos/o/a/"), c)
            self.assertNotIn(" ", c)


class SortFilterTests(unittest.TestCase):
    def row(self, name, prs=0, ci="success", dep=0, alerts=0, stale=0, errors=()):
        return {"repo": "o/" + name, "open_prs": prs, "ci": ci, "ci_red": ci in cs.RED,
                "dependabot_prs": dep, "dependabot_alerts": alerts, "stale_branches": stale,
                "errors": list(errors)}

    def setUp(self):
        self.rows = [self.row("c", prs=1), self.row("a", ci="failure"), self.row("b", dep=2, alerts=None),
                     self.row("d", stale=3, ci="none"), self.row("e", errors=["x"])]

    def names(self, rows):
        return [r["repo"][2:] for r in rows]

    def test_sort(self):
        self.assertEqual(self.names(od.sort_rows(self.rows)), list("abcde"))
        self.assertEqual(self.names(od.sort_rows(self.rows, "name", True)), list("edcba"))
        self.assertEqual(self.names(od.sort_rows(self.rows, "prs"))[0], "c")
        self.assertEqual(self.names(od.sort_rows(self.rows, "ci")), ["a", "d", "b", "c", "e"])
        self.assertEqual(self.names(od.sort_rows(self.rows, "deps"))[0], "b")
        self.assertEqual(self.names(od.sort_rows(self.rows, "stale"))[0], "d")

    def test_filter(self):
        f = lambda n, s="": self.names(od.filter_rows(self.rows, n, s))
        self.assertEqual(f("failing"), ["a"])
        self.assertEqual(f("prs"), ["c"])
        self.assertEqual(f("dependabot"), ["b"])
        self.assertEqual(f("stale"), ["d"])
        self.assertEqual(f("errors"), ["e"])
        self.assertEqual(sorted(f("attention")), ["a", "b", "d", "e"])
        self.assertEqual(f("all", "O/C"), ["c"])


class DashTests(Base):
    def test_cache_ttl_and_force(self):
        gh = FakeGh()
        d = self.dash(gh)
        d.refresh()
        n = len(gh.calls)
        self.assertGreater(n, 0)
        self.assertTrue(os.path.exists(self.cache))
        # fresh cache: a new instance makes no API or list calls
        d2 = self.dash(gh)
        d2.refresh()
        self.assertEqual(len(gh.calls), n)
        self.assertEqual(self.list_calls, ["o"])
        self.assertEqual([r["repo"] for r in d2.rows()], ["o/a", "o/b"])
        # TTL expired -> re-fetch
        self.clock[0] += od.DEFAULT_TTL + 1
        self.dash(gh).refresh()
        self.assertGreater(len(gh.calls), n)
        # force refresh ignores the TTL
        m = len(gh.calls)
        self.dash(gh).refresh(force=True)
        self.assertGreater(len(gh.calls), m)

    def test_refresh_single_repo(self):
        gh = FakeGh()
        d = self.dash(gh)
        d.refresh()
        gh.calls.clear()
        d.refresh(force=True, only={"o/b"})
        self.assertTrue(gh.calls)
        self.assertFalse([c for c in gh.calls if c.startswith("repos/o/a/")])

    def test_rate_limit_reserve_stops_early(self):
        gh = FakeGh(rate=(cs.MIN_RATE_REMAINING + 6, 5000))
        d = self.dash(gh, {"o": repos("a", "b", "c")})
        d.refresh()
        self.assertEqual(len(d.cache["repos"]), 1)
        self.assertIn("rate limit", d.notes[0])
        self.assertEqual(d.errors, [])

    def test_rate_limit_reserve_enforced_per_request(self):
        # 606 remaining, reserve 600: branch lookups must not push it below the reserve
        br = [{"name": "main", "sha": "m"}] + [{"name": "b%d" % i, "sha": "s%d" % i} for i in range(30)]
        gh = FakeGh(rate=(cs.MIN_RATE_REMAINING + 6, 5000), branches={"o/a": br, "o/b": br},
                    commits={"s%d" % i: iso(400) for i in range(30)})
        d = self.dash(gh)
        d.refresh()
        self.assertEqual(len([c for c in gh.calls if c != "rate_limit"]), 6)
        self.assertGreaterEqual(d.rate["remaining"], cs.MIN_RATE_REMAINING)
        self.assertEqual(list(d.cache["repos"]), ["o/a"])  # kept, with dates left unknown
        self.assertEqual(od.summarize_entry(d.cache["repos"]["o/a"], 90, NOW)["unknown_branch_dates"], 28)
        self.assertTrue(any("rate limit" in n for n in d.notes))
        self.assertEqual(d.errors, [])

    def test_budget_refuses_request_before_sending(self):
        gh = FakeGh(runs={"o/a": [run_obj("main", "failure")]})
        with self.assertRaises(od.BudgetExhausted):
            od.fetch_dash_repo(repos("a")[0], od.CountingGh(gh, budget=2), now=lambda: NOW)
        self.assertEqual(len(gh.calls), 2)

    def test_rate_limited_response_keeps_cache(self):
        d = self.dash(FakeGh())
        d.refresh()
        before = json.dumps(d.cache["repos"]["o/b"], sort_keys=True)
        self.clock[0] += od.DEFAULT_TTL + 1
        gh = FakeGh(fail={"repos/o/a/": cs.GhError("rate limited", 403, rate_limited=True)})
        d = self.dash(gh)
        d.refresh()
        self.assertIn("rate limited", d.notes[0])
        self.assertFalse([c for c in gh.calls if c.startswith("repos/o/b/")])
        self.assertEqual(json.dumps(d.cache["repos"]["o/b"], sort_keys=True), before)
        self.assertEqual(len(d.rows()), 2)

    def test_skip_archived_and_prune(self):
        table = {"o": repos("a", "Greymarch") + [{"owner": "o", "name": "old", "archived": True, "branch": "main"}]}
        d = self.dash(FakeGh(), table)
        d.refresh()
        self.assertEqual(list(d.cache["repos"]), ["o/a"])
        self.clock[0] += od.DEFAULT_TTL + 1
        d = self.dash(FakeGh(), {"o": repos("b")})
        d.refresh()
        self.assertEqual(sorted(d.cache["repos"]), ["o/b"])

    def test_offline(self):
        gh = FakeGh()
        d = self.dash(gh, extra=["--offline"])
        d.refresh()
        self.assertEqual(gh.calls, [])
        self.assertEqual(self.list_calls, [])

    def test_list_failure_uses_cached_list(self):
        self.dash(FakeGh()).refresh()
        self.clock[0] += od.DEFAULT_TTL + 1

        def broken(acct):
            raise cs.GhError("gh repo list o failed")
        d = od.Dash(self.args(), gh=FakeGh(), list_fn=broken, now=lambda: self.clock[0])
        d.refresh()
        self.assertEqual(len(d.repos), 2)
        self.assertTrue(any("from cache" in n for n in d.notes))

    def test_does_not_touch_state_or_known(self):
        with self.assertRaises(SystemExit):
            with unittest.mock.patch("sys.stderr", io.StringIO()):
                od.parse_args(["--cache", os.path.join(od.HERE, "state.json")])


class JsonTests(Base):
    def test_json_report(self):
        gh = FakeGh(runs={"o/a": [run_obj("main", "failure")]},
                    jobs={"o/a": [{"name": "t", "conclusion": "failure", "url": "u"}]},
                    pulls={"o/b": [pr(7)]},
                    alerts={"o/b": cs.GhError("HTTP 403: no scope", 403)},
                    branches={"o/a": [{"name": "main", "sha": "m"}, {"name": "x", "sha": "s"}]},
                    commits={"s": iso(100)})
        rc, rep, err = self.json_main(gh, extra=["--sort", "ci"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([r["repo"] for r in rep["repos"]], ["o/a", "o/b"])
        a, b = rep["repos"]
        self.assertEqual(a["ci"], "failure")
        self.assertEqual(a["details"]["ci"]["failed_jobs"][0]["name"], "t")
        self.assertEqual(a["details"]["stale_branches"][0]["name"], "x")
        self.assertEqual(b["open_prs"], 1)
        self.assertIsNone(b["dependabot_alerts"])
        self.assertIn("n/a", b["details"]["alerts_note"])
        self.assertEqual(rep["rate_limit"]["limit"], 5000)
        rc, rep, _ = self.json_main(gh, extra=["--filter", "failing"])
        self.assertEqual([r["repo"] for r in rep["repos"]], ["o/a"])
        rc, rep, _ = self.json_main(gh, extra=["--stale-days", "200"])
        self.assertEqual(rep["repos"][0]["stale_branches"], 0)

    def test_json_errors_exit_2(self):
        gh = FakeGh(fail={"repos/o/a/pulls": cs.GhError("HTTP 500", 500)})
        rc, rep, err = self.json_main(gh)
        self.assertEqual(rc, 2)
        self.assertIn("o/a: prs", err)
        self.assertEqual(len(rep["repos"]), 2)

    def test_cached_entry_errors_reported(self):
        gh = FakeGh(fail={"repos/o/a/pulls": cs.GhError("HTTP 500", 500)})
        rc, _, _ = self.json_main(gh)
        self.assertEqual(rc, 2)
        # second run within the TTL serves o/a from cache: its errors still count
        calls = len(gh.calls)
        rc, rep, err = self.json_main(gh)
        self.assertEqual(len(gh.calls), calls)
        self.assertEqual(rc, 2)
        self.assertIn("o/a: prs", err)
        self.assertEqual(len(rep["errors"]), 1)
        self.assertIn("o/a: prs", rep["errors"][0])
        rc, rep, _ = self.json_main(gh, extra=["--offline"])
        self.assertEqual((rc, len(rep["errors"])), (2, 1))

    def test_ci_status_dash_dispatch(self):
        called = {}
        orig = od.main
        self.addCleanup(setattr, od, "main", orig)
        od.main = lambda argv: called.setdefault("argv", argv) and 0
        sys.modules["ci_status"] = cs
        self.assertEqual(cs.main(["dash", "--json"]), 0)
        self.assertEqual(called["argv"], ["--json"])


class FakeScreen:
    def __init__(self, keys=(), h=20, w=100):
        self.keys, self.h, self.w = list(keys), h, w
        self.lines = {}

    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        self.lines = {}

    def addnstr(self, y, x, text, n, attr=0):
        if y >= self.h:
            raise curses.error
        self.lines[y] = text[:n]

    def refresh(self):
        pass

    def nodelay(self, flag):
        pass

    def getch(self):
        return self.keys.pop(0) if self.keys else ord("q")

    def text(self):
        return "\n".join(self.lines[k] for k in sorted(self.lines))


class TUITests(Base):
    def setUp(self):
        super().setUp()
        gh = FakeGh(runs={"o/b": [run_obj("main", "failure")]},
                    pulls={"o/a": [pr(1), pr(2)], "o/b": [pr(3, "dependabot[bot]", "dependabot/pip/x")]},
                    branches={"o/a": [{"name": "main", "sha": "m"}, {"name": "zombie", "sha": "z"}]},
                    commits={"z": iso(365)})
        self.d = self.dash(gh, {"o": repos("a", "b", "c")})
        self.d.refresh()

    def tui(self, keys=()):
        scr = FakeScreen(keys)
        return od.TUI(scr, self.d), scr

    def test_list_render(self):
        t, scr = self.tui()
        t.draw()
        out = scr.text()
        self.assertIn("REPO", out)
        self.assertIn("o/a", out)
        self.assertIn("failure", out)
        self.assertIn("1/0", out)  # one dependabot PR, zero alerts
        self.assertIn("API ", out)

    def test_keys_sort_filter_search_drill(self):
        t, scr = self.tui()
        t.handle_key(ord("s"))
        self.assertEqual(od.SORT_KEYS[t.sort], "prs")
        t.draw()
        self.assertTrue(scr.lines[2].startswith(" o/a"))
        t.handle_key(ord("s"))  # ci
        t.draw()
        self.assertTrue(scr.lines[2].startswith(" o/b"))
        t.handle_key(ord("S"))
        t.draw()
        self.assertFalse(scr.lines[2].startswith(" o/b"))
        t.handle_key(ord("f"))  # attention
        self.assertEqual([r["repo"] for r in t.rows()], ["o/a", "o/b"])
        for ch in "/b":
            t.handle_key(ord(ch))
        t.handle_key(10)
        self.assertEqual(t.search, "b")
        self.assertEqual([r["repo"] for r in t.rows()], ["o/b"])
        t.handle_key(10)  # drill down
        self.assertEqual(t.detail, "o/b")
        t.draw()
        out = scr.text()
        self.assertIn("Open pull requests: 1", out)
        self.assertIn("[dependabot]", out)
        self.assertIn("Dependabot alerts: 0 open", out)
        t.handle_key(27)
        self.assertIsNone(t.detail)
        self.assertTrue(t.handle_key(ord("?")))
        self.assertTrue(t.help)
        t.handle_key(ord("x"))
        self.assertFalse(t.handle_key(ord("q")))

    def test_drill_down_stale(self):
        t, scr = self.tui()
        t.handle_key(10)
        t.draw()
        self.assertIn("zombie", scr.text())
        self.assertIn("365 days", scr.text())

    def test_refresh_key_and_cancel(self):
        t, scr = self.tui()
        gh = self.d.gh
        gh.calls.clear()
        t.handle_key(ord("R"))
        self.assertTrue(gh.calls)
        self.assertFalse([c for c in gh.calls if c.startswith("repos/o/b/") or c.startswith("repos/o/c/")])
        gh.calls.clear()
        scr.keys = [ord("q")]  # cancel during progress
        t.handle_key(ord("r"))
        self.assertEqual([c for c in gh.calls if c != "rate_limit"], [])
        self.assertIn("cancelled", self.d.notes[0])

    def test_run_loop(self):
        t, scr = self.tui([ord("j"), ord("j"), ord("j"), ord("G"), ord("g"), ord("q")])
        t.run(initial_refresh=False)
        self.assertEqual(t.sel, 0)

    def test_tiny_terminal(self):
        scr = FakeScreen(h=3, w=10)
        t = od.TUI(scr, self.d)
        t.draw()
        t.handle_key(10)
        t.draw()


if __name__ == "__main__":
    unittest.main()
