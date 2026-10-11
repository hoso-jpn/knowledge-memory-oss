# Releasing and updating

This is the maintainer checklist for landing changes and cutting a release of this repository.
It assumes only what is visible in the public repository: Issues, pull requests, GitHub Actions,
tags and GitHub Releases.

A release here means an annotated Git tag plus a GitHub Release with notes. Nothing is published
to PyPI or another package registry. Publishing to a registry would be a separate decision with
its own Issue.

`v0.1.0`, the first public release, started from a single reviewed source-tree commit followed by
two small commits on `main`, with no pull requests; its tag and GitHub Release use the format in
sections 6 and 7. Changes after `v0.1.0` go through section 1.

## 1. Landing a change

Every change, including documentation and release preparation, goes through a pull request.

1. Start from an Issue that states the purpose and acceptance criteria.
2. Branch from the latest `main` (`feat/…`, `fix/…`, `docs/…`, `ci/…`, `chore/…`, with the Issue
   number).
3. Open a Draft PR. One PR covers one purpose. Its description says:
   - what changed and why;
   - the tests and commands actually run, and where;
   - environments that were not checked;
   - `Closes #N` for the Issue it completes.
4. Get an independent review. The reviewer reads the actual base…head diff against the Issue's
   acceptance criteria, not the PR description. If no second person is available, do a separate
   review pass after stepping away from the implementation, and say so in the PR.
5. Classify findings as blocker / should fix before merge / follow-up / no action. Fix the first
   two on the same branch with new commits. Do not force-push or rewrite published history.
   When the head changes, review again; a fix can break something elsewhere.
6. Open follow-up Issues only for findings that have value on their own. Do not widen the PR.
7. Merge when all of these hold, rechecked just before merging:
   - the head SHA is the one that was reviewed;
   - every job of the `verify` and `package` workflows passes on that head (the PR's checks list
     shows them as `tests (3.11)`, `build`, `install whl on 3.11` and so on);
   - no unresolved review threads and no open blocker findings;
   - the PR is not a draft and is mergeable.

   `gh pr merge <N> --merge --match-head-commit <reviewed SHA>` refuses to merge if the head
   moved after review.
8. After merging, check that CI on `main` passes and that the Issue was closed.

### Bringing in a reviewed source tree

