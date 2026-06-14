# Research: Tag-triggered PyPI publishing via GitHub Actions Trusted Publishing (OIDC)

- **Query**: Current (2025/2026) best practice for tag-triggered automatic PyPI publishing via GitHub Actions using Trusted Publishing (OIDC), mapped onto BareAgent (Hatchling, uv, `github.com/525300887039/BareAgent`, default branch `main`).
- **Scope**: external (authoritative docs + project mapping)
- **Date**: 2026-06-14

## Repo facts (mapped from this codebase)

| Fact | Value | Source |
|---|---|---|
| PyPI project name (`[project].name`) | `bareagent` | `pyproject.toml:6` |
| Build backend | Hatchling (`hatchling>=1.27`) | `pyproject.toml:1-3` |
| Python floor | `>=3.12` | `pyproject.toml:9` |
| Git remote owner / repo | `525300887039` / `BareAgent` | `git remote -v` |
| Default branch | `main` | env |
| Existing CI uses | `actions/checkout@v5`, `astral-sh/setup-uv@v8.1.0`, uv | `.github/workflows/ci.yml` |
| Existing docs deploy job already sets per-job `permissions:` | yes | `.github/workflows/deploy-docs.yml:13-15` |
| Target release workflow path | `.github/workflows/release.yml` | task spec |

Note: the GitHub owner is the numeric login `525300887039` (not a different org). The PyPI publisher binding `owner` field must be exactly `525300887039` and `repository` exactly `BareAgent` (PyPI matches these case-insensitively but match them exactly to be safe).

> **Two viable, equally-blessed paths.** Because this repo is uv-native, both are first-class:
> - **Path A — PyPA canonical** (`python -m build` + `pypa/gh-action-pypi-publish`). Most widely documented; supports the separate build/publish-job security model and PEP 740 attestations out of the box.
> - **Path B — uv-native** (`uv build` + `uv publish`). Official Astral recommendation; fewer moving parts, reuses the `astral-sh/setup-uv` already in this repo's CI.
>
> Both use the **same** PyPI-side trusted-publisher setup. Recommendation for BareAgent below.

---

## Findings

### Q1 — The exact recommended workflow YAML

#### Current version pins (verified 2026-06-14)

| Action | Recommended pin | Notes |
|---|---|---|
| `pypa/gh-action-pypi-publish` | `@release/v1` (moving) **or** exact `@v1.14.0` / commit SHA | Latest release `v1.14.0` published 2026-04-07. `master` branch is **sunset** — do NOT use `@master`. PyPA docs use `@release/v1`; for max security pin to a commit SHA + Dependabot. |
| `actions/checkout` | `@v6` (PyPA guide) — repo currently on `@v5` | `@v5` works; `@v6` is current. Add `persist-credentials: false` on the build job. |
| `actions/setup-python` | `@v6` | Path A only. |
| `actions/upload-artifact` | `@v5` | Required to hand dists from build job to publish job. |
| `actions/download-artifact` | `@v6` | (PyPA guide pairs upload v5 + download v6; both current.) |
| `astral-sh/setup-uv` | `@v8.1.0` (commit `0880764...` for SHA-pin) | Path B; already used in `ci.yml`. |

`permissions: id-token: write` is **mandatory** for Trusted Publishing and must be on the **publish job only** (not workflow-global). It lets the runner mint the GitHub OIDC token that `gh-action-pypi-publish` (or `uv publish`) exchanges with PyPI for a short-lived, project-scoped upload token. With OIDC you supply **no** `user`/`password`.

#### Path A — PyPA canonical (build + publish split jobs) — RECOMMENDED for BareAgent

```yaml
# .github/workflows/release.yml
name: Publish to PyPI

on:
  push:
    tags:
      - 'v*'           # publish on any tag starting with v, e.g. v0.1.0

jobs:
  build:
    name: Build distribution
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
        with:
          persist-credentials: false
      - name: Set up Python
        uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - name: Install pypa/build
        run: python3 -m pip install build --user
      - name: Build sdist + wheel
        run: python3 -m build
      - name: Store the distribution packages
        uses: actions/upload-artifact@v5
        with:
          name: python-package-distributions
          path: dist/

  publish-to-pypi:
    name: Publish to PyPI
    needs: [build]
    runs-on: ubuntu-latest
    environment:
      name: pypi
      url: https://pypi.org/p/bareagent
    permissions:
      id-token: write           # MANDATORY for trusted publishing
    steps:
      - name: Download all the dists
        uses: actions/download-artifact@v6
        with:
          name: python-package-distributions
          path: dist/
      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
        # no user/password: OIDC trusted publishing is implicit
```

