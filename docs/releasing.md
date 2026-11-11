# How to release basis

A release is a `v*` tag on `main`. The release workflow, `.github/workflows/release.yml`, then builds each package
that says a release publishes it, installs them from those files alone, makes job repos from them, and creates the
GitHub release with the files attached. A release publishes basis-core, basis-job and basis-api. optim's version moves
with theirs, since it pins basis-core exactly, and CI builds and tests it on every pull request, but releases leave it
out until its design settles.

## The version and what a release publishes

basis has one version, the root `pyproject.toml`'s `[project] version`. Every member carries it, and each depends on
basis-core at exactly that version, so a repo's lock can only hold basis packages from one release. Each member's own
`pyproject.toml` says whether a release publishes it, as `[tool.basis-release] publish = true` or `false`; optim's says
`false`, so making it true is a reviewed change to `packages/optim/pyproject.toml`. The root's `dependencies`, which a
plain `uv sync` installs, list exactly the published members. `scripts/release.py` sets the version and checks all of
this. The commit hook runs its check whenever a `pyproject.toml`, `uv.lock` or the script
changes, and refuses a member, a pin or the lock at another version with one `ERROR` line and its fix.

```sh
uv run scripts/release.py check           # every member at one version, pinning basis-core at it, and the lock
uv run scripts/release.py version minor   # or patch, or X.Y.Z: every member, each pin, and uv.lock
uv run scripts/release.py packages        # what a release publishes, a name per line
```

## Cut a release

Prerequisites:

- Push access to github.com/jacobragsdale/basis, which a tag needs.
- A clone with the commit hook installed (`uv run pre-commit install`), and `gh`, logged in.
- `main`'s CI green.

To cut a release:

