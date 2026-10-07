"""Normalize only documented per-read fields: response envelopes and hosted file objects."""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Do not strip arbitrary query parameters from ordinary links or from file object URLs.
SIGNING_KEYS = {'x-amz-algorithm', 'x-amz-credential', 'x-amz-date', 'x-amz-expires',
                'x-amz-signedheaders', 'x-amz-signature', 'x-amz-security-token',
                'x-amz-content-sha256', 'awsaccesskeyid', 'signature', 'expires'}


def stable_record(value):
    if isinstance(value, list):
        return [stable_record(v) for v in value]
    if not isinstance(value, dict):
        return value
    result = {k: stable_record(v) for k, v in value.items()}
    # Notion stamps every response envelope with a request_id for support correlation, so it
    # changes on each read of an unchanged page. Only an object that carries the envelope's
    # own "object" key is treated as one; a page property a person happens to have named
    # request_id lives under properties, which has no such key, and is left alone.
    if isinstance(value.get('object'), str) and isinstance(value.get('request_id'), str):
        result.pop('request_id')
    # A block with type=file wraps this file object one level deeper; it is not itself
    # a file object unless its file payload has a url. No generic key-name deletion.
    if value.get('type') == 'file' and isinstance(value.get('file'), dict) \
            and isinstance(value['file'].get('url'), str):
        file = result['file']
        parts = urlsplit(file['url'])
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if k.lower() not in SIGNING_KEYS]
        file['url'] = urlunsplit((parts.scheme, parts.netloc, parts.path,
                                 urlencode(query), parts.fragment))
        file.pop('expiry_time', None)
    return result
