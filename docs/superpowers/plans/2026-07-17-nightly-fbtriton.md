# Nightly fbtriton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A scheduled GitHub Actions workflow that, once a day, selects the newest commit on `main` whose four required GPU/CI checks are green, builds a shrunk wheel matrix, and publishes it as a nightly `fbtriton` `.dev` package to a self-managed GitHub Releases + GitHub Pages index (pruned), independent of PyPI.

**Architecture:** A standalone `nightly-fbtriton.yml` runs `select → build → publish → prune`. Selection is a pure Python module (`nightly_select.py`, unit-tested) that reads per-commit `check-runs` (trigger-agnostic, latest-verdict-wins) and a `latest.json` boundary manifest read back from the rolling `nightly` GitHub Release. The build reuses `wheels_fb.yml` (extended with `ref`/`version`/matrix inputs) to build a specific commit with a pinned clean wheel version and a rewritten runtime `__version__`. Wheels are uploaded as assets on a single rolling `nightly` Release; a PEP 503 "simple" index is generated and deployed to GitHub Pages; assets older than the retention window are pruned. Formal releases still go to PyPI via the untouched `publish_fbtriton.yml`.

**Tech Stack:** GitHub Actions, `gh` CLI, Python 3.11 (stdlib only — `json`, `subprocess`, `datetime`), `cibuildwheel`, GitHub Releases + GitHub Pages (`actions/deploy-pages`).

## Global Constraints