Because the workflow only triggers on `push: tags: ['v*']`, every run is already a tag push, so the extra `if: startsWith(github.ref, 'refs/tags/')` guard the PyPA "on: push" guide uses is **not needed here** (it's only needed when the same workflow also runs on branch pushes).

#### Path B — uv-native (single job; matches repo's existing toolchain)

This is the official Astral example (`docs.astral.sh/uv/guides/integration/github/` + `astral-sh/trusted-publishing-examples`), trimmed of the optional smoke tests:

```yaml
# .github/workflows/release.yml
name: Publish to PyPI

on:
  push:
    tags:
      - 'v*'

jobs:
  publish:
    runs-on: ubuntu-latest
    environment:
      name: pypi
    permissions:
      id-token: write          # MANDATORY for trusted publishing
      contents: read
    steps:
      - uses: actions/checkout@v6
      - name: Install uv
        uses: astral-sh/setup-uv@v8.1.0
      - name: Build sdist + wheel
        run: uv build
      - name: Publish to PyPI
        run: uv publish          # auto-detects OIDC; no token needed
```

`uv publish` auto-detects the GitHub OIDC environment and performs trusted publishing with no token. `uv build` produces both sdist and wheel into `dist/` by default.

> **Caveat (path choice).** Path B runs build + publish in **one job**, which the `gh-action-pypi-publish` README explicitly discourages for the PyPA action (build-in-publish-job is a privilege-escalation surface). uv's own example accepts the single-job model. If you want the strict "build in a privilege-less job, publish in a separate id-token job" hardening, prefer **Path A**, or split Path B into two uv jobs with `upload-artifact`/`download-artifact`. For a small single-maintainer CLI, Path B single-job is acceptable and is what Astral ships.

---

### Q2 — GitHub Environments (`environment: pypi`): yes, use it

**Recommended (strongly).** Add `environment: { name: pypi }` to the publish job.

- The PyPI trusted-publisher binding can (and should) include an **environment name**. When set, PyPI will only accept an OIDC token whose `environment` claim matches — so a workflow run that isn't in the `pypi` environment cannot publish, even if the repo/workflow filename match.
- GitHub Environments let you attach **deployment protection rules**: required reviewers (manual approval before the publish job runs), wait timers, and branch/tag restrictions. The PyPA guide explicitly says: *"For security reasons, you must require manual approval on each run for the `pypi` environment."* This is the main defence-in-depth win — a maintainer must click "Approve" before any tag actually ships to PyPI.
- The `url:` under `environment` (e.g. `https://pypi.org/p/bareagent`) is cosmetic — it just renders a nice deep-link in the GitHub deployments UI.
- PyPI docs: configuring an environment is *"optional but strongly recommended."* Treat it as required for this project.

Manual setup on GitHub side: repo **Settings → Environments → New environment → `pypi`**, then (optionally) add "Required reviewers". Do the same for `testpypi` if you add TestPyPI (Q4).

---

### Q3 — Exact MANUAL steps on pypi.org and test.pypi.org (pending publisher)

**Key fact: the project does NOT need to exist first.** Use a **"pending publisher"** — a trusted-publisher binding registered against your *account* before the project exists. On the **first** successful publish, PyPI auto-creates the project `bareagent` and converts the pending publisher into a normal one. No manual "prime upload" needed.

> Important caveats from PyPI docs:
> - A pending publisher does **not** reserve the name until first publish. If someone else registers `bareagent` before your first publish, your pending publisher is **invalidated**. (Check name availability first; `bareagent` may already be taken — verify at `https://pypi.org/project/bareagent/`.)
> - TestPyPI is a **separate** account/registry from PyPI — separate login, separate pending publisher.

#### On pypi.org (production)

1. Log in at pypi.org. Go to **`https://pypi.org/manage/account/publishing/`** (Account → Publishing). (If `bareagent` already exists and you own it, instead go to the project's **Settings → Publishing** and use "Add a publisher" — same form, minus the project-name field.)
2. Under **"Add a new pending publisher"**, choose **GitHub** and fill the form **exactly**:
   - **PyPI Project Name**: `bareagent`
   - **Owner**: `525300887039`  *(GitHub org/user that owns the repo)*
   - **Repository name**: `BareAgent`
   - **Workflow name**: `release.yml`  *(just the filename, not the full path; must be the file under `.github/workflows/`)*
   - **Environment name**: `pypi`  *(must match `environment.name` in the YAML; strongly recommended, technically optional)*
3. Click **Add**.

#### On test.pypi.org (optional, see Q4)

4. If you don't have a TestPyPI account, create one (it is **not** the same account as PyPI).
5. Go to **`https://test.pypi.org/manage/account/publishing/`** and repeat step 2 with the **same** Owner/Repository/Workflow values, but set:
   - **Environment name**: `testpypi`
6. Click **Add**.

After these, the pending publishers are "ready for first use" and will create the projects automatically on first publish.

#### On GitHub side (matching the bindings)

7. Repo **Settings → Environments**: create environment `pypi` (and `testpypi` if used). Optionally add required reviewers to `pypi`.
8. Ensure the workflow file is committed as `.github/workflows/release.yml` (filename must match the PyPI binding) on the default branch.

#### Then publish

9. `git tag -a v0.1.0 -m v0.1.0 && git push origin v0.1.0` (tag must match `v*`). The workflow runs, OIDC handshake succeeds, project `bareagent` is created on first publish.

> Field-matching is unforgiving: a mismatch in any of owner / repo / workflow-filename / environment causes a `trusted-publisher` auth failure (`invalid-publisher` / "not a valid OIDC token"). Re-check all five fields if the publish step 403s.

---

### Q4 — Also supporting TestPyPI

TestPyPI needs its **own separate trusted-publisher binding** (its own account/registry, see Q3 steps 4–6) and the publish step needs `repository-url: https://test.pypi.org/legacy/` (Path A) or `uv publish --publish-url https://test.pypi.org/legacy/` (Path B). Use a **separate** GitHub environment named `testpypi`.

**Gating recommendation.** Two common patterns; for BareAgent prefer **(a)**:

- **(a) `workflow_dispatch` (manual button) — recommended.** Add a separate publish-to-testpypi job (or a separate `release-testpypi.yml`) triggered by `on: workflow_dispatch`. You click "Run workflow" to push a test build whenever you want, decoupled from real version tags. Clean, no accidental TestPyPI spam, no version-collision headaches.
- **(b) Every push to `main`** (the PyPA guide's default `on: push` pattern publishes every commit to TestPyPI). This needs a **unique version per push** (e.g. `setuptools_scm` / dev versions) or it will collide, and the PyPA docs warn it can hit the TestPyPI **project size limit** on busy repos. With Hatchling's static `version = "0.1.0"`, repeated TestPyPI uploads of the same version would fail unless you bump the version or add `skip-existing: true`. Not recommended for this repo's static-version setup.
- A third option — **separate tag pattern** (e.g. real tags `v*` → PyPI, prerelease tags `*rc*`/`*a*`/`*b*` → TestPyPI) — works but is over-engineered for a single-maintainer CLI; `workflow_dispatch` is simpler.

Per the PyPA guide, the `testpypi` environment typically does **not** need manual-approval protection (it's meant to run freely), whereas `pypi` should.

Path A TestPyPI job snippet (add alongside `publish-to-pypi`, both depend on the same `build` job):

```yaml
  publish-to-testpypi:
    name: Publish to TestPyPI
    needs: [build]
    runs-on: ubuntu-latest
    environment:
      name: testpypi
      url: https://test.pypi.org/p/bareagent
    permissions:
      id-token: write
    steps:
      - uses: actions/download-artifact@v6
        with:
          name: python-package-distributions
          path: dist/
      - uses: pypa/gh-action-pypi-publish@release/v1
        with:
          repository-url: https://test.pypi.org/legacy/
```

(For a `workflow_dispatch`-gated variant, put this in its own workflow file triggered only by `workflow_dispatch`, with its own `build` step or artifact source.)

---

### Q5 — Common pitfalls

1. **`id-token: write` scope placement.** It is mandatory for OIDC, but put it on the **publish job only**, never workflow-global. The `gh-action-pypi-publish` README: *"only set the `id-token: write` permission in the job that does publishing."* Global `id-token: write` widens the OIDC blast radius to the build job (where untrusted build deps run). Missing it entirely → publish fails with "OIDC token not available" / `id-token` errors.

2. **Build in a separate job from publish.** Strongly advised (Path A does this). The publish job should be minimal (download artifact + publish) with no build/test deps, so injected build-time code can't piggyback on the elevated `id-token` permission. Running build + publish in one job is a documented privilege-escalation surface (Path B's single-job model trades this hardening for simplicity).

3. **Attestations (PEP 740) are ON by default.** As of recent `gh-action-pypi-publish` (v1.11+), `attestations: true` is the default for **Trusted Publishing** flows: it signs each dist with Sigstore using the same GitHub OIDC identity and uploads the attestations. Requirements/gotchas:
   - Only works with **Trusted Publishing** (not with API-token auth).
   - Needs the same `id-token: write` permission (already present).
   - To disable: `with: { attestations: false }`. Generally leave it ON — it's free supply-chain provenance. Disable only if a custom index rejects attestations.
   - uv (`uv publish`) does not generate PEP 740 attestations today; if attestations matter, prefer Path A.

4. **Artifact upload/download between jobs (Path A).** The publish job runs on a fresh runner with an empty `dist/`. You MUST `upload-artifact` in build and `download-artifact` into `dist/` in publish, or the action finds nothing to upload (or, worse, uploads stale/empty). Match the artifact `name` exactly across both. Keep upload/download action major versions compatible (PyPA guide currently pairs `upload-artifact@v5` + `download-artifact@v6`).

5. **Tag trigger vs release trigger.** This task wants `on: push: tags: ['v*']` — fires the moment a matching tag is pushed (`git push origin v0.1.0`), independent of GitHub Releases. The alternative `on: release: types: [published]` fires only when you cut a GitHub Release via the UI/API. Don't mix expectations:
   - With `tags: ['v*']`, just creating a GitHub Release (without pushing a NEW tag, e.g. releasing from an existing tag) may **not** fire it.
   - With pure tag trigger and a static version in `pyproject.toml`, remember to bump `version` before tagging, or the upload will 400 ("file already exists"). PyPI does **not** allow re-uploading the same version/filename — there is no overwrite; deletes are permanent and the version is burned.
   - The glob `'v*'` is a tag **filter** (e.g. matches `v0.1.0`, `v1.2.3rc1`); it is not a regex. Quote it in YAML.

6. **First-publish name race.** (Q3) Pending publisher does not reserve the name; if `bareagent` is taken before first publish, the binding silently becomes invalid. Verify availability and publish promptly after registering.

7. **Docker/Linux-only + no reusable workflows.** `gh-action-pypi-publish` is a Docker action → **Linux runners only** (`ubuntu-latest`); won't run on macOS/Windows runners or inside `container:`. Also, Trusted Publishing is **not supported from reusable workflows** — keep the `pypi-publish` job in a top-level (non-reusable) workflow.

## Caveats / Not Found

- I could not verify whether the PyPI project name `bareagent` is already claimed. **Action item for the user:** check `https://pypi.org/project/bareagent/` and `https://test.pypi.org/project/bareagent/` before registering the pending publisher (Q3 name-race caveat).
- Exact minimum `gh-action-pypi-publish` version where attestations flipped to default-on (~v1.11) is approximate; latest verified release is `v1.14.0` (2026-04-07) and attestations are default-on there. `@release/v1` always tracks the latest v1.
- The repo's existing CI pins `actions/checkout@v5` and `astral-sh/setup-uv@v8.1.0`; the PyPA guide uses `checkout@v6`/`setup-python@v6`. Both work; pick consistently. SHA-pinning every action + Dependabot is the maximal-security stance (mentioned, not mandated).

### External References

- PyPA guide (canonical): https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/ — full build+publish-to-PyPI+TestPyPI workflow, environment setup, manual approval requirement.
- `pypa/gh-action-pypi-publish` release/v1 README: https://github.com/pypa/gh-action-pypi-publish/blob/release/v1/README.md — OIDC usage, `id-token: write`, attestations-default-on, non-goals (separate-job/Docker/reusable-workflow constraints). Latest release `v1.14.0`.
- PyPI Trusted Publishers docs:
  - Adding a publisher (existing project): https://docs.pypi.org/trusted-publishers/adding-a-publisher/
  - Pending publisher (create project on first publish): https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/
- uv-native path: https://docs.astral.sh/uv/guides/integration/github/ (Publishing to PyPI) + example repo https://github.com/astral-sh/trusted-publishing-examples (`.github/workflows/release.yml`, `on: push: tags: ['v*']`, `uv build` + `uv publish`).
