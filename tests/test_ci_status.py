import importlib.machinery, importlib.util, io, json, os, tempfile, unittest, argparse

_p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ci-status")
_loader = importlib.machinery.SourceFileLoader("ci_status", _p)
_spec = importlib.util.spec_from_loader("ci_status", _loader)
cs = importlib.util.module_from_spec(_spec)
_loader.exec_module(cs)


def run_obj(branch, conclusion, name="CI", status="completed", repo=None, rid=1):
    return {"id": rid, "name": name, "head_branch": branch, "status": status, "conclusion": conclusion,
            "html_url": "https://x/%s" % rid, "head_sha": "abcdef1234567", "repo": repo}


class FakeGh:
    """Mocks gh_api. data[path-prefix] -> payload."""
    def __init__(self, runs, issues, branches=None):
        self.runs, self.issues, self.branches = runs, issues, branches or {}
        self.calls = []

    def __call__(self, path, jq=None, paginate=False):
        self.calls.append(path)
        if "/actions/runs" in path:
            full = path.split("repos/")[1].split("/actions")[0]
            if "branch=" in path:
                b = path.split("branch=")[1].split("&")[0]
                rs = [r for r in self.runs.get(full, []) if r["head_branch"] == b]
                return {"total": len(rs), "runs": rs[:1]}
            return {"total": len(self.runs.get(full, [])), "runs": self.runs.get(full, [])}
        if "/issues" in path:
            full = path.split("repos/")[1].split("/issues")[0]
            return self.issues.get(full, [])
        if "/branches/" in path:
            full = path.split("repos/")[1].split("/branches/")[0]
            br = path.split("/branches/")[1]
            if br in self.branches.get(full, []):
                return br
            raise cs.GhError("not found", 404)
        raise AssertionError(path)


def issue(n, user, assoc="NONE", pr=False, title="t"):
    return {"number": n, "title": title, "user": user, "assoc": assoc, "pr": pr, "url": "https://x/i/%d" % n}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state.json")
        self.known = os.path.join(self.tmp.name, "known.txt")
        with open(self.known, "w") as fh:
            fh.write("# c\nfoo#5\nbar:ci\nbaz:dependabot/npm/x\n")
        self._orig = cs.gh_api
        self.addCleanup(setattr, cs, "gh_api", self._orig)

    def read(self):
        with open(self.state) as fh:
            return fh.read()

    def go(self, fake, extra=(), repos=None):
        cs.gh_api = fake
        repos = repos or [{"owner": "o", "name": n, "archived": False, "branch": "main"}
                          for n in ("foo", "bar", "baz", "qux")]
        args = cs.parse_args(["--state", self.state, "--known", self.known, "--sleep", "0", *extra])
        out, err = io.StringIO(), io.StringIO()
        rc = cs.run(args, list_fn=lambda a: repos if a == "o" else [],
                    fetch_fn=lambda r, o, s: cs.fetch_repo(r, o, s), out=out, err=err) \
            if False else self._run(args, repos, out, err)
        return rc, out.getvalue(), err.getvalue()

    def _run(self, args, repos, out, err):
        args.accounts = ["o"]
        return cs.run(args, list_fn=lambda a: repos, out=out, err=err)