- **Base version:** `3.8.0` (bumped in `setup.py` `TRITON_VERSION` and `python/triton/__init__.py`). Current `main` is `3.7.0` / `3.7.0+fb.beta`.
- **Wheel (packaging) version:** clean PEP 440 — nightly `3.8.0.dev<YYYYMMDD>` (no local segment).
- **Runtime `triton.__version__`:** nightly `3.8.0.dev<YYYYMMDD>+fb.git<shorthash>`; formal `3.8.0+fb`.
- **Commit provenance (no extra plumbing):** the **short** hash is stamped into `__version__` from `git rev-parse HEAD` at build time; the **full** SHA is recorded in the `latest.json` manifest on the rolling Release (also the boundary marker). No `TRITON_BUILD_COMMIT` env, no `project_urls` — the checkout already knows the commit.
- **Required checks (all four, hard gate):** `LIT Tests`, `h100-tlx-test`, `mi350-tlx-test`, `b200-tlx-test`.
- **Aggregation rule:** a check is green iff its **latest completed verdict** (`success`/`failure`/`timed_out`, by `completed_at`) as of the cut time is `success`; `cancelled` runs are ignored (not a verdict). Signals are read **SHA-keyed** and are **trigger-agnostic** (push or schedule).
- **Cut / trigger:** `cron: '0 4 * * *'` (04:00 UTC) + `workflow_dispatch`; job guard `if: github.repository_owner == 'facebookexperimental'`.
- **Nightly ABI matrix (shrunk, tunable):** `python: [cp312, cp313]` × `arch: [x86_64, aarch64]` (4 wheels, ~1 GB/day).
- **Retention (tunable):** keep the last **30** nightly builds; prune older Release assets.
- **Storage:** wheels = assets on a single rolling `nightly` GitHub Release (**one tag, never per-day tags**); index = PEP 503 `simple/` on GitHub Pages; boundary = `latest.json` asset on the Release.
- **Auth:** `GITHUB_TOKEN` only (Releases + Pages). **No PAT, no GitHub App, no PyPI Trusted Publisher for nightlies.**
- **Permissions:** the caller top-level must be `read-all` (wheels_fb declares `permissions: read-all`, and a reusable can't exceed its caller); `publish` and `report-nogreen` narrow to the writes they need (`contents/pages/id-token`, `issues`). Mismatch here causes a `startup_failure`.
- **No-op / failure behavior:** no new commits since boundary → exit silently (no release, no issue). New commits but none green within the cap → file/update a tracking issue via the existing `report-nightly-failure.yml`.
- **Repo VCS:** `git` (commit on a feature branch; do not commit to `main` directly).
- **Install (nightly):** `pip install --pre fbtriton --index-url https://facebookexperimental.github.io/triton/nightly/simple/`

---

### Task 1: Bump base version to 3.8.0

**Files:**
- Modify: `setup.py` (the `TRITON_VERSION = "3.7.0" + get_triton_version_suffix()` line)
- Modify: `python/triton/__init__.py:2` (`__version__ = '3.7.0+fb.beta'`)

**Interfaces:**
- Produces: base version string `3.8.0` used by every later task (wheel version derives from `setup.py`; runtime default from `__init__.py`).

- [ ] **Step 1: Bump `setup.py` base**

Change `TRITON_VERSION = "3.7.0" + get_triton_version_suffix()` to:

```python
TRITON_VERSION = "3.8.0" + get_triton_version_suffix()
```

- [ ] **Step 2: Bump `__init__.py` runtime literal**

Change `python/triton/__init__.py:2` to:

```python
__version__ = '3.8.0+fb'
```

- [ ] **Step 3: Verify both report 3.8.0**

Run:
```bash
grep -m1 '^TRITON_VERSION = ' setup.py
grep -m1 __version__ python/triton/__init__.py
```
Expected: prints `TRITON_VERSION = "3.8.0" + …` and `__version__ = '3.8.0+fb'`.

- [ ] **Step 4: Commit**

```bash
git commit setup.py python/triton/__init__.py -m "[release] Bump base version to 3.8.0 for nightly dev series"
```

---

### Task 2: Selection library (`nightly_select.py`) + tests

**Files:**
- Create: `.github/scripts/nightly_select.py`
- Test: `.github/scripts/test_nightly_select.py`

**Interfaces:**
- Consumes: for each candidate SHA, a `dict[check_name -> list[run]]` where a `run` is `{"completed_at": iso, "conclusion": str}` (fetched from `gh api .../commits/<sha>/check-runs`).
- Produces:
  - `check_green(runs: list[dict], cut: str) -> bool`
  - `commit_green(runs_by_check: dict, required: list[str], cut: str) -> bool`
  - `select(shas: list[str], fetch, required: list[str], cut: str, cap: int=100) -> str | None` — `shas` newest-first (already limited to commits after the boundary); `fetch(sha)` returns `runs_by_check`.

- [ ] **Step 1: Write the failing tests**

```python
# .github/scripts/test_nightly_select.py
from nightly_select import check_green, commit_green, select

CUT = "2026-07-17T04:00:00Z"

def r(t, c, e="schedule"):
    return {"completed_at": t, "conclusion": c, "event": e}

def test_latest_verdict_wins_over_earlier_success():
    runs = [r("2026-07-16T10:00:00Z", "success"), r("2026-07-16T14:00:00Z", "failure")]
    assert check_green(runs, CUT) is False

def test_cancelled_is_ignored_not_a_verdict():
    runs = [r("2026-07-16T00:59:00Z", "cancelled"), r("2026-07-17T03:01:00Z", "success")]
    assert check_green(runs, CUT) is True

def test_runs_after_cut_are_invisible():
    runs = [r("2026-07-17T18:46:00Z", "success")]  # after 04:00 cut
    assert check_green(runs, CUT) is False

def test_no_runs_is_not_green():
    assert check_green([], CUT) is False

def test_commit_green_requires_all_four():
    ok = {"LIT Tests": [r("2026-07-16T01:00:00Z", "success")],
          "h100-tlx-test": [r("2026-07-17T03:01:00Z", "success")],
          "mi350-tlx-test": [r("2026-07-16T01:00:00Z", "success")],
          "b200-tlx-test": [r("2026-07-16T01:10:00Z", "success")]}
    req = ["LIT Tests", "h100-tlx-test", "mi350-tlx-test", "b200-tlx-test"]
    assert commit_green(ok, req, CUT) is True
    missing = dict(ok); missing["b200-tlx-test"] = []
    assert commit_green(missing, req, CUT) is False

def test_select_walks_to_first_green():
    data = {"newest": {"LIT Tests": []},               # not green
            "older":  {"LIT Tests": [r("2026-07-16T01:00:00Z", "success")]}}
    req = ["LIT Tests"]
    assert select(["newest", "older"], lambda s: data[s], req, CUT) == "older"

def test_select_returns_none_when_cap_exhausted():
    req = ["LIT Tests"]
    assert select(["a", "b"], lambda s: {"LIT Tests": []}, req, CUT) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd .github/scripts && python3 -m pytest test_nightly_select.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nightly_select'`.

- [ ] **Step 3: Write the implementation**

```python
# .github/scripts/nightly_select.py
"""Point-in-time nightly commit selection (pure, unit-tested).

A check is green iff its LATEST completed verdict (success/failure/timed_out)
as of the cut time is success; cancelled runs are ignored.
"""
from __future__ import annotations

_VERDICT = ("success", "failure", "timed_out")


def check_green(runs, cut):
    verdicts = sorted(
        (x for x in runs
         if x.get("conclusion") in _VERDICT and x.get("completed_at") and x["completed_at"] <= cut),
        key=lambda x: x["completed_at"],
    )
    return bool(verdicts) and verdicts[-1]["conclusion"] == "success"


def commit_green(runs_by_check, required, cut):
    return all(check_green(runs_by_check.get(name, []), cut) for name in required)


def select(shas, fetch, required, cut, cap=100):
    for sha in shas[:cap]:
        if commit_green(fetch(sha), required, cut):
            return sha
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd .github/scripts && python3 -m pytest test_nightly_select.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git commit .github/scripts/nightly_select.py .github/scripts/test_nightly_select.py -m "[nightly] add point-in-time selection library + tests"
```

---

### Task 3: PEP 503 simple-index generator (`nightly_gen_simple_index.py`) + tests

**Files:**
- Create: `.github/scripts/nightly_gen_simple_index.py`
- Test: `.github/scripts/test_nightly_gen_simple_index.py`

**Interfaces:**
- Consumes: list of `(filename: str, url: str, sha256: str)` for the wheels currently in the rolling Release.
- Produces:
  - `render_project_index(files) -> str` — HTML for `/simple/fbtriton/index.html` (anchors with `#sha256=`).
  - `render_root_index() -> str` — HTML for `/simple/index.html` linking to `fbtriton/`.

- [ ] **Step 1: Write the failing tests**

```python
# .github/scripts/test_nightly_gen_simple_index.py
from nightly_gen_simple_index import render_project_index, render_root_index

def test_project_index_lists_files_with_hash():
    html = render_project_index([
        ("fbtriton-3.8.0.dev20260717-cp312-cp312-manylinux_2_28_x86_64.whl",
         "https://example/dl/fbtriton-...whl", "deadbeef"),
    ])
    assert "fbtriton-3.8.0.dev20260717-cp312" in html
    assert "#sha256=deadbeef" in html
    assert "<a href=" in html

def test_root_index_links_project():
    html = render_root_index()
    assert 'href="fbtriton/"' in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd .github/scripts && python3 -m pytest test_nightly_gen_simple_index.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
# .github/scripts/nightly_gen_simple_index.py
"""Generate a minimal PEP 503 'simple' index (HTML) for GitHub Pages."""
import html as _html

_PROJECT_TMPL = (
    "<!DOCTYPE html><html><head>"
    "<meta name='pypi:repository-version' content='1.0'>"
    "<title>Links for fbtriton</title></head><body>"
    "<h1>Links for fbtriton</h1>\n{anchors}\n</body></html>\n"
)


def render_project_index(files):
    anchors = "\n".join(
        '<a href="{url}#sha256={sha}">{name}</a><br>'.format(
            url=_html.escape(url), sha=_html.escape(sha), name=_html.escape(name))
        for name, url, sha in files
    )
    return _PROJECT_TMPL.format(anchors=anchors)


def render_root_index():
    return (
        "<!DOCTYPE html><html><body>"
        '<a href="fbtriton/">fbtriton</a><br>'
        "</body></html>\n"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd .github/scripts && python3 -m pytest test_nightly_gen_simple_index.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git commit .github/scripts/nightly_gen_simple_index.py .github/scripts/test_nightly_gen_simple_index.py -m "[nightly] add PEP 503 simple-index generator + tests"
```

---

### Task 4: Extend `wheels_fb.yml` to build a selected commit

**Files:**
- Modify: `.github/workflows/wheels_fb.yml`

**Interfaces:**
- Consumes (new `workflow_call` inputs): `ref` (SHA to build), `version` (**caller-provided** clean version to pin, e.g. `3.8.0.dev<date>`), `pythons` (JSON array, default full), `arches` (JSON array, default full).
- The version *scheme* is decided by the caller (`nightly-fbtriton.yml`); this generic builder only **applies** the given `version`. **No `commit` input** — the checkout is the target commit.
- Produces: wheel artifacts named `wheels-*` built from `ref`, with `setup.py` pinned to `version` and `__init__.py` `__version__` = `<version>+fb.git<shorthash>` (short hash from `git rev-parse HEAD`).

- [ ] **Step 1: Add `workflow_call` inputs**

Under `on:`, replace the bare `workflow_call:` with:

```yaml
  workflow_call:
    inputs:
      ref:      { type: string, required: false, default: "" }
      version:  { type: string, required: false, default: "" }
      pythons:  { type: string, required: false, default: '["cp310","cp311","cp312","cp313","cp314"]' }
      arches:   { type: string, required: false, default: '["x86_64","aarch64"]' }
```

- [ ] **Step 2: Drive the matrix from inputs**

Replace the `matrix:` block with:

```yaml
      matrix:
        python: ${{ fromJSON(inputs.pythons || '["cp310","cp311","cp312","cp313","cp314"]') }}
        arch:   ${{ fromJSON(inputs.arches  || '["x86_64","aarch64"]') }}
```

- [ ] **Step 3: Checkout the selected ref**

In the `Checkout` step, add:

```yaml
      - name: Checkout
        uses: actions/checkout@v6
        with:
          ref: ${{ inputs.ref || github.ref }}
```

- [ ] **Step 4: Pin version + rewrite runtime `__version__` (nightly path)**

Add a step after checkout, before the existing tag-pin step. It **applies** the caller-provided `version` (no derivation here); the short hash comes from `HEAD` (the checked-out target commit):

```yaml
      - name: Pin version + runtime __version__ (workflow_call builds)
        if: ${{ inputs.version != '' }}
        run: |
          short="$(git rev-parse --short=8 HEAD)"
          sed -i "s:^TRITON_VERSION = .*:TRITON_VERSION = '${{ inputs.version }}':" setup.py
          sed -i "s:^__version__ = .*:__version__ = '${{ inputs.version }}+fb.git${short}':" python/triton/__init__.py
          grep '^TRITON_VERSION' setup.py
          grep '^__version__' python/triton/__init__.py
```

- [ ] **Step 5: Validate workflow syntax**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/wheels_fb.yml')); print('yaml ok')"`
Expected: `yaml ok`. (If `actionlint` is available: `actionlint .github/workflows/wheels_fb.yml`.)

- [ ] **Step 6: Commit**

```bash
git commit .github/workflows/wheels_fb.yml -m "[nightly] wheels_fb: add ref/version/matrix inputs for nightly builds"
```

---

### Task 5: The `nightly-fbtriton.yml` orchestration workflow

**Files:**
- Create: `.github/workflows/nightly-fbtriton.yml`
- Create: `.github/scripts/nightly_run.py`
- Create: `.github/scripts/nightly_prune.py`
- Create: `.github/scripts/nightly_build_index.py`

**Interfaces:**
- Consumes: `nightly_select.py`, `nightly_gen_simple_index.py` (Tasks 2–3), `wheels_fb.yml` inputs (Task 4), the existing `report-nightly-failure.yml` (reusable).
- Produces: the rolling `nightly` GitHub Release (wheel assets + `latest.json`), the GitHub Pages `nightly/simple/` index, a pruned asset set, and (on no-green) a tracking issue.

- [ ] **Step 1: Write `nightly_run.py` (selection entrypoint)**

```python
#!/usr/bin/env python3
"""Nightly selection entrypoint.

Reads the boundary (last-shipped commit) from the rolling 'nightly' Release's
latest.json, enumerates new commits since then, walks them newest->oldest with
nightly_select, and emits GitHub Actions outputs: status (ship|noop|nogreen),
sha, version.
"""
import datetime
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nightly_select import select  # noqa: E402

REQUIRED = ["LIT Tests", "h100-tlx-test", "mi350-tlx-test", "b200-tlx-test"]
REPO = os.environ.get("GITHUB_REPOSITORY", "facebookexperimental/triton")
SIGNAL_REPO = os.environ.get("TRITON_SIGNAL_REPO") or REPO  # fork testing: point at upstream
CAP = 100


def sh(*args):
    return subprocess.run(args, capture_output=True, text=True).stdout


def gh_json(path):
    out = sh("gh", "api", path)
    return json.loads(out) if out.strip() else None


def boundary_commit():
    data = gh_json(f"repos/{REPO}/releases/tags/nightly")
    if not data:
        return None
    for asset in data.get("assets", []):
        if asset["name"] == "latest.json":
            raw = sh("gh", "api", asset["url"], "-H", "Accept: application/octet-stream")
            try:
                return json.loads(raw).get("commit")
            except json.JSONDecodeError:
                return None
    return None


def new_commits(boundary):
    rng = f"{boundary}..HEAD" if boundary else "HEAD"
    out = sh("git", "rev-list", "--max-count", str(CAP), rng)
    return [line for line in out.split() if line]


def fetch(sha):
    data = gh_json(f"repos/{SIGNAL_REPO}/commits/{sha}/check-runs?per_page=100") or {}
    by_check = {}
    for cr in data.get("check_runs", []):
        by_check.setdefault(cr["name"], []).append(
            {"completed_at": cr.get("completed_at"), "conclusion": cr.get("conclusion")})
    return by_check


def emit(**kv):
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


def base_version():
    m = re.search(r'^TRITON_VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"', open("setup.py").read(), re.M)
    return m.group(1) if m else "3.8.0"


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    cut = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    date = now.strftime("%Y%m%d")
    shas = new_commits(boundary_commit())
    if not shas:
        print("no new commits since last nightly -> noop")
        emit(status="noop")
        return
    sha = select(shas, fetch, REQUIRED, cut, CAP)
    if not sha:
        print(f"no all-green commit in {len(shas)} candidates -> nogreen")
        emit(status="nogreen")
        return
    # The nightly version *scheme* is decided here (the caller); wheels_fb applies it.
    version = f"{base_version()}.dev{date}"
    print(f"selected {sha} -> ship {version}")
    emit(status="ship", sha=sha, version=version)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write `nightly_prune.py`**

```python
#!/usr/bin/env python3
"""Delete wheel assets on the rolling 'nightly' release beyond the last N builds
(grouped by dev-date). latest.json and non-dated assets are left untouched.
"""
import collections
import json
import re
import subprocess
import sys

REPO = os.environ.get("GITHUB_REPOSITORY", "facebookexperimental/triton")
KEEP = int(sys.argv[1]) if len(sys.argv) > 1 else 30


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True).stdout


