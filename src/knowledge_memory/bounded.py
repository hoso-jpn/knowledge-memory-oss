"""Bounded MCP payloads with lossless, version-bound character ranges."""
from __future__ import annotations

import json
import re

from .core import MemoryError


def response_text(result):
    return json.dumps(result, ensure_ascii=False)


def response_size(result):
    # Includes the JSON text escaped inside the MCP content envelope. JSON-RPC transport
    # ids/framing are client-specific and excluded; reserve that margin in the client.
    wire = json.dumps({'content': [{'type': 'text', 'text': response_text(result)}],
                       'isError': False}, ensure_ascii=False, separators=(',', ':'))
    return len(wire), len(wire.encode('utf-8'))


class ResponseLimit:
    def __init__(self, max_chars=12000, max_bytes=24000):
        if any(type(v) is not int or not 512 <= v <= 1_000_000 for v in (max_chars, max_bytes)):
            raise MemoryError('response limits must be integers from 512 to 1000000')
        self.max_chars, self.max_bytes = max_chars, max_bytes

    def fits(self, value):
        chars, size = response_size(value)
        return chars <= self.max_chars and size <= self.max_bytes

    def check(self, value):
        if not self.fits(value):
            raise MemoryError('response header exceeds server limit; request fewer records or adjust operator limits')
        return value


def reference(note):
    """Keep citation and state facts; omit arbitrary imported metadata from the header."""
    meta = note['metadata']
    selected = {k: meta[k] for k in ('title', 'kind', 'status', 'claim_kind', 'updated',
                                    'sources', 'event_at', 'verified_at') if k in meta}
    origin = meta.get('origin', {})
    selected['origin'] = {k: origin[k] for k in ('kind', 'record_id', 'source_url',
                                                'retrieved_at', 'source_edited_at') if k in origin}
    document_date = origin.get('source_metadata', {}).get('document_date')
    if document_date is not None:
        selected['origin']['document_date'] = document_date
    return {'id': note['id'], 'sha256': note['sha256'], 'metadata': selected}


def search_response(notes, limit):
    result = {'records': [], 'omitted_results': len(notes), 'truncated': bool(notes),
              'reason': 'response_limit' if notes else None,
              'selection': 'top matches within requested result count; not a complete inventory'}
    limit.check(result)
    for note in notes:
        item = dict(reference(note), excerpt=note['excerpt'], excerpt_normalized=True)
        candidate = dict(result, records=result['records'] + [item],
                         omitted_results=result['omitted_results'] - 1)
        candidate.update(truncated=bool(candidate['omitted_results']),
                         reason='response_limit' if candidate['omitted_results'] else None)
        if not limit.fits(candidate):
            if not result['records']:
                raise MemoryError('first search result header exceeds server limit; narrow query or adjust operator limits')
            break
        result = candidate
    return limit.check(result)


def chunk_response(note, cursor, limit):
    start = 0
    if cursor is not None:
        match = re.fullmatch(r'([0-9a-f]{64})\.([0-9]{1,10})', cursor)
        if not match:
            raise MemoryError('invalid continuation cursor')
        if match[1] != note['sha256']:
            raise MemoryError('note changed; search again and restart retrieval')
        start = int(match[2])
    content = note['content']
    if start > len(content):
        raise MemoryError('cursor exceeds content range')

    def result(end):
        return dict(reference(note), content=content[start:end],
                    locator={'unit': 'unicode_codepoint', 'start': start, 'end': end,
                             'total': len(content), 'range': 'half-open raw Markdown'},
                    next_cursor=f"{note['sha256']}.{end}" if end < len(content) else None,
                    truncated=end < len(content), reason='response_limit' if end < len(content) else None)

    # EOF removes cursor/reason overhead, so test it before the monotonic search
    # over non-final prefixes. A fitting tail must never be stranded by a cursor.
    if len(content) - start <= limit.max_chars and limit.fits(result(len(content))):
        return limit.check(result(len(content)))
    low, high = start, min(len(content) - 1, start + limit.max_chars)
    limit.check(result(start))
    while low < high:
        mid = (low + high + 1) // 2
        if limit.fits(result(mid)):
            low = mid
        else:
            high = mid - 1
    if low == start and start < len(content):
        raise MemoryError('response header leaves no room for content; adjust operator limits')
    return limit.check(result(low))