class Tests(Base):
    def test_baseline_then_silent(self):
        fake = FakeGh({"o/foo": [run_obj("main", "failure"), run_obj("dependabot/npm/a", "failure", rid=2)],
                       "o/qux": [run_obj("main", "success")]},
                      {"o/foo": [issue(5, "stranger")]}, {"o/foo": ["dependabot/npm/a"]})
        rc, out, _ = self.go(fake)
        self.assertEqual(rc, 0)
        self.assertIn("baseline recorded", out)
        self.assertIn("failing_dependabot_branches=1", out)
        rc, out, err = self.go(fake)
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_changes_detected(self):
        runs = {"o/qux": [run_obj("main", "success")], "o/foo": [run_obj("main", "failure")]}
        self.go(FakeGh(runs, {}))
        runs2 = {"o/qux": [run_obj("main", "failure", rid=9), run_obj("main", "success")],
                 "o/foo": [run_obj("main", "success", rid=10)]}
        issues = {"o/qux": [issue(1, "stranger", pr=True), issue(2, "synthalorian"),
                            issue(3, "dependabot[bot]", pr=True), issue(4, "friend", "COLLABORATOR")]}
        runs2["o/qux"].append(run_obj("dependabot/pip/z", "failure", rid=11))
        fake = FakeGh(runs2, issues, {"o/qux": ["dependabot/pip/z"]})
        rc, out, _ = self.go(fake)
        self.assertEqual(rc, 0)
        self.assertIn("RED     o/qux", out)
        self.assertIn("GREEN   o/foo", out)  # foo not acked for ci
        self.assertIn("DEPBOT  o/qux  dependabot/pip/z", out)
        self.assertIn("NEW PR    o/qux#1", out)
        self.assertNotIn("#2", out)
        self.assertNotIn("#3", out)
        self.assertNotIn("#4", out)
        # next run: nothing new
        self.assertEqual(self.go(fake)[1], "")

    def test_known_suppresses(self):
        self.go(FakeGh({"o/bar": [run_obj("main", "success")]}, {}))
        fake = FakeGh({"o/bar": [run_obj("main", "failure")],
                       "o/baz": [run_obj("dependabot/npm/x", "failure")]},
                      {"o/foo": [issue(5, "stranger")], "o/baz": []}, {"o/baz": ["dependabot/npm/x"]})
        rc, out, _ = self.go(fake)
        self.assertEqual(out, "")

    def test_deleted_dependabot_branch_ignored(self):
        self.go(FakeGh({}, {}))
        fake = FakeGh({"o/qux": [run_obj("dependabot/npm/gone", "failure")]}, {}, {"o/qux": []})
        self.assertEqual(self.go(fake)[1], "")

    def test_fork_run_not_counted_and_inprogress_skipped(self):
        self.go(FakeGh({"o/qux": [run_obj("main", "success")]}, {}))
        fake = FakeGh({"o/qux": [run_obj("main", "failure", repo="evil/qux"),
                                 run_obj("main", None, status="in_progress", repo="o/qux"),
                                 run_obj("main", "success", repo="o/qux")]}, {})
        self.assertEqual(self.go(fake)[1], "")

    def test_skip_and_archived(self):
        repos = [{"owner": "o", "name": "Greymarch", "archived": False, "branch": "main"},
                 {"owner": "o", "name": "old", "archived": True, "branch": "main"},
                 {"owner": "o", "name": "daedric-ledger", "archived": False, "branch": "main"},
                 {"owner": "o", "name": "ok", "archived": False, "branch": "main"}]
        fake = FakeGh({}, {})
        self.go(fake, repos=repos)
        st = json.loads(self.read())
        self.assertEqual(list(st["repos"]), ["o/ok"])

    def test_no_update_and_json(self):
        fake = FakeGh({"o/qux": [run_obj("main", "success")]}, {})
        self.go(fake)
        before = self.read()
        fake2 = FakeGh({"o/qux": [run_obj("main", "failure")]}, {})
        rc, out, _ = self.go(fake2, extra=["--no-update", "--json"])
        self.assertEqual(json.loads(out)["newly_red"][0]["repo"], "o/qux")
        self.assertEqual(self.read(), before)
        # --baseline absorbs the change silently with counts
        rc, out, _ = self.go(fake2, extra=["--baseline"])
        self.assertIn("ci_red=1", out)
        self.assertEqual(self.go(fake2)[1], "")

    def test_fetch_error_keeps_old_state(self):
        self.go(FakeGh({"o/qux": [run_obj("main", "success")]}, {}))
        def boom(path, jq=None, paginate=False):
            raise cs.GhError("HTTP 500", 500)
        rc, out, err = self.go(boom)
        self.assertEqual(rc, 2)
        self.assertIn("o/qux", err)
        self.assertEqual(self.go(FakeGh({"o/qux": [run_obj("main", "success")]}, {}))[1], "")


if __name__ == "__main__":
    unittest.main()