def main():
    rel = json.loads(sh("gh", "api", f"repos/{REPO}/releases/tags/nightly"))
    by_date = collections.defaultdict(list)
    for a in rel.get("assets", []):
        m = re.search(r"\.dev(\d{8})", a["name"])
        if m:
            by_date[m.group(1)].append(a)
    stale_dates = sorted(by_date)[:-KEEP] if len(by_date) > KEEP else []
    for date in stale_dates:
        for a in by_date[date]:
            sh("gh", "api", "-X", "DELETE", f"repos/{REPO}/releases/assets/{a['id']}")
            print("pruned", a["name"])
    print(f"kept {min(len(by_date), KEEP)} dev-date(s); pruned {len(stale_dates)}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Write `nightly_build_index.py`**

```python
#!/usr/bin/env python3
"""Fetch current 'nightly' release assets and write a PEP 503 simple index
(root + fbtriton/) under the given output directory, for GitHub Pages.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nightly_gen_simple_index import render_project_index, render_root_index  # noqa: E402

REPO = os.environ.get("GITHUB_REPOSITORY", "facebookexperimental/triton")


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True).stdout


def main(outdir):
    rel = json.loads(sh("gh", "api", f"repos/{REPO}/releases/tags/nightly"))
    files = [(a["name"], a["browser_download_url"], (a.get("digest") or "").replace("sha256:", ""))
             for a in rel.get("assets", []) if a["name"].endswith(".whl")]
    os.makedirs(os.path.join(outdir, "fbtriton"), exist_ok=True)
    with open(os.path.join(outdir, "index.html"), "w") as f:
        f.write(render_root_index())
    with open(os.path.join(outdir, "fbtriton", "index.html"), "w") as f:
        f.write(render_project_index(files))
    print(f"wrote index with {len(files)} wheels -> {outdir}")


if __name__ == "__main__":
    main(sys.argv[1])
```

- [ ] **Step 4: Write the workflow `nightly-fbtriton.yml`**

Note the `build` job passes **no `commit`** input (the short hash is derived from the checkout in `wheels_fb.yml`); the full SHA is recorded in `latest.json` by the `publish` job.

```yaml
name: Nightly fbtriton
on:
  schedule:
    - cron: '0 4 * * *'
  workflow_dispatch:

permissions:
  contents: write
  pages: write
  id-token: write
  issues: write

concurrency:
  group: nightly-fbtriton
  cancel-in-progress: false

jobs:
  select:
    if: github.repository_owner == 'facebookexperimental'
    runs-on: ubuntu-latest
    outputs:
      sha:     ${{ steps.pick.outputs.sha }}
      version: ${{ steps.pick.outputs.version }}
      status:  ${{ steps.pick.outputs.status }}
    steps:
      - uses: actions/checkout@v6
        with:
          fetch-depth: 0
      - name: Pick commit
        id: pick
        env:
          GH_TOKEN: ${{ github.token }}
        run: python3 .github/scripts/nightly_run.py

  build:
    needs: select
    if: needs.select.outputs.status == 'ship'
    uses: ./.github/workflows/wheels_fb.yml
    with:
      ref:     ${{ needs.select.outputs.sha }}
      version: ${{ needs.select.outputs.version }}
      pythons: '["cp312","cp313"]'
      arches:  '["x86_64","aarch64"]'

  publish:
    needs: [select, build]
    if: needs.select.outputs.status == 'ship'
    runs-on: ubuntu-latest
    permissions:
      contents: write
      pages: write
      id-token: write
    environment:
      name: github-pages
      url: ${{ steps.deploy.outputs.page_url }}
    steps:
      - uses: actions/checkout@v6
      - uses: actions/download-artifact@v6
        with:
          path: dist
          pattern: wheels-*
          merge-multiple: true
      - name: Ensure rolling 'nightly' release exists
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh release view nightly || gh release create nightly --title "fbtriton nightly" --notes "Rolling nightly wheels." --latest=false
      - name: Upload wheels + latest.json
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh release upload nightly dist/*.whl --clobber
          printf '{"commit":"%s","version":"%s"}\n' \
            "${{ needs.select.outputs.sha }}" "${{ needs.select.outputs.version }}" > latest.json
          gh release upload nightly latest.json --clobber
      - name: Prune assets older than retention window
        env:
          GH_TOKEN: ${{ github.token }}
        run: python3 .github/scripts/nightly_prune.py 30
      - name: Generate simple index from current assets
        env:
          GH_TOKEN: ${{ github.token }}
        run: python3 .github/scripts/nightly_build_index.py public/nightly/simple
      - uses: actions/upload-pages-artifact@v3
        with:
          path: public
      - id: deploy
        uses: actions/deploy-pages@v4

  report-nogreen:
    needs: select
    if: needs.select.outputs.status == 'nogreen'
    uses: ./.github/workflows/report-nightly-failure.yml
    with:
      issue_title: "Nightly fbtriton: no green commit in the last 100 commits"
      normalized_failure_id: "nightly-fbtriton-nogreen"
      workflow_name: "Nightly fbtriton"
      job_name: "select"
      run_url: "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}"
      ref: "refs/heads/main"
      sha: "${{ github.sha }}"
      event_name: "schedule"
      run_attempt: "${{ github.run_attempt }}"
      summary: "No commit in the walked window had all four required checks green as of the cut."
```

- [ ] **Step 5: Mark scripts executable + validate**

Run:
```bash
chmod +x .github/scripts/nightly_run.py .github/scripts/nightly_prune.py .github/scripts/nightly_build_index.py
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/nightly-fbtriton.yml')); print('yaml ok')"
python3 -m py_compile .github/scripts/nightly_run.py .github/scripts/nightly_prune.py .github/scripts/nightly_build_index.py
```
Expected: `yaml ok`, no compile errors.

- [ ] **Step 6: Commit**

```bash
git add --chmod=+x .github/scripts/nightly_run.py .github/scripts/nightly_prune.py .github/scripts/nightly_build_index.py
git commit .github/workflows/nightly-fbtriton.yml .github/scripts/nightly_run.py .github/scripts/nightly_prune.py .github/scripts/nightly_build_index.py -m "[nightly] add nightly-fbtriton orchestration workflow + run/prune/index scripts"
```

---

### Task 6: Enable GitHub Pages + bootstrap the first run

**Files:**
- None (repo settings + a manual dispatch). **Requires repo admin** (token needs admin:pages).

**Interfaces:**
- Consumes: everything above.
- Produces: a live `https://facebookexperimental.github.io/triton/nightly/simple/` index and the first nightly wheel.

- [ ] **Step 1: Enable Pages (GitHub Actions source)**

Run:
```bash
gh api -X POST repos/facebookexperimental/triton/pages -f build_type=workflow || \
gh api -X PUT  repos/facebookexperimental/triton/pages -f build_type=workflow
```
Expected: HTTP 201/204 (Pages configured for Actions deployment).

- [ ] **Step 2: Dispatch the workflow manually**

Run: `gh workflow run nightly-fbtriton.yml`
Then watch: `gh run watch "$(gh run list --workflow nightly-fbtriton.yml -L1 --json databaseId --jq '.[0].databaseId')"`
Expected: `select` → `ship`, `build` + `publish` succeed.

- [ ] **Step 3: Verify the index and install**

Run:
```bash
curl -sSf https://facebookexperimental.github.io/triton/nightly/simple/fbtriton/ | grep -c "\.whl"
python3 -m venv /tmp/nv && /tmp/nv/bin/pip install --pre fbtriton \
  --index-url https://facebookexperimental.github.io/triton/nightly/simple/
/tmp/nv/bin/python -c "import triton; print(triton.__version__)"
```
Expected: index lists ≥1 wheel; install succeeds; `__version__` prints `3.8.0.dev<date>+fb.git<hash>`.

---

### Task 7: Document the nightly install

**Files:**
- Modify: `README.md` (add a short "Nightly builds" section)

**Interfaces:**
- Consumes: the live index URL.
- Produces: user-facing install docs.

- [ ] **Step 1: Add a Nightly section to `README.md`**

```markdown
## Nightly builds (fbtriton)

Nightly `.dev` wheels are published to a self-managed index (not PyPI):

    pip install --pre fbtriton \
      --index-url https://facebookexperimental.github.io/triton/nightly/simple/

Each nightly is built from the newest `main` commit whose GPU/CI checks are all
green. `triton.__version__` reports `3.8.0.dev<YYYYMMDD>+fb.git<hash>`. Nightlies
are retained for ~30 days. Formal releases remain on PyPI (`pip install fbtriton`).
```

- [ ] **Step 2: Commit**

```bash
git commit README.md -m "[nightly] document self-managed nightly index install"
```

---

## Fork testing (safe full end-to-end)

The scripts auto-detect the publish repo from `GITHUB_REPOSITORY`, so the whole pipeline
can run on a personal fork without touching the upstream repo. A fork's own GPU CI is
guarded off, but it shares commit SHAs with upstream, so selection reads check-runs from a
`signal_repo` (upstream) while building/publishing to the fork:

1. Sync the fork's `main` with upstream, and push this branch's changes there (the workflow
   must be on the fork's default branch to be dispatchable).
2. Enable Pages (fork admin): `gh api -X POST repos/<you>/triton/pages -f build_type=workflow`.
3. Dispatch, pointing signals at upstream:
   ```bash
   gh workflow run nightly-fbtriton.yml -R <you>/triton -f signal_repo=facebookexperimental/triton
   gh run watch "$(gh run list -R <you>/triton --workflow nightly-fbtriton.yml -L1 --json databaseId --jq '.[0].databaseId')"
   ```
4. Verify the fork's `nightly` Release has `fbtriton-3.8.0.dev<date>-…whl` + `latest.json`, and
   `pip install --pre fbtriton --index-url https://<you>.github.io/triton/nightly/simple/`
   reports `triton.__version__ == 3.8.0.dev<date>+fb.git<hash>`.

The `select` job's guard allows `workflow_dispatch` on any owner (scheduled runs still only
fire on `facebookexperimental`), and `signal_repo` defaults to the running repo (so upstream
production behavior is unchanged).

## Self-Review

**1. Spec coverage:**
- Version bump 3.8.0 → Task 1. Clean wheel + runtime `+fb.git<hash>` → Tasks 1/4. Latest-verdict SHA-keyed selection → Task 2. Simple index → Task 3. Build a selected commit / shrunk matrix → Task 4. Cron/select/build/publish/prune/latest.json → Task 5. No-green issue reuse → Task 5 (step 4). No new commits → `noop` (Task 5, `nightly_run.py`). Pages enablement + install verification → Task 6. Docs → Task 7. Retention (30) → Task 5 (`nightly_prune.py`). Commit provenance → `__version__` short hash (Task 4) + `latest.json` full SHA (Task 5). No PAT → all jobs use `github.token`. ✅
- Bootstrap: on the first run `latest.json` doesn't exist → `boundary` is `None` → `new_commits(None)` walks `HEAD` capped at 100. Intended. ✅

**2. Placeholder scan:** No TBD/TODO; every code step has real code. ✅

**3. Type consistency:** `check_green`/`commit_green`/`select` match between Task 2 tests, implementation, and `nightly_run.py`. `render_project_index`/`render_root_index` match between Task 3 and `nightly_build_index.py`. Workflow input names (`ref`,`version`,`pythons`,`arches`) match between Task 4 (`wheels_fb.yml`) and Task 5 (`build` job `with:`). No `commit` input anywhere. ✅

## Notes / risks for the implementer

- **`digest` field:** `nightly_build_index.py` reads `asset.digest` (GitHub returns `sha256:...` on newer API); if absent, the `#sha256=` fragment is empty (pip still installs, just without hash pinning). Optionally compute the hash at upload time and store it in `latest.json`.
- **`workflow_call` + Pages:** the `deploy-pages` action must run in a job with `pages: write` + `id-token: write` and an `environment: github-pages` (set on the `publish` job).
- **Dependency confusion:** the nightly install intentionally uses `--index-url` (not `--extra-index-url`) since fbtriton has no runtime deps on Python ≥ 3.10; do not add `--extra-index-url pypi` unless a real dependency is introduced.
