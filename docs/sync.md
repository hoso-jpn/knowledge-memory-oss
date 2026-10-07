# Explicit source sync

`python -m knowledge_memory.sync --config /outside/allowlist.yaml --vault /outside/vault --state /outside/sync-state --dry-run`

Remove `--dry-run` to apply, and run `km index` after notes change. Install the package to
use `km-sync`. Tokens come only from GITHUB_TOKEN / NOTION_TOKEN; no token CLI argument.
The transport uses fixed API hosts, rejects redirects, bounds retries and never logs response
bodies. A long Retry-After stops with retry_later rather than retrying too early.

Exit code 0 means every selected source was read and the run decided what to write; 2 means it
did not. A dry run writes nothing, so it has no `complete` flag, but it still exits 2 when any
enumeration failed or any source came back inaccessible, archived, moved, needs_review or
configuration_conflict. `locally_modified` and `promoted` are decisions not to overwrite a note,
not read failures, so they exit 0; read the reported statuses rather than the code alone.

```yaml
scope: personal
sources:
  - provider: github
    repo: example/personal-notes
    path: README.md
    ref: main
  - provider: notion
    page_id: 00000000-0000-0000-0000-000000000001
    body_source: property
    body_property: Content
    title_property: Title
    page_body_slugs: []
```

Only named files/pages are read; no repository/database/account-wide discovery. Notion block
children and long properties are paginated within each selected page. The existing parser
rejects repeated cursors and incomplete pages. External links and linked pages are not fetched.

GitHub files use stable repo/path IDs. Notion uses existing src-pageid IDs. Raw responses,
version/content hashes, retrieval time and per-source events/checkpoints stay outside the
repository. GitHub blob SHA is the version; the Contents API does not supply an edit timestamp.
No invented edit time is stored. Notion last_edited_time remains separate from retrieval time.

## Failure and replay

Each source is marked pending before fetching. Complete source results are applied independently;
a failed source cannot mark the entire run current. `--resume` retries unfinished sources from
the same allowlist/config and resolved vault path, and retains completed checkpoints.
A checkpoint without a vault binding must also start a new normal sync. A completed run requires a new
normal sync. Resumed results may span times: their individual retrieval timestamps are the
truth, not an atomic remote snapshot. A subsequent normal sync rechecks all sources.

The vault's `.km-source-status.json` is an availability ledger, not a claim of factual truth.
The core reads it on every query/get/read_notes call; anything other than current is excluded,
even with history enabled. Thus prior indexes cannot leak stale imported sources after failure.
Original notes are retained, including promoted/manually edited records (needs_review). A new
candidate never silently overwrites an active record. Removing an allowlist entry blocks its
record as unselected. Do not delete this ledger to bypass the hold; preserve it in backups.
A malformed ledger stops retrieval. This is source-record exclusion, not automatic invalidation
of independently authored notes that merely cite a source; those need human review.

Notion archived/in_trash is deleted; 401/403 is inaccessible; 404 is missing_or_inaccessible
because neither API proves deletion from 404. Temporary failure is fetch_failed/retry_later.
A failed source remains blocked until a later successful sync or explicit human repair.

Single writer only. An exclusive vault lock prevents concurrent syncs. After SIGKILL, inspect
that no writer remains before removing the lock and resuming. Existing staging rollback covers
runtime exceptions, not power loss. Evidence files are retained, never pruned automatically.

## Publication body rule

Some Notion databases keep an article body in a property for most pages and in the page body
for a few. `page_body_slugs` lists the pages whose body comes from the page body; maintain it
explicitly in versioned operator config. If another system (for example a site publisher)
decides which pages use the page body, compare its list with this config before syncing and
whenever that list changes. Config changes invalidate resume. Reading another system's list
automatically is not implemented; this manual step is an operational limitation, not
automatic continuous synchronization.

### Continued maintenance, slug changes and unpublication

`page_body_slugs` chooses **where the body is read from**. It is not the source selection,
publication permission, or an access boundary. Removing a slug from this list selects the
configured property body; it does not retire the page. The Knowledge import's explicit
page-body mapping is separate from this rule.

1. Before a sync following a publisher/config change, the operator compares the publisher's
   pinned list revision with the private import settings. Record the two revisions and the
   reason for any difference. Do not derive permission from a title or URL.
2. If a slug changes, identify the same Notion page by its stable page ID, review the old/new
   body locations, then update `page_body_slugs` deliberately. Keep the ID and provenance;
   changed rendering may produce `revised`. Start a new sync after config changes, not resume
   with an older fingerprint. Review the resulting body diff before rebuilding the index.
3. A Published flag is source metadata, not automatic withdrawal from this selected-source
   sync. If an unpublished article must stop being served, remove its exact page selection,
   synchronize under the shared runtime lock, and verify the ledger and search/get/context
   exclusion. If it belongs to a whole-DS profile, it remains selected until the profile is
   deliberately split or retired; do not pretend that the slug/body rule removes it.
4. Empty source lists and removal of registered DS containers are refused. For the last page
   or a whole-DS migration, stop the old scheduled runs/readers/connections and isolate the old
   profile; use a new independent profile if needed, following the
   [profile boundary procedure](profile-boundaries.md).
   Keep old notes/evidence for the selected retention policy, but prevent normal access through
   old indexes, published generations and presets. Do not restore the mixed connection on failure.
5. Observe a legitimate original update when it occurs; do not edit/delete/unpublish actual
   articles merely to pass acceptance. Until then, record the policy as documented and the
   real slug/unpublication outcome as unobserved. Do not fetch a publisher repository or expand
   the selected corpus implicitly.

## Verification boundaries

Synthetic tests cover idempotence, exact allowlist, dry-run, partial failure/replay, local edits,
search/get/read_notes exclusion and token-safe errors. The repository tests do not call the real
Notion or GitHub APIs. Before relying on sync, run one selected file/page of your own through
dry-run → apply → unchanged → access-loss; keep raw content and credentials outside any public
issue or pull request. Sync never writes to Notion or GitHub.

API references: https://docs.github.com/en/rest/repos/contents and
https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api and
https://developers.notion.com/reference/retrieve-a-page
