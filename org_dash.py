#!/usr/bin/env python3
"""org-dash: read-only terminal dashboard of every non-archived repo of the
configured GitHub owners: open PRs, latest default-branch CI, Dependabot PRs and
alerts, and stale branches. Reuses ci-status for the repo list, skip list, `gh`
wrapper and state-file IO. Python standard library (curses) + `gh`; GET only.

    ./ci-status dash [options]      or      ./org_dash.py [options]

Results are cached (default ~/.cache/ci-status/org-dash.json, TTL 15 min); state.json
and known.txt are never touched.
"""
import argparse
import calendar
import curses
import importlib.machinery
import importlib.util
import json
import os
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_ci_status():
    mod = sys.modules.get("ci_status")
    if mod is not None:
        return mod
    loader = importlib.machinery.SourceFileLoader("ci_status", os.path.join(HERE, "ci-status"))
    spec = importlib.util.spec_from_loader("ci_status", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ci_status"] = mod
    loader.exec_module(mod)
    return mod


cs = _load_ci_status()
GhError = cs.GhError

CACHE_VERSION = 1
DEFAULT_TTL = 900
DEFAULT_STALE_DAYS = 90
DEFAULT_MAX_LOOKUPS = 30
EST_CALLS_PER_REPO = 5  # runs + pulls + alerts + branches + jobs/commit lookups
PAGE = 100
SOFT_STATUSES = (404, 409, 410, 451)     # feature disabled / empty repo: treat as "nothing"
ALERT_NA_STATUSES = (401, 403, 404)      # missing scope or alerts disabled: "n/a"
ALERT_NA_ALL = ALERT_NA_STATUSES + tuple(s for s in SOFT_STATUSES if s not in ALERT_NA_STATUSES)
OK_JOB_CONCLUSIONS = {"success", "skipped", "neutral", None}

PULLS_JQ = ('[.[] | {number, title, user: .user.login, draft, head: .head.ref, '
            'url: .html_url, updated_at}]')
ALERTS_JQ = ('[.[] | {number, severity: .security_advisory.severity, '
             'package: .dependency.package.name, ecosystem: .dependency.package.ecosystem, '
             'summary: .security_advisory.summary, url: .html_url}]')
BRANCHES_JQ = '[.[] | {name, sha: .commit.sha}]'
JOBS_JQ = '[.jobs[] | {name, conclusion, url: .html_url}]'
COMMIT_DATE_JQ = '.commit.committer.date'
RATE_JQ = '.resources.core | {remaining, limit, reset}'

SORT_KEYS = ["name", "prs", "ci", "deps", "stale"]
FILTERS = ["all", "attention", "failing", "prs", "dependabot", "stale", "errors"]


def default_cache_path():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "ci-status", "org-dash.json")


def parse_ts(s):
    """GitHub ISO-8601 UTC timestamp -> epoch seconds (None if missing/garbled)."""
    if not s or not isinstance(s, str):
        return None
    try:
        return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def is_dependabot_pr(pr):
    return pr.get("user") == "dependabot[bot]" or (pr.get("head") or "").startswith("dependabot/")


def rate_info(gh):
    """Core REST rate limit ({remaining, limit, reset}); the endpoint itself is free."""
    try:
        d = gh("rate_limit", jq=RATE_JQ)
        return {k: int(d[k]) for k in ("remaining", "limit", "reset")}
    except Exception:
        return None


class BudgetExhausted(GhError):
    """Raised instead of making a request that would cross the rate-limit reserve."""

    def __init__(self, msg="rate-limit reserve reached"):
        super().__init__(msg, rate_limited=True)


class CountingGh:
    """Counts requests; refuses (BudgetExhausted) once `budget` calls were made."""

    def __init__(self, gh, budget=None):
        self.gh, self.calls, self.budget = gh, 0, budget

    def __call__(self, path, **kw):
        if self.budget is not None and self.calls >= self.budget:
            raise BudgetExhausted()
        self.calls += 1
        return self.gh(path, **kw)


