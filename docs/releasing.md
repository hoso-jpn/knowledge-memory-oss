# Releasing and updating

This is the maintainer checklist for landing changes and cutting a release of this repository.
It assumes only what is visible in the public repository: Issues, pull requests, GitHub Actions,
tags and GitHub Releases.

A release here means an annotated Git tag plus a GitHub Release with notes. Nothing is published
to PyPI or another package registry. Publishing to a registry would be a separate decision with
its own Issue.

`v0.1.0`, the first public release, started from a single reviewed source-tree commit followed by
two small commits on `main`; its tag and GitHub Release use the format in sections 4–6. Changes
after `v0.1.0` go through section 1.

## 1. Landing a change

Every change, including documentation and release preparation, goes through a pull request.

1. Start from an Issue that states the purpose and acceptance criteria.
2. Branch from the latest `main` (`feat/…`, `fix/…`, `docs/…`, `ci/…`, with the Issue number).
3. Open a Draft PR. One PR covers one purpose. Its description says:
   - what changed and why;
   - the tests and commands actually run, and where;
   - environments that were not checked;
   - `Closes #N` for the Issue it completes.
4. Get an independent review. The reviewer reads the actual base…head diff against the Issue's
   acceptance criteria, not the PR description. If no second person is available, do a separate
   review pass after stepping away from the implementation, and say so in the PR.
5. Classify findings as blocker / should fix before merge / follow-up / no action. Fix the first
   two on the same branch with new commits. Never force-push a branch that is under review.
   When the head changes, review again; a fix can break something elsewhere.
6. Open follow-up Issues only for findings that have value on their own. Do not widen the PR.
7. Merge when all of these hold, rechecked just before merging:
   - the head SHA is the one that was reviewed;
   - all checks (`verify`, `package`) pass on that head;
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

## 2. Before a release

Stop and fix things first if any of these fail (see [Stop conditions](#stop-conditions)).

### Repository state

- `main` is green for both `verify` and `package` on the commit you intend to release.
- No open PR or Issue is marked as blocking the release.
- List the merged PRs since the previous tag; they are the input for the release notes:

  ```bash
  git log --oneline --first-parent v<previous>..origin/main
  ```

### Security settings

- Private vulnerability reporting is enabled (Settings → Code security → Private vulnerability
  reporting), and [SECURITY.md](../SECURITY.md) still names it as the reporting route:

  ```bash
  gh api repos/<owner>/<repo>/private-vulnerability-reporting   # {"enabled": true}
  ```

- Secret scanning and push protection are still enabled.
- No open security advisory affects the release.

### Fresh clone

Check the release candidate from a fresh clone, not from your working copy, with each supported
Python (3.11, 3.12, 3.13) where possible:

```bash
git clone https://github.com/<owner>/<repo>.git /tmp/km-release-check
cd /tmp/km-release-check
git checkout <release commit>
python -m venv .venv
.venv/bin/python -m pip install -e '.[mcp]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python examples/demo.py
```

Then run the Quick start commands from the README as written. Delete the clone afterwards.

### Package artifacts

The `package` workflow already builds the wheel and sdist once and installs each into a clean venv
on Python 3.11–3.13. To repeat it locally (see [CONTRIBUTING.md](../CONTRIBUTING.md#package-artifacts)):

```bash
rm -rf dist build src/*.egg-info
python -m pip install build twine
python -m build
python -m twine check dist/*
python scripts/verify_distribution.py contents dist
python scripts/verify_distribution.py install dist/knowledge_memory-*.whl
python scripts/verify_distribution.py install dist/knowledge_memory-*.tar.gz
```

Do not upload these files anywhere. A locally built sdist records your OS user name in its tar
headers, so prefer the CI-built artifacts if artifacts are ever attached to a release.

### Documentation

- README Quick start, demo and MCP examples match the current CLI.
- "What it does not guarantee" and "Verification status" in both `README.md` and `README.ja.md`
  still describe what was actually verified. Do not widen them for the release.

## 3. Version

The package version lives only in `pyproject.toml` (`[project] version`).

- Versions follow `MAJOR.MINOR.PATCH`. While the major version is 0, increase MINOR for
  incompatible changes or notable new features and PATCH for fixes and documentation-only
  releases. State incompatible changes in the release notes either way.
- Change the version in its own PR (for example `chore: release 0.2.0`), after the changes it
  covers have merged. The PR body contains the draft release notes so they get reviewed.
- Tags are `v` + the version (`v0.1.0`) and must match `pyproject.toml` on the tagged commit.

## 4. Tag

Tag the merge commit of the version PR on `main`, after CI on that commit has passed:

```bash
git fetch origin
git checkout --detach origin/main
git log -1 --format='%H %s'                     # the commit you reviewed and CI checked
grep '^version' pyproject.toml                  # must match the tag
git tag -a v0.2.0 -m "knowledge-memory v0.2.0"
git push origin v0.2.0
```

Use an annotated tag with the message `knowledge-memory vX.Y.Z`, as `v0.1.0` does. Push only the
tag you just created, not `--tags`.

## 5. Release notes

Write notes for a reader who has not followed the PRs. `v0.1.0` used these sections; keep them
and add the change list for later releases:

- one-paragraph summary of what knowledge-memory is;
- **Changes** since the previous release, linking the PRs or Issues (for later releases);
- **Upgrade notes**: incompatible changes, index rebuilds or configuration changes, if any;
- **Highlights** of what the release can do;
- **Current scope**: single user and small vaults, literal search by default, synthetic-only
  tests and evaluations, `scope` is not access control, zero hits do not prove absence;
- **Verified environments**, and what was not verified;
- **Getting started**: point to the README Quick start and the demo;
- **Security**: report vulnerabilities through private vulnerability reporting (SECURITY.md);
- **License**: Apache-2.0.

Do not claim quality on real data, platforms that CI does not run, or guarantees the README does
not make.

## 6. GitHub Release

Create the release as a draft from the pushed tag, read it on GitHub, then publish:

```bash
gh release create v0.2.0 --verify-tag --draft \
  --title "knowledge-memory v0.2.0" --notes-file release-notes.md
gh release view v0.2.0 --web        # check rendering and links
gh release edit v0.2.0 --draft=false
```

`v0.1.0` has no attached files; users install from the tagged source. Attaching built
distributions would be a separate decision.

After publishing:

- the release page shows the intended tag, and the tag points to the commit CI checked;
- a fresh clone of the tag passes the Quick start;
- the version on `main` is not bumped again until the next release PR.

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

Fix the problem through a normal PR and restart from section 2.

## Rollback and recovery

Tags and releases are copied as soon as someone fetches them, so fix forward:

- **Before pushing a tag**: delete the local tag (`git tag -d vX.Y.Z`) and start again.
- **Draft release**: edit or delete the draft freely.
- **Published release with a defect**: do not move or reuse the tag. Fix through a PR, release a
  new PATCH version, and edit the old release notes to point to it. Mark the old release as a
  pre-release or add a warning at the top if users should avoid it.
- **Wrong release notes**: edit the GitHub Release; the tag stays.
- **Secret or private data in a release**: revoke the credential first, then follow
  [SECURITY.md](../SECURITY.md#if-a-secret-was-exposed). Deleting the release or tag does not
  undo the exposure. If a tag must still be deleted, say so in a public Issue so users know why
  it disappeared.
- **A change on `main` that breaks things**: revert it with a normal PR (`git revert -m 1 <merge>`)
  rather than rewriting history.
