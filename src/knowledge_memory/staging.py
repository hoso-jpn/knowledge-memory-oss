"""Shared note writing for importers.

Two separate things are tracked per note. `content_sha256` identifies the source record.
`render_sha256` identifies the file we produced from it, so a change of import settings or
of the conversion itself is a change even when the source record is untouched, and a note
edited by hand afterwards is recognised instead of being overwritten in silence.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

import yaml

from .core import MemoryError, parse_note

PLACEHOLDER = "__render__"
# Values that describe the read rather than the record: they move when an unrelated row
# changes or when the same export is read again, so they are left out of the digest.
# Matched at the exact depth the frontmatter is written at, and only inside the
# frontmatter: a body line that happens to begin with one of these words is content,
# and blanking it would hide an edit to the note.
VOLATILE = re.compile(
    r"^(updated|  render_sha256|  retrieved_at|  source_file_sha256): .*$", re.M)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _split(text: str) -> tuple[str, str]:
    """Separate the frontmatter from the body. Only the frontmatter is ever blanked."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return "", text
    return "".join(lines[:end]), "".join(lines[end:])


def render_digest(text: str) -> str:
    """Hash a note with the per-fetch values blanked, so re-reading the same record matches."""
    front, rest = _split(text)
    if re.search(r"^  semantic_sha256: [0-9a-f]{64}$", front, re.M):
        front = re.sub(r"^  content_sha256: [0-9a-f]{64}$", "  content_sha256: -", front, flags=re.M)
    return digest((VOLATILE.sub(r"\1: -", front) + rest).encode())



def note_text(meta: dict, body: str) -> str:
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, width=10**6)
    return "---\n" + front + "---\n" + body


def finalize(meta: dict, body: str) -> str:
    """Serialise a note and stamp it with the digest of its own rendered form."""
    meta["origin"]["render_sha256"] = PLACEHOLDER
    draft = note_text(meta, body)
    stamped = draft.replace(f"render_sha256: {PLACEHOLDER}",
                            f"render_sha256: {render_digest(draft)}", 1)
    if PLACEHOLDER in stamped:
        raise MemoryError("could not stamp the rendered digest")
    return stamped


def plan(target: Path, built: dict[str, str], scope: str) -> dict:
    """Sort notes into what to write and what to leave alone, without touching anything."""
    groups = {name: [] for name in ("created", "revised", "unchanged",
                                    "locally_modified", "promoted")}
    conflicting = []
    for identifier, text in built.items():
        path = target / f"{identifier}.md"
        if not path.exists():
            groups["created"].append(identifier)
            continue
        current = path.read_text(encoding="utf-8")
        note = parse_note(str(path), current.encode())
        if note.metadata.get("scope") != scope:
            conflicting.append(f"{identifier} is in scope {note.metadata.get('scope')!r}")
            continue
        stored = note.metadata.get("origin", {}).get("render_sha256")
        # A note written before the digest was scoped to the frontmatter agrees with this
        # one unless its body holds a line shaped like one of the blanked keys, which is
        # exactly the case the old stamp could not see. Those are reported rather than
        # rewritten, so an edit made there is never lost to a stamp that never covered it.
        if stored is None or stored != render_digest(current):
            groups["locally_modified"].append(identifier)
        elif stored == render_digest(text):
            groups["unchanged"].append(identifier)
        elif note.metadata.get("status") != "candidate":
            # Someone reviewed and promoted it; a re-import must not undo that.
            groups["promoted"].append(identifier)
        else:
            groups["revised"].append(identifier)
    if conflicting:
        raise MemoryError("these notes already exist under a different scope, so importing "
                          f"would change it: {sorted(conflicting)}")
    return {name: sorted(values) for name, values in groups.items()}


def write_all(items: list[tuple[Path, str]]):
    """Write every file or leave the tree as it was.

    Each file is staged beside its destination and existing files are copied aside first.
    If any write or replace raises, replacements already made are undone and files newly
    created by this call are removed. This covers runtime failures, not a process killed
    mid-loop: SIGKILL or a power cut between two renames can still leave a mixture, which
    a re-run then corrects.
    """
    staged, replaced = [], []
    try:
        for path, text in items:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, name = tempfile.mkstemp(prefix=".km-stage-", dir=path.parent)
            entry = [path, Path(name), None]
            staged.append(entry)      # registered first, so a failure below still cleans up
            with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
                stream.write(text)
            if path.exists():
                handle, backup_name = tempfile.mkstemp(prefix=".km-backup-", dir=path.parent)
                os.close(handle)
                entry[2] = Path(backup_name)
                shutil.copy2(path, backup_name)
        try:
            for path, temporary, backup in staged:
                os.replace(temporary, path)
                replaced.append((path, backup))
        except BaseException:
            for path, backup in reversed(replaced):
                if backup is None:
                    path.unlink(missing_ok=True)
                else:
                    os.replace(backup, path)
            raise
    finally:
        for path, temporary, backup in staged:
            temporary.unlink(missing_ok=True)
            if backup is not None:
                backup.unlink(missing_ok=True)