# ---------------------------------------------------------------- fetching
def fetch_dash_repo(repo, gh, sleep=0.0, commit_dates=None, max_lookups=DEFAULT_MAX_LOOKUPS,
                    now=time.time):
    """Fetch one repo's dashboard entry. Section failures are recorded in
    entry["errors"]; only a rate-limit GhError propagates."""
    owner, name, branch = repo["owner"], repo["name"], repo.get("branch")
    full = "%s/%s" % (owner, name)
    commit_dates = {} if commit_dates is None else commit_dates
    entry = {"owner": owner, "name": name, "default_branch": branch, "fetched_at": now(),
             "ci": None, "prs": [], "prs_truncated": False, "alerts": None, "alerts_note": None,
             "branches": [], "branches_truncated": False, "errors": []}

    def get(section, path, jq, fatal=SOFT_STATUSES, quiet=SOFT_STATUSES):
        try:
            return gh(path, jq=jq, fatal=fatal)
        except GhError as e:
            if e.rate_limited:
                raise
            if e.status not in quiet:
                entry["errors"].append("%s: %s" % (section, e))
            return None
        finally:
            time.sleep(sleep)

    if branch:
        d = get("ci", "repos/%s/actions/runs?branch=%s&per_page=20"
                % (full, urllib.parse.quote(branch, safe="")), cs.RUNS_JQ)
        runs = [r for r in (d or {}).get("runs") or []
                if cs.same_repo(r, full) and r.get("head_branch") in (None, branch)]
        done = cs.latest_completed(runs)
        if done:
            ci = cs.ci_summary(done)
            ci["updated_at"] = done.get("updated_at")
            ci["in_progress"] = bool(runs) and runs[0].get("status") != "completed"
            ci["failed_jobs"] = []
            if ci["conclusion"] in cs.RED and ci["run_id"]:
                jobs = get("ci jobs", "repos/%s/actions/runs/%s/jobs?per_page=100"
                           % (full, ci["run_id"]), JOBS_JQ) or []
                ci["failed_jobs"] = [j for j in jobs if j.get("conclusion") not in OK_JOB_CONCLUSIONS]
            entry["ci"] = ci
        elif runs:
            entry["ci"] = {"workflow": runs[0].get("name"), "conclusion": None, "in_progress": True,
                           "run_id": runs[0].get("id"), "url": runs[0].get("html_url"),
                           "sha": (runs[0].get("head_sha") or "")[:10], "failed_jobs": []}

    prs = get("prs", "repos/%s/pulls?state=open&per_page=%d" % (full, PAGE), PULLS_JQ) or []
    entry["prs"] = prs
    entry["prs_truncated"] = len(prs) >= PAGE

    try:
        entry["alerts"] = gh("repos/%s/dependabot/alerts?state=open&per_page=%d" % (full, PAGE),
                             jq=ALERTS_JQ, fatal=ALERT_NA_ALL) or []
    except GhError as e:
        if e.rate_limited:
            raise
        if e.status in ALERT_NA_ALL:
            entry["alerts_note"] = "n/a (%s)" % e
        else:
            entry["alerts_note"] = "error"
            entry["errors"].append("alerts: %s" % e)
    time.sleep(sleep)

    if branch:
        branches = get("branches", "repos/%s/branches?per_page=%d" % (full, PAGE), BRANCHES_JQ) or []
        entry["branches_truncated"] = len(branches) >= PAGE
        lookups = 0
        for b in branches:
            if b.get("name") == branch:
                continue
            key = "%s@%s" % (full, b.get("sha"))
            date = commit_dates.get(key)
            if date is None and lookups < max_lookups and b.get("sha"):
                lookups += 1
                try:
                    date = get("branches", "repos/%s/commits/%s" % (full, b["sha"]), COMMIT_DATE_JQ)
                except BudgetExhausted:
                    # optional lookups: keep the entry, leave the remaining dates unknown
                    lookups, date = max_lookups, None
                if isinstance(date, str):
                    commit_dates[key] = date
                else:
                    date = None
            entry["branches"].append({"name": b.get("name"), "sha": b.get("sha"), "date": date})
    return entry