Larger changes may be prepared outside this repository. They come in as a normal public PR: a
reviewed, sanitized source tree committed on a branch, with the same description, review and CI
as any other change. The PR describes what changes in this repository. It does not reference
history, issue numbers, hosts or tooling that are not public. Review it against
[CONTRIBUTING.md](../CONTRIBUTING.md#privacy-and-test-data) like any other contribution.

## 2. Release overview

Do these in order. Each step links to its details below.

1. Merge the changes that belong in the release through section 1.
2. Open a release Issue and draft the release notes ([section 5](#5-release-notes)) from the
   changes merged on `origin/main`. Keep the notes file outside the checkout.
3. Open a version PR that changes the version and the version text ([section 4](#4-version)).
   Link the release Issue with `Refs #N`, not `Closes`, so it stays open until the release is
   published. Put the draft notes in its description so they are reviewed with it.
4. Merge the version PR and write down its merge commit SHA. This is the release commit.
5. Check the release commit ([section 3](#3-checks-on-the-release-commit)).
6. Finish the notes: regenerate the change list up to the release SHA and fill in the verified
   environments from step 5. These changes are reviewed again when reading the draft in step 8.
7. Tag exactly that SHA and push the tag ([section 6](#6-tag)).
8. Create a draft GitHub Release from the tag, read it, then publish it ([section 7](#7-github-release)).
   Close the release Issue.

If anything fails along the way, see [Stop conditions](#stop-conditions) and
[Rollback and recovery](#rollback-and-recovery).

## 3. Checks on the release commit

Run these against the release commit from step 4 of the overview. Stop and fix things first if
any of them fail.

### Repository state

- Every job of the `verify` and `package` workflows passed on the release commit:

  ```bash
  gh run list --commit <release SHA>
  ```

- No open PR or Issue is marked as blocking the release.

### Security settings

- Private vulnerability reporting is enabled (Settings → Code security → Private vulnerability
  reporting), and [SECURITY.md](../SECURITY.md) still names it as the reporting route.
- Secret scanning and push protection are enabled.
- No open security advisory or unresolved vulnerability report affects the release (Security
  tab → Advisories).

```bash
gh api repos/<owner>/<repo>/private-vulnerability-reporting   # {"enabled": true}
gh api repos/<owner>/<repo> --jq .security_and_analysis       # secret_scanning and push protection "enabled"
gh api repos/<owner>/<repo>/security-advisories --jq '.[] | {ghsa_id, state, summary}'
```

### Fresh clone

Check the release commit from a fresh clone, not from your working copy, once with each supported
Python (3.11, 3.12, 3.13) where they are installed. Use a directory that does not exist yet:

```bash
git clone https://github.com/<owner>/<repo>.git km-release-check
cd km-release-check
git checkout --detach <release SHA>
python3.11 -m venv .venv          # repeat with python3.12 and python3.13 in fresh clones
.venv/bin/python -m pip install -e '.[mcp]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python examples/demo.py
```

Then run the Quick start commands from the README as written, and the package artifact check
below in the same clone, so the artifacts come from the release commit. Delete the clone
afterwards.
CI already runs the tests on all three versions; this check catches what CI cannot, such as a
README command that only works from a developer's checkout.

### Package artifacts

The `package` workflow builds the wheel and sdist once and installs each into a clean venv on
Python 3.11–3.13. To repeat it locally, run this once, inside one fresh clone of the release
commit and with that clone's venv (see [CONTRIBUTING.md](../CONTRIBUTING.md#package-artifacts)).
The wheel is pure Python, so one build is enough; `install` checks it with the Python that runs
the script:

```bash
rm -rf dist build src/*.egg-info
.venv/bin/python -m pip install build twine
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
.venv/bin/python scripts/verify_distribution.py contents dist
.venv/bin/python scripts/verify_distribution.py install dist/knowledge_memory-*.whl
.venv/bin/python scripts/verify_distribution.py install dist/knowledge_memory-*.tar.gz
```

Do not upload these files anywhere. A locally built sdist records your OS user name in its tar
headers. If artifacts are ever attached to a release, use the ones the `package` workflow built
for the release commit; it keeps them for 7 days.

### Documentation

- README Quick start, demo and MCP examples match the current CLI.
- "What it does not guarantee" and "Verification status" in both `README.md` and `README.ja.md`
  still describe what was actually verified. Do not widen them for the release.

## 4. Version

- Versions follow `MAJOR.MINOR.PATCH`. While the major version is 0, increase MINOR for
  incompatible changes or notable new features and PATCH for fixes and documentation-only
  releases. State incompatible changes in the release notes either way.
- Tags are `v` + the version (`v0.2.0`) and must match `pyproject.toml` on the tagged commit.
- A tag name that was ever pushed is never reused, even if the tag was deleted.

The version PR (for example `chore: release 0.2.0` on a `chore/…` branch) changes:

- `pyproject.toml`: `[project] version`. This is the only version the package itself carries;
  there is no `__version__` in `src/`.
- The version prose, where it names the current release:
  - `README.md` and `README.ja.md`, section "Status";
  - `SECURITY.md`, section "Supported versions".

Search for the old version to catch anything new (for `0.1.0`: `-e '0\.1\.0' -e 'v0\.1'`):

```bash
git grep -n -e '<old X\.Y\.Z>' -e 'v<old X\.Y>' -- ':!docs/releasing.md'
```

Recorded results under `evaluation/` name the package version they were measured with. Leave
them unchanged.

## 5. Release notes

Write notes for a reader who has not followed the PRs. List what changed since the previous tag:

```bash
git log --oneline --first-parent v<previous>..<release SHA>
```

The `v0.1.0` notes had a one-paragraph summary, **Highlights**, **Current scope**, **Getting
started** (which also pointed to private vulnerability reporting) and **License**. Keep those and,
from the second release on, add the sections marked *new*. The change list and verified
environments are finished only after the release commit is checked (overview step 6):

- one-paragraph summary of what knowledge-memory is;
- **Changes** since the previous release, linking the PRs or Issues (*new*);
- **Upgrade notes**: incompatible changes, index rebuilds or configuration changes, if any (*new*);
- **Highlights** of what the release can do;
- **Current scope**: single user and small vaults, literal search by default, synthetic-only
  tests and evaluations, `scope` is not access control, zero hits do not prove absence;
- **Verified environments**, and what was not verified (*new*);
- **Supported versions**: which releases receive security fixes, as [SECURITY.md](../SECURITY.md)
  says the release notes will state (*new*);
- **Getting started**: point to the README Quick start and the demo;
- **Security**: report vulnerabilities through private vulnerability reporting (SECURITY.md);
  in `v0.1.0` this was a sentence under Getting started (*split out*);
- **License**: Apache-2.0.

Do not claim quality on real data, platforms that CI does not run, or guarantees the README does
not make. Keep the notes file outside the checkout; it is not committed.

## 6. Tag

Tag the release commit by its SHA, not whatever `main` points to now, so a PR merged in the
meantime cannot end up in the release:

```bash
git fetch origin
SHA=<release SHA>                                   # merge commit of the version PR
git show "$SHA:pyproject.toml" | grep '^version'    # must match the tag
git merge-base --is-ancestor "$SHA" origin/main || echo "NOT on main: stop"
git tag -a v0.2.0 "$SHA" -m "knowledge-memory v0.2.0"
```

Push only if the version matched, the commit is on `main` and the tag points at it:

```bash
test "$(git rev-parse 'v0.2.0^{commit}')" = "$SHA" && git push origin v0.2.0
```

Use an annotated tag with the message `knowledge-memory vX.Y.Z`, as `v0.1.0` does. Push only the
tag you just created, never `--tags`.

## 7. GitHub Release

Create the release as a draft from the pushed tag, read it on GitHub, then publish:

```bash
gh release create v0.2.0 --verify-tag --draft \
  --title "knowledge-memory v0.2.0" --notes-file ../release-notes-0.2.0.md
gh release view v0.2.0 --web        # check rendering and links
gh release edit v0.2.0 --draft=false
```

`v0.1.0` has no attached files; users install from the tagged source. Attaching built
distributions would be a separate decision.

After publishing:

- the release page shows the intended tag, and the tag points to the release commit;
- a fresh clone of the tag passes the Quick start;
- the version is not changed again until the next version PR.

## Stop conditions

Do not tag or publish, or stop a release in progress, when:

- any check fails on the release commit, or the commit is not the one that was reviewed;
- a blocker or should-fix review finding is open;
- the fresh clone or package verification fails;
- the diff or release notes contain credentials, private data, real Notion IDs, local paths or
  references to non-public history;
- an unresolved vulnerability report affects the release;
- `pyproject.toml` and the tag disagree;
- the release notes claim more than was verified.

Fix the problem through a normal PR and restart from the [overview](#2-release-overview).

## Rollback and recovery

Tags and releases are copied as soon as someone fetches them, so fix forward:

- **Tag created but not pushed**: delete it locally (`git tag -d vX.Y.Z`) and start again.
- **Tag pushed, no published release yet**: do not move it. Leave the tag unused, fix through a
  PR and release the next PATCH version. If the tag must go (for example it points at a commit
  with private data), delete it with `git push origin :refs/tags/vX.Y.Z` and `git tag -d vX.Y.Z`,
  say why in a public Issue, and never reuse that version number. Deleting the tag does not remove
  the commit from `main`; for private data follow the secret bullet below.
- **Draft release**: edit or delete the draft freely. `gh release delete vX.Y.Z` keeps the pushed
  tag; then follow the previous bullet.
- **Published release with a defect**: do not move or reuse the tag. Fix through a PR, release a
  new PATCH version, and edit the old release notes to point to it. Mark the old release as a
  pre-release or add a warning at the top if users should avoid it.
- **Wrong release notes**: edit the GitHub Release; the tag stays.
- **Secret or private data in a release**: revoke the credential first, then follow
  [SECURITY.md](../SECURITY.md#if-a-secret-was-exposed). Deleting the release or tag does not
  undo the exposure. If the release and tag must still be deleted
  (`gh release delete vX.Y.Z --cleanup-tag`), say so in a public Issue so users know why they
  disappeared, and never reuse the version number.
- **A change on `main` that breaks things**: revert it with a normal PR (`git revert -m 1 <merge>`)
  rather than rewriting history.