1. From the newest `main`, branch and set the version:

   ```sh
   git fetch origin
   git switch --create release-0.1.0 origin/main
   uv run scripts/release.py version minor
   ```

   It prints `basis 0.1.0: every member, each basis-core pin and uv.lock; commit them`. It also records the
   release's surface and a corpus of what it stores, in `docs/surface/0.1.0/` (`scripts/surface.py write`; see
   [the compatibility promise](#the-compatibility-promise)), and writes the example API's `openapi.released.json`, the sample's copy as this release gives it, which the example's contract test
   compares each later sample with, and which the release workflow's `check --tag` checks names the tag. For another bump, pass
   `patch`, or a version such as `0.1.0`. Generated repos cap basis below the next 0.x minor version, so the script
   refuses a 1.x version, and changes nothing, until those templates change. It works out every file's new text
   before it writes any, and puts every file back, `uv.lock` among them, when `uv lock` fails, so the workspace is
   never left at two versions.
2. Commit, push and open a pull request:

   ```sh
   git commit --all --message "basis 0.1.0"
   git push --set-upstream origin release-0.1.0
   gh pr create --fill
   ```

   The pull request touches release files, so the release workflow's dry run runs on it beside CI. Merge it when both
   are green.
3. Tag the commit the merge made, and push the tag:

   ```sh
   git fetch origin
   git tag --annotate v0.1.0 --message "basis 0.1.0" "$(gh pr view release-0.1.0 --json mergeCommit --jq .mergeCommit.oid)"
   git push origin v0.1.0
   ```

   Before it builds anything, the workflow refuses a tag whose commit is not on `main`, or whose ci.yml run did not
   pass; while that run is queued or still running, it waits for it, for up to 30 minutes. It then refuses a tag
   whose version differs from the tagged commit's `pyproject.toml` version, or that names `v0.0.0`, which no release
   does. Each refusal is one `ERROR` line, such as
   `ERROR: the tag v0.1.0 does not name the workspace's version, 0.0.0: ...`, and the run releases nothing.
4. Watch the run the tag started:

   ```sh
   gh run watch "$(gh run list --repo jacobragsdale/basis --workflow release.yml --event push --limit 1 --json databaseId --jq '.[0].databaseId')" --repo jacobragsdale/basis
   ```

   The release exists once its last job, `release`, passes. If a job failed on something outside basis, such as the
   network, rerun it with `gh run rerun RUN_ID --failed --repo jacobragsdale/basis`. If it failed on a defect, no
   release names the tag yet: delete the tag, fix the defect in a pull request, and tag the fixed commit as in step 3.

   ```sh
   git tag --delete v0.1.0 && git push origin :refs/tags/v0.1.0
   ```

5. Optional: add to the release's notes, such as which packages it changed nothing in. The workflow writes a first
   paragraph, and GitHub lists the pull requests merged since the last release (every one, for the first release).
   `gh release edit --notes-file` replaces the whole body, so edit what is there:

   ```sh
   gh release view v0.1.0 --repo jacobragsdale/basis --json body --jq .body > NOTES.md
   gh release edit v0.1.0 --repo jacobragsdale/basis --notes-file NOTES.md
   ```

To confirm the release, `gh release view v0.1.0 --repo jacobragsdale/basis` lists a wheel and an sdist for each name
`uv run scripts/release.py packages` prints, and `SHA256SUMS`: seven files for basis-api, basis-core and basis-job.
Then, in a scratch folder outside the clone, a job repo made from the tag passes its checks:

```sh
uvx --from 'git+https://github.com/jacobragsdale/basis@v0.1.0#subdirectory=packages/job' basis-job new repo loads
cd loads && uv run basis check
```

Its `pyproject.toml` takes basis-job from the tag, as
`basis-job = { git = "https://github.com/jacobragsdale/basis", subdirectory = "packages/job", tag = "v0.1.0" }`.

Publishing to the firm's internal feed needs the firm's environment, and waits on spike 0.1 in
[the roadmap](roadmap.md#phase-0-spikes), which says how the firm's desktops and pipelines reach the feed. The files
to publish are the release's, `gh release download v0.1.0 --repo jacobragsdale/basis`, checked with
`sha256sum --check SHA256SUMS`. Public PyPI has an unrelated `basis-core` at 0.1.0, so a feed that proxies PyPI could
serve it in place of basis's: once published, a repo made from the feed must lock `basis_core-0.1.0-py3-none-any.whl`
with the hash `SHA256SUMS` gives it.

## The compatibility promise

Every repo imports `basis.job` or `basis.api`, runs the `basis` command and sets `BASIS_*` variables, and a run store,
the ledgers and the check records outlive the code that wrote them. So what a release had stays:

- **The surface** is each part's `__all__`, every verb and its options, every setting's variable, and the fields of a
  run's manifest, of `basis check --full`'s record and of the ledger's row, as `uv run python scripts/surface.py`
  prints it. `docs/surface/VERSION/surface.json` keeps each release's, and `scripts/tests/test_surface.py` fails when
  any name a release had has left it. The ledger's columns never change, since its tables outlive every release.
- **A rename** goes through one release in which the old name is refused with its replacement: a variable by its
  part's `renamed`, a Python name by its package's `RENAMED`, which its `__getattr__` refuses as
  `basis.job.Old is renamed basis.job.New: use New`. Only the release after removes it, and lists it in the test's
  `RENAMED`.
- **What a release stored** is read by every later one: `docs/surface/VERSION/` keeps a run store (the example job's
  run on its fixture), a check record and the ledger's `CREATE TABLE` in each dialect, each written by that release's
  own code; `packages/job/tests/test_corpus.py` replays the run, and `packages/core/tests/test_corpus.py` reads the
  record and compares the ledger. A manifest and a check record carry their `format`; a format a basis does not know,
  which a newer basis wrote, is refused with the upgrade, rather than failing on a field it does not read.

## What the dry run proves

The release workflow runs without releasing on each pull request that touches a release file (the workflow,
`scripts/`, any `pyproject.toml`, `uv.lock`, or any package's code under `packages/*/src`, which is what a release
ships and all a generator reads), and
when run by hand from the Actions tab, or with `gh workflow run release.yml --ref BRANCH` once the workflow is on
`main`. Its token can only read, its `release` job runs only on a tag, and it uploads to no index. What leaves the
runner is the built files, kept as the run's `dist` artifact for seven days as any workflow's build output is. Its
jobs:

| Job | What passes |
|---|---|
| `build` | The gate, which passes on `main`'s newest commit that ci.yml passed on, and refuses a commit made on the runner, which is not on `main`. `release.py check`. Each published package's wheel and sdist, built with `uv build --no-sources --package NAME`, as an install from an index would build it, and no other file. |
| `verify` | With no checkout of the workspace: the wheels in one clean environment and the sdists in another, where `basis --version` names each package at the version and `basis --help` loads every part's verbs. A job repo made with `uvx --from` the basis-job wheel and `--index` naming a stand-in feed on the runner, which serves the files and sends every other name to PyPI, as the firm's feed proxies it: its lock holds basis-job and basis-core at the version, from that feed, and it passes `basis check`. A job repo made with `uvx` installing basis-job from that feed: without `--index` it fails with one `ERROR` line, since uv does not record which index a package came from, and with `--index` it passes `basis check`. |
| `from-git` | A scratch clone of the commit on the runner, tagged there as the version's release and never pushed: a job repo made with `uvx --from 'git+URL@vX.Y.Z#subdirectory=packages/job'` depends on that tag, and passes `basis check`. |
| `one-version` | `release.py version minor` moves every member, each pin and the lock, `release.py check` then passes, and so does every unit test, the release script's among them, at the next version. |

Only the tag's run proves four things. The gate passes the tag's own commit, waiting for ci.yml's run on it if it
must. `from-git` clones the tag from github.com, not from a scratch clone. A job repo made from the basis-job wheel
with no `--index` takes the release's tag from GitHub: a wheel's file, unlike an index, is no place a new repo could
take its packages from. And the `release` job creates the release with its files.

No run here proves the firm's feed: the stand-in has no authentication and no upstream of its own, and the desktops'
`uv.toml`, Windows and the feed's upload are spike 0.1's. A tag also leaves optim installable from git
(`#subdirectory=packages/optim`), as every commit does; what a release adds is the GitHub release's files and, at the
firm, the feed's.