# ---------------------------------------------------------------- summaries
def stale_branches(entry, stale_days, now):
    cutoff = now - stale_days * 86400
    out = []
    for b in entry.get("branches") or []:
        ts = parse_ts(b.get("date"))
        if b.get("name") != entry.get("default_branch") and ts is not None and ts < cutoff:
            out.append(dict(b, age_days=int((now - ts) // 86400)))
    return sorted(out, key=lambda b: (-b["age_days"], b["name"]))


def ci_state(entry):
    ci = entry.get("ci")
    if not ci:
        return "none"
    return ci.get("conclusion") or "running"


def summarize_entry(entry, stale_days, now):
    prs = entry.get("prs") or []
    alerts = entry.get("alerts")
    stale = stale_branches(entry, stale_days, now)
    state = ci_state(entry)
    return {
        "repo": "%s/%s" % (entry["owner"], entry["name"]),
        "owner": entry["owner"], "name": entry["name"],
        "default_branch": entry.get("default_branch"),
        "open_prs": len(prs), "prs_truncated": bool(entry.get("prs_truncated")),
        "ci": state, "ci_red": state in cs.RED,
        "ci_running": bool((entry.get("ci") or {}).get("in_progress")),
        "dependabot_prs": sum(1 for p in prs if is_dependabot_pr(p)),
        "dependabot_alerts": None if alerts is None else len(alerts),
        "stale_branches": len(stale),
        "unknown_branch_dates": sum(1 for b in entry.get("branches") or []
                                    if b.get("date") is None and b.get("name") != entry.get("default_branch")),
        "fetched_at": entry.get("fetched_at"),
        "errors": list(entry.get("errors") or []),
    }


def full_report(entry, stale_days, now):
    row = summarize_entry(entry, stale_days, now)
    row["details"] = {
        "ci": entry.get("ci"),
        "prs": entry.get("prs") or [],
        "alerts": entry.get("alerts"),
        "alerts_note": entry.get("alerts_note"),
        "stale_branches": stale_branches(entry, stale_days, now),
    }
    return row


_CI_RANK = {"none": 3, "success": 4}


def _sort_key(key):
    if key == "prs":
        return lambda r: -r["open_prs"]
    if key == "ci":
        return lambda r: 0 if r["ci_red"] else _CI_RANK.get(r["ci"], 1 if r["ci"] == "running" else 2)
    if key == "deps":
        return lambda r: -(r["dependabot_prs"] + (r["dependabot_alerts"] or 0))
    if key == "stale":
        return lambda r: -r["stale_branches"]
    return lambda r: 0


def sort_rows(rows, key="name", reverse=False):
    """Sort with the most interesting repos first (most PRs, red CI, ...); name breaks ties."""
    rows = sorted(rows, key=lambda r: r["repo"].lower())
    if key != "name":
        rows = sorted(rows, key=_sort_key(key))
    return rows[::-1] if reverse else rows


def needs_attention(r):
    return bool(r["ci_red"] or r["dependabot_prs"] or r["dependabot_alerts"]
                or r["stale_branches"] or r["errors"])


_FILTER_FNS = {
    "all": lambda r: True,
    "attention": needs_attention,
    "failing": lambda r: r["ci_red"],
    "prs": lambda r: r["open_prs"] > 0,
    "dependabot": lambda r: bool(r["dependabot_prs"] or r["dependabot_alerts"]),
    "stale": lambda r: r["stale_branches"] > 0,
    "errors": lambda r: bool(r["errors"]),
}


def filter_rows(rows, name="all", search=""):
    fn = _FILTER_FNS[name]
    s = (search or "").lower()
    return [r for r in rows if fn(r) and (not s or s in r["repo"].lower())]


def detail_lines(entry, stale_days, now):
    """Drill-down text as (text, style) pairs; style is a key of the TUI's attrs."""
    row = summarize_entry(entry, stale_days, now)
    L = [("%s  (default branch: %s)" % (row["repo"], row["default_branch"] or "-"), "bold"),
         ("fetched %s ago" % fmt_age(now - (row["fetched_at"] or now)), "dim"), ("", None)]
    ci = entry.get("ci")
    L.append(("CI (latest default-branch run)", "bold"))
    if not ci:
        L.append(("  no workflow runs on the default branch", "dim"))
    else:
        style = "red" if row["ci_red"] else ("green" if row["ci"] == "success" else None)
        L.append(("  %s: %s%s" % (ci.get("workflow"), row["ci"],
                                   "  (newer run in progress)" if ci.get("in_progress") and ci.get("conclusion") else ""),
                  style))
        L.append(("  %s" % (ci.get("url") or ""), "dim"))
        for j in ci.get("failed_jobs") or []:
            L.append(("    failed job: %s (%s)" % (j.get("name"), j.get("conclusion")), "red"))
    L.append(("", None))
    prs = entry.get("prs") or []
    L.append(("Open pull requests: %d%s" % (len(prs), "+" if row["prs_truncated"] else ""), "bold"))
    for p in prs:
        tag = " [dependabot]" if is_dependabot_pr(p) else ""
        tag += " [draft]" if p.get("draft") else ""
        L.append(("  #%s %s  (%s)%s" % (p.get("number"), p.get("title"), p.get("user"), tag), None))
    L.append(("", None))
    alerts = entry.get("alerts")
    if alerts is None:
        L.append(("Dependabot alerts: %s" % (entry.get("alerts_note") or "n/a"), "bold"))
    else:
        L.append(("Dependabot alerts: %d open" % len(alerts), "bold"))
        for a in alerts:
            L.append(("  [%s] %s (%s): %s" % (a.get("severity"), a.get("package"), a.get("ecosystem"),
                                               a.get("summary")), "red" if a.get("severity") in ("critical", "high") else None))
    L.append(("", None))
    stale = stale_branches(entry, stale_days, now)
    L.append(("Stale branches (no commits in %d days): %d" % (stale_days, len(stale)), "bold"))
    for b in stale:
        L.append(("  %s  last commit %s (%d days)" % (b["name"], (b.get("date") or "")[:10], b["age_days"]), None))
    if row["unknown_branch_dates"]:
        L.append(("  (%d branch date(s) not yet looked up; refresh again to fill in)"
                  % row["unknown_branch_dates"], "dim"))
    if entry.get("branches_truncated"):
        L.append(("  (only the first %d branches were checked)" % PAGE, "dim"))
    if row["errors"]:
        L.append(("", None))
        L.append(("Errors", "red"))
        for e in row["errors"]:
            L.append(("  " + e, "red"))
    return L


def fmt_age(secs):
    secs = max(0, int(secs))
    if secs < 90:
        return "%ds" % secs
    if secs < 5400:
        return "%dm" % (secs // 60)
    if secs < 172800:
        return "%dh" % (secs // 3600)
    return "%dd" % (secs // 86400)


# ---------------------------------------------------------------- backend
class Dash:
    """Repo list + cache + refresh logic shared by the TUI and --json mode."""

    def __init__(self, opts, gh=None, list_fn=None, now=time.time):
        self.opts = opts
        self.gh = gh or cs.gh_api
        self.list_fn = list_fn or cs.list_repos
        self.now = now
        self.cache = self.load_cache()
        self.repos = []
        self.rate = None
        self.notes, self.errors = [], []

    # cache io (reuses ci-status state helpers; never state.json)
    def load_cache(self):
        try:
            c = cs.load_state(self.opts.cache)
        except (OSError, ValueError):
            c = None
        if not isinstance(c, dict) or c.get("version") != CACHE_VERSION:
            c = {"version": CACHE_VERSION, "repos": {}, "repo_lists": {}, "commit_dates": {}}
        for k in ("repos", "repo_lists", "commit_dates"):
            c.setdefault(k, {})
        return c

    def save_cache(self):
        live = {"%s/%s@%s" % (e["owner"], e["name"], b.get("sha"))
                for e in self.cache["repos"].values() for b in e.get("branches") or []}
        self.cache["commit_dates"] = {k: v for k, v in self.cache["commit_dates"].items() if k in live}
        self.cache["updated"] = self.now()
        d = os.path.dirname(os.path.abspath(self.opts.cache))
        os.makedirs(d, exist_ok=True)
        cs.save_state(self.opts.cache, self.cache)

    def fresh(self, fetched_at):
        return fetched_at is not None and self.now() - fetched_at < self.opts.ttl

    def _list(self, acct, force, errors):
        cached = self.cache["repo_lists"].get(acct)
        if cached and (self.opts.offline or (not force and self.fresh(cached.get("fetched_at")))):
            return cached["repos"]
        if self.opts.offline:
            return []
        try:
            repos = self.list_fn(acct)
        except (GhError, OSError, ValueError) as e:
            if getattr(e, "rate_limited", False) or not cached:
                errors.append("%s: %s" % (acct, e))
            if cached:
                self.notes.append("repo list for %s from cache (%s)" % (acct, e))
                return cached["repos"]
            return []
        self.cache["repo_lists"][acct] = {"fetched_at": self.now(), "repos": repos}
        return repos

    def load_repos(self, force=False, errors=None):
        errors = self.errors if errors is None else errors
        self.repos = cs.select_repos(self.opts.owners, self.opts.skip, self.opts.only,
                                     lambda a: self._list(a, force, errors))
        return self.repos

    def refresh(self, force=False, only=None, progress=None):
        """Re-fetch repos whose cache entry is older than the TTL (all of them if
        `force`; just `only` if given). Stops early, keeping cached data, when the
        rate-limit reserve would be crossed or GitHub reports rate limiting."""
        self.notes, self.errors = [], []
        if only is None or not self.repos:
            self.load_repos(force=force and only is None)
        if self.opts.offline:
            self.notes.append("offline: showing cached data only")
            self._collect_errors()
            return
        todo = [r for r in self.repos
                if (only is None or "%s/%s" % (r["owner"], r["name"]) in only)
                and (force or not self.fresh((self.cache["repos"].get("%s/%s" % (r["owner"], r["name"])) or {})
                                             .get("fetched_at")))]
        if todo:
            self.rate = rate_info(self.gh)
        budget = None if self.rate is None else self.rate["remaining"] - self.opts.reserve
        used = 0
        for i, r in enumerate(todo):
            full = "%s/%s" % (r["owner"], r["name"])
            if budget is not None and budget - used < EST_CALLS_PER_REPO:
                self.notes.append("rate limit: %d API calls left (reserve %d); %d repo(s) not refreshed"
                                  % (self.rate["remaining"] - used, self.opts.reserve, len(todo) - i))
                break
            if progress and progress(i, len(todo), full) is False:
                self.notes.append("refresh cancelled; %d repo(s) not refreshed" % (len(todo) - i))
                break
            counted = CountingGh(self.gh, None if budget is None else budget - used)
            try:
                entry = fetch_dash_repo(r, counted, self.opts.sleep, self.cache["commit_dates"],
                                        self.opts.max_branch_lookups, self.now)
            except BudgetExhausted:
                self.notes.append("rate limit: reserve %d reached; %d repo(s) not refreshed"
                                  % (self.opts.reserve, len(todo) - i))
                break
            except GhError as e:
                self.notes.append("rate limited by GitHub after %d/%d repo(s); showing cached data"
                                  % (i, len(todo)))
                self.errors.append("%s: %s" % (full, e))
                break
            finally:
                used += counted.calls
            self.cache["repos"][full] = entry
        if todo:
            self.rate = rate_info(self.gh) or self.rate
        self._prune()
        self._collect_errors()
        self.save_cache()

    def _collect_errors(self):
        """Section errors of every listed repo's entry, refreshed now or served from cache."""
        for r in self.repos:
            full = "%s/%s" % (r["owner"], r["name"])
            self.errors.extend("%s: %s" % (full, e) for e in (self.entry(full) or {}).get("errors") or [])

    def _prune(self):
        listed = {"%s/%s" % (r["owner"], r["name"]) for r in self.repos}
        owners = {o.lower() for o in self.opts.owners}
        for full in list(self.cache["repos"]):
            if full.split("/", 1)[0].lower() in owners and full not in listed and not self.opts.only:
                del self.cache["repos"][full]

    def entry(self, full):
        return self.cache["repos"].get(full)

    def rows(self, sort="name", reverse=False, filt="all", search=""):
        now = self.now()
        rows = []
        for r in self.repos:
            e = self.entry("%s/%s" % (r["owner"], r["name"]))
            if e:
                rows.append(summarize_entry(e, self.opts.stale_days, now))
        return sort_rows(filter_rows(rows, filt, search), sort, reverse)

    def report(self):
        now = self.now()
        rows = self.rows(self.opts.sort, self.opts.reverse, self.opts.filter, self.opts.search)
        return {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "owners": list(self.opts.owners), "stale_days": self.opts.stale_days,
            "sort": self.opts.sort, "filter": self.opts.filter,
            "rate_limit": self.rate, "notes": self.notes, "errors": self.errors,
            "missing": sorted("%s/%s" % (r["owner"], r["name"]) for r in self.repos
                              if not self.entry("%s/%s" % (r["owner"], r["name"]))),
            "repos": [full_report(self.entry(r["repo"]), self.opts.stale_days, now) for r in rows],
        }


# ---------------------------------------------------------------- curses UI
HELP = [
    "org-dash keys",
    "",
    "  j/k, arrows     move            PgUp/PgDn, g/G   page / top / bottom",
    "  Enter, l        drill down      Esc, h, q        back (q on list: quit)",
    "  s / S           next sort key / reverse order",
    "  f / F           next / previous filter",
    "  /               search repo names (Enter to apply, Esc to clear)",
    "  r               refresh all repos (ignores the cache TTL)",
    "  R               refresh the selected repo",
    "  ?               this help",
    "",
    "Columns: PRS open PRs | CI latest completed default-branch run |",
    "DEPENDABOT open Dependabot PRs / open alerts (n/a: no access) | STALE branches",
    "",
    "press any key",
]


class TUI:
    def __init__(self, scr, dash, attrs=None):
        self.scr, self.dash = scr, dash
        self.attrs = attrs or {"red": curses.A_BOLD, "green": curses.A_NORMAL, "dim": curses.A_DIM,
                               "bold": curses.A_BOLD, "head": curses.A_REVERSE, "sel": curses.A_REVERSE}
        o = dash.opts
        self.sort = SORT_KEYS.index(o.sort)
        self.reverse = o.reverse
        self.filt = FILTERS.index(o.filter)
        self.search = o.search or ""
        self.sel, self.top = 0, 0
        self.detail, self.dtop = None, 0
        self.prompt = None
        self.help = False
        self.message = ""

    # helpers
    def put(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                self.scr.addnstr(y, x, text, max(0, w - x - (1 if y == h - 1 else 0)), attr)
            except curses.error:
                pass

    def rows(self):
        return self.dash.rows(SORT_KEYS[self.sort], self.reverse, FILTERS[self.filt], self.search)

    def body_height(self):
        return max(1, self.scr.getmaxyx()[0] - 3)

    def status_text(self):
        if self.message:
            return self.message
        bits = self.dash.notes + self.dash.errors
        if bits:
            return "%s%s" % (bits[0], "  (+%d more; see --json)" % (len(bits) - 1) if len(bits) > 1 else "")
        return "? help  s sort  f filter  / search  Enter details  r refresh  q quit"

    def header(self, shown):
        rate = self.dash.rate
        api = ("API %d/%d, resets %s" % (rate["remaining"], rate["limit"],
                                         time.strftime("%H:%M", time.localtime(rate["reset"])))
               if rate else "API ?")
        return "org-dash  %s | %d/%d repos | sort %s%s | filter %s%s | %s" % (
            ",".join(self.dash.opts.owners), shown, len(self.dash.repos), SORT_KEYS[self.sort],
            " (rev)" if self.reverse else "", FILTERS[self.filt],
            " /%s" % self.search if self.search else "", api)

    # drawing
    def draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        if self.help:
            for i, line in enumerate(HELP):
                self.put(i, 0, line)
        elif self.detail:
            self.draw_detail(h)
        else:
            self.draw_list(h, w)
        if self.prompt is not None:
            self.put(h - 1, 0, "/" + self.prompt, self.attrs["bold"])
        elif not self.help:
            self.put(h - 1, 0, self.status_text(), self.attrs["dim"])
        self.scr.refresh()

    def draw_list(self, h, w):
        rows = self.rows()
        self.sel = max(0, min(self.sel, len(rows) - 1))
        bh = self.body_height()
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + bh:
            self.top = self.sel - bh + 1
        self.put(0, 0, self.header(len(rows)), self.attrs["bold"])
        nw = max(12, w - 44)
        self.put(1, 0, ("%-*s %5s  %-14s %10s %5s" % (nw, "REPO", "PRS", "CI", "DEPENDABOT", "STALE"))
                 .ljust(w), self.attrs["head"])
        for i, r in enumerate(rows[self.top:self.top + bh]):
            idx = self.top + i
            alerts = "n/a" if r["dependabot_alerts"] is None else str(r["dependabot_alerts"])
            ci = r["ci"] + ("*" if r["ci_running"] and r["ci"] != "running" else "")
            flag = "!" if r["errors"] else ("~" if not self.dash.fresh(r["fetched_at"]) else " ")
            line = "%-*s %5s  %-14s %10s %5d" % (
                nw, (flag + r["repo"])[:nw], "%d%s" % (r["open_prs"], "+" if r["prs_truncated"] else ""),
                ci[:14], "%d/%s" % (r["dependabot_prs"], alerts), r["stale_branches"])
            if idx == self.sel:
                attr = self.attrs["sel"]
            elif r["ci_red"]:
                attr = self.attrs["red"]
            else:
                attr = 0
            self.put(2 + i, 0, line.ljust(w) if idx == self.sel else line, attr)
        if not rows:
            self.put(3, 2, "no repos match" if self.dash.repos else "no data yet - press r to refresh",
                     self.attrs["dim"])

    def draw_detail(self, h):
        e = self.dash.entry(self.detail)
        if not e:
            self.put(0, 0, "%s: no data" % self.detail)
            return
        lines = detail_lines(e, self.dash.opts.stale_days, self.dash.now())
        self.dtop = max(0, min(self.dtop, len(lines) - 1))
        for i, (text, style) in enumerate(lines[self.dtop:self.dtop + h - 1]):
            self.put(i, 0, text, self.attrs.get(style, 0) if style else 0)

    # actions
    def do_refresh(self, force=True, only=None):
        def progress(i, n, full):
            self.message = "refreshing %d/%d %s ... (q to cancel)" % (i + 1, n, full)
            self.draw()
            self.scr.nodelay(True)
            try:
                return self.scr.getch() not in (ord("q"), 27)
            finally:
                self.scr.nodelay(False)
        self.message = "refreshing ..."
        self.draw()
        self.dash.refresh(force=force, only=only, progress=progress)
        self.message = ""

    def handle_key(self, ch):
        """Returns False when the app should exit."""
        if self.help:
            self.help = False
            return True
        if self.prompt is not None:
            if ch in (10, 13, curses.KEY_ENTER):
                self.search, self.prompt = self.prompt, None
                self.sel = self.top = 0
            elif ch == 27:
                self.search, self.prompt = "", None
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                self.prompt = self.prompt[:-1]
            elif 32 <= ch < 127:
                self.prompt += chr(ch)
            return True
        self.message = ""
        if ch == ord("?"):
            self.help = True
        elif self.detail:
            return self.handle_detail_key(ch)
        else:
            return self.handle_list_key(ch)
        return True

    def handle_detail_key(self, ch):
        h = self.body_height()
        if ch in (27, ord("q"), ord("h"), curses.KEY_LEFT):
            self.detail = None
        elif ch in (ord("j"), curses.KEY_DOWN):
            self.dtop += 1
        elif ch in (ord("k"), curses.KEY_UP):
            self.dtop = max(0, self.dtop - 1)
        elif ch in (curses.KEY_NPAGE, ord(" ")):
            self.dtop += h
        elif ch == curses.KEY_PPAGE:
            self.dtop = max(0, self.dtop - h)
        elif ch == ord("R"):
            self.do_refresh(only={self.detail})
        elif ch == ord("r"):
            self.do_refresh()
        return True

    def handle_list_key(self, ch):
        rows = self.rows()
        h = self.body_height()
        if ch in (ord("q"), 27):
            return False
        if ch in (ord("j"), curses.KEY_DOWN):
            self.sel += 1
        elif ch in (ord("k"), curses.KEY_UP):
            self.sel -= 1
        elif ch == curses.KEY_NPAGE:
            self.sel += h
        elif ch == curses.KEY_PPAGE:
            self.sel -= h
        elif ch in (ord("g"), curses.KEY_HOME):
            self.sel = 0
        elif ch in (ord("G"), curses.KEY_END):
            self.sel = len(rows) - 1
        elif ch == ord("s"):
            self.sort = (self.sort + 1) % len(SORT_KEYS)
        elif ch == ord("S"):
            self.reverse = not self.reverse
        elif ch == ord("f"):
            self.filt = (self.filt + 1) % len(FILTERS)
            self.sel = 0
        elif ch == ord("F"):
            self.filt = (self.filt - 1) % len(FILTERS)
            self.sel = 0
        elif ch == ord("/"):
            self.prompt = self.search
        elif ch in (10, 13, curses.KEY_ENTER, ord("l"), curses.KEY_RIGHT) and rows:
            self.detail, self.dtop = rows[max(0, min(self.sel, len(rows) - 1))]["repo"], 0
        elif ch == ord("r"):
            self.do_refresh()
        elif ch == ord("R") and rows:
            self.do_refresh(only={rows[max(0, min(self.sel, len(rows) - 1))]["repo"]})
        self.sel = max(0, min(self.sel, len(rows) - 1))
        return True

    def run(self, initial_refresh=True):
        if initial_refresh:
            self.do_refresh(force=self.dash.opts.refresh)
        while True:
            self.draw()
            if not self.handle_key(self.scr.getch()):
                return


def _curses_main(scr, dash):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    attrs = None
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(1, curses.COLOR_RED, bg)
        curses.init_pair(2, curses.COLOR_GREEN, bg)
        attrs = {"red": curses.color_pair(1) | curses.A_BOLD, "green": curses.color_pair(2),
                 "dim": curses.A_DIM, "bold": curses.A_BOLD, "head": curses.A_REVERSE,
                 "sel": curses.A_REVERSE}
    TUI(scr, dash, attrs).run()


# ---------------------------------------------------------------- cli
def parse_args(argv):
    ap = argparse.ArgumentParser(prog="ci-status dash",
                                 description="Read-only org dashboard (curses TUI or --json).")
    ap.add_argument("--owners", "--accounts", nargs="+", default=cs.DEFAULT_ACCOUNTS)
    ap.add_argument("--skip", nargs="*", default=cs.DEFAULT_SKIP)
    ap.add_argument("--only", nargs="*", default=None, help="limit to these repo names")
    ap.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS,
                    help="branches without commits for this many days are stale (default %(default)s)")
    ap.add_argument("--json", action="store_true", help="print a JSON report instead of the TUI")
    ap.add_argument("--sort", choices=SORT_KEYS, default="name")
    ap.add_argument("--reverse", action="store_true")
    ap.add_argument("--filter", choices=FILTERS, default="all")
    ap.add_argument("--search", default="", help="substring filter on owner/repo")
    ap.add_argument("--cache", default=default_cache_path(), help="cache file (default %(default)s)")
    ap.add_argument("--ttl", type=int, default=DEFAULT_TTL, help="cache TTL in seconds (default %(default)s)")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache TTL and re-fetch everything")
    ap.add_argument("--offline", action="store_true", help="never call the API; show cached data only")
    ap.add_argument("--reserve", type=int, default=cs.MIN_RATE_REMAINING,
                    help="stop fetching when fewer API calls than this remain (default %(default)s)")
    ap.add_argument("--max-branch-lookups", type=int, default=DEFAULT_MAX_LOOKUPS,
                    help="per repo and refresh, new branch heads to date (default %(default)s)")
    ap.add_argument("--sleep", type=float, default=0.25, help="seconds between API calls")
    args = ap.parse_args(argv)
    if os.path.abspath(args.cache) in {os.path.join(HERE, "state.json"), os.path.join(HERE, "known.txt")}:
        ap.error("--cache must not point at state.json or known.txt")
    return args


def main(argv=None, gh=None, list_fn=None, out=sys.stdout, err=sys.stderr, now=time.time):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    dash = Dash(args, gh=gh, list_fn=list_fn, now=now)
    if args.json:
        try:
            dash.refresh(force=args.refresh)
        except OSError as e:
            err.write("org-dash: %s\n" % e)
            return 2
        out.write(json.dumps(dash.report(), indent=1) + "\n")
        if dash.errors:
            err.write("org-dash: %d problem(s): %s\n" % (len(dash.errors), "; ".join(dash.errors[:5])))
        return 2 if dash.errors else 0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        err.write("org-dash: not a terminal; use --json for non-interactive output\n")
        return 2
    curses.wrapper(_curses_main, dash)
    return 0


if __name__ == "__main__":
    sys.exit(main())
