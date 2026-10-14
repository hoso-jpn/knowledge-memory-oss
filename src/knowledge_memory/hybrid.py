"""Opt-in CPU retrieval experiments. Literal hits retain priority; vectors are disposable."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
try:
    import resource
except ImportError:  # Windows has no resource module.
    resource = None
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from .core import MemoryError, index_meta, normalize, read_notes, search_notes
from .evaluation import load_questions

VERSION = 1
CHUNK_CHARS, OVERLAP = 256, 32


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def peak_rss_kib(resource_module=resource):
    """Best-effort peak RSS; unavailable on platforms without the resource module."""
    if resource_module is None:
        return None
    return resource_module.getrusage(resource_module.RUSAGE_SELF).ru_maxrss


def signature(notes):
    return hashlib.sha256(canonical([(n['id'], n['sha256']) for n in notes]).encode()).hexdigest()


def unit(vector):
    if not isinstance(vector, (list, tuple)) or not vector or any(not math.isfinite(float(x)) for x in vector):
        raise MemoryError('invalid embedding')
    norm = math.sqrt(sum(float(x) ** 2 for x in vector))
    return [float(x) / norm if norm else 0.0 for x in vector]


class CharacterEncoder:
    """Dependency-free lexical control. This is not a semantic pretrained model."""
    identity = {'backend': 'character-ngram-v1', 'dimensions': 512, 'semantic': False}
    def encode(self, texts, query=False):
        vectors = []
        for text in texts:
            text = normalize(text)
            vector = [0.0] * 512
            for length in (2, 3):
                for index in range(len(text) - length + 1):
                    token = text[index:index + length].encode()
                    bucket = int.from_bytes(hashlib.sha256(token).digest()[:4], 'big') % 512
                    vector[bucket] += 1
            vectors.append(unit(vector))
        return vectors


class SentenceEncoder:
    def __init__(self, directory: Path):
        directory = directory.resolve(strict=True)
        if not directory.is_dir():
            raise MemoryError('model must be an existing local directory')
        files = []
        for path in sorted(directory.rglob('*')):
            if path.is_file():
                # Hugging Face snapshot symlinks may point to its local blob cache.
                hasher = hashlib.sha256()
                with path.open('rb') as stream:
                    for block in iter(lambda: stream.read(1048576), b''):
                        hasher.update(block)
                files.append((str(path.relative_to(directory)), hasher.hexdigest()))
        if not files:
            raise MemoryError('empty local model directory')
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['TRANSFORMERS_OFFLINE'] = '1'
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(str(directory), device='cpu', local_files_only=True,
                                         trust_remote_code=False)
        self.identity = {'backend': 'sentence-transformers', 'semantic': True,
                         'package': importlib.metadata.version('sentence-transformers'),
                         'weights_sha256': hashlib.sha256(canonical(files).encode()).hexdigest(),
                         'files': len(files), 'device': 'cpu', 'remote_code': False}

    def encode(self, texts, query=False):
        for text in texts:
            if len(self.model.tokenizer.encode(text)) > self.model.max_seq_length:
                raise MemoryError('chunk/query exceeds model token budget; reduce chunk size')
        method = self.model.encode_query if query else self.model.encode_document
        return method(texts, normalize_embeddings=True, show_progress_bar=False).tolist()


def chunks(note):
    text = note['metadata']['title'] + '\n' + note['body']
    for start in range(0, len(text), CHUNK_CHARS - OVERLAP):
        yield start, text[start:start + CHUNK_CHARS]


def build_vectors(database: Path, vectors: Path, scope: str, encoder):
    meta = index_meta(database)
    if (vectors.is_symlink() or vectors.resolve() == database.resolve()
            or (vectors.exists() and os.path.samefile(vectors, database))
            or vectors.resolve().is_relative_to(Path(meta['vault'])) or vectors.suffix != '.sqlite3'):
        raise MemoryError('vectors must be a separate .sqlite3 file outside vault')
    started = time.perf_counter()
    notes = read_notes(database, scope, True, True)
    entries = [(n['id'], start, text) for n in notes for start, text in chunks(n)]
    values = encoder.encode([e[2] for e in entries]) if entries else []
    if len(values) != len(entries):
        raise MemoryError('embedding count mismatch')
    values = [unit(v) for v in values]
    if len({len(v) for v in values}) > 1:
        raise MemoryError('embedding dimension mismatch')
    header = {'version': VERSION, 'source_signature': signature(notes), 'scope': scope,
              'encoder': encoder.identity, 'chunk_chars': CHUNK_CHARS, 'overlap': OVERLAP,
              'dimensions': len(values[0]) if values else 0}
    vectors.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=vectors.parent, suffix='.sqlite3')
    os.close(handle)
    try:
        with sqlite3.connect(name) as con:
            con.execute('CREATE TABLE metadata (json TEXT)')
            con.execute('INSERT INTO metadata VALUES (?)', (canonical(header),))
            con.execute('CREATE TABLE vectors (id TEXT, start INTEGER, excerpt TEXT, vector TEXT)')
            con.executemany('INSERT INTO vectors VALUES (?,?,?,?)',
                            [(identifier, start, text, canonical(vector))
                             for (identifier, start, text), vector in zip(entries, values)])
        os.replace(name, vectors)
    finally:
        Path(name).unlink(missing_ok=True)
    return {**header, 'records': len(notes), 'chunks': len(entries),
            'seconds': time.perf_counter() - started, 'peak_rss_kib': peak_rss_kib(),
            'gpu_required': False}


def hybrid_search(database: Path, vectors: Path, scope: str, query: str, encoder,
                  limit=10, threshold=0.2, include_candidates=False, include_history=False):
    # Core validates query/limit and enforces its scope/state/source policy first.
    literal = search_notes(database, query, scope, limit, include_candidates, include_history)
    if not isinstance(threshold, (float, int)) or not math.isfinite(threshold) or not -1 <= threshold <= 1:
        raise MemoryError('invalid similarity threshold')
    all_notes = read_notes(database, scope, True, True)
    eligible = {n['id']: n for n in read_notes(database, scope, include_candidates, include_history)}
    with sqlite3.connect(vectors.resolve().as_uri() + '?mode=ro', uri=True) as con:
        header = json.loads(con.execute('SELECT json FROM metadata').fetchone()[0])
        expected = {'version': VERSION, 'source_signature': signature(all_notes), 'scope': scope,
                    'encoder': encoder.identity, 'chunk_chars': CHUNK_CHARS, 'overlap': OVERLAP}
        if any(header.get(key) != value for key, value in expected.items()):
            raise MemoryError('vectors changed/stale or model mismatch; rebuild vectors')
        rows = con.execute('SELECT id,start,excerpt,vector FROM vectors').fetchall()
    # Exact ID lookup adds functionality without displacing literal matches unnecessarily.
    output = []
    if query in eligible:
        n = eligible[query]
        output.append({k: n[k] for k in ('id', 'path', 'sha256', 'metadata')} | {'method': 'exact-id'})
    output.extend(dict(item, method='literal') for item in literal if item['id'] != query)
    present = {item['id'] for item in output}
    query_vector = unit(encoder.encode([query], query=True)[0])
    if header['dimensions'] and len(query_vector) != header['dimensions']:
        raise MemoryError('query embedding dimension mismatch')
    ranked = {}
    for identifier, start, excerpt, raw in rows:
        if identifier not in eligible or identifier in present:
            continue
        vector = unit(json.loads(raw))
        if len(vector) != len(query_vector):
            raise MemoryError('corrupt vector dimension')
        score = sum(a * b for a, b in zip(vector, query_vector))
        if score >= threshold and (identifier not in ranked or score > ranked[identifier][0]):
            ranked[identifier] = (score, start, excerpt)
    for identifier, (score, start, excerpt) in sorted(ranked.items(), key=lambda item: (-item[1][0], item[0])):
        note = eligible[identifier]
        output.append({k: note[k] for k in ('id', 'path', 'sha256', 'metadata')} |
                      {'method': 'semantic' if encoder.identity.get('semantic') else 'character',
                       'score': score, 'score_is_truth': False, 'excerpt': excerpt,
                       'excerpt_normalized': False, 'chunk_start': start})
    return output[:limit]


def compare(database, vectors, scope, questions, encoder, include_candidates=False, limit=10, threshold=0.2):
    rows = []
    for question in questions:
        expected = set(question.get('expect_ids', []))
        unknown = question.get('expect', 'ids' if expected else 'unknown') == 'unknown'
        minimum = question.get('min_hits', 1)
        if type(minimum) is not int or minimum < 1 or (not unknown and minimum > len(expected)):
            raise MemoryError('invalid min_hits for comparison')
        results, timings = {}, {}
        for method in ('literal', 'hybrid'):
            started = time.perf_counter()
            found = (search_notes(database, question['query'], scope, limit, include_candidates)
                     if method == 'literal' else hybrid_search(database, vectors, scope, question['query'],
                        encoder, limit, threshold, include_candidates))
            timings[method] = time.perf_counter() - started
            ids = [r['id'] for r in found]
            matched = len(expected.intersection(ids))
            results[method] = {'ids': ids, 'hit': not ids if unknown else matched >= minimum,
                               'matched_expected': matched, 'min_hits': minimum,
                               'reciprocal_rank': next((1 / (i + 1) for i, v in enumerate(ids) if v in expected), 0),
                               'false_hit': bool(ids) if unknown else False}
        rows.append({'id': question['id'], 'kind': question.get('kind'), 'results': results, 'seconds': timings})
    return {'encoder': encoder.identity, 'python': platform.python_version(), 'platform': platform.platform(),
            'threshold': threshold, 'limit': limit, 'questions': len(rows),
            'success': {m: sum(r['results'][m]['hit'] for r in rows) for m in ('literal', 'hybrid')},
            'improvements': [r['id'] for r in rows if not r['results']['literal']['hit'] and r['results']['hybrid']['hit']],
            'regressions': [r['id'] for r in rows if r['results']['literal']['hit'] and not r['results']['hybrid']['hit']],
            'peak_rss_kib': peak_rss_kib(), 'gpu_required': False,
            'rows': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['index', 'search', 'compare'])
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--vectors', type=Path, required=True)
    parser.add_argument('--scope', required=True)
    parser.add_argument('--local-model', type=Path)
    parser.add_argument('--query')
    parser.add_argument('--questions', type=Path)
    parser.add_argument('--include-candidates', action='store_true')
    parser.add_argument('--threshold', type=float, default=0.2)
    args = parser.parse_args()
    try:
        encoder = SentenceEncoder(args.local_model) if args.local_model else CharacterEncoder()
        if args.action == 'index':
            result = build_vectors(args.db, args.vectors, args.scope, encoder)
        elif args.action == 'search':
            result = hybrid_search(args.db, args.vectors, args.scope, args.query, encoder,
                                   threshold=args.threshold, include_candidates=args.include_candidates)
        else:
            if not args.questions:
                raise MemoryError('questions file required')
            result = compare(args.db, args.vectors, args.scope, load_questions(args.questions, allow_legacy_list=True),
                             encoder, args.include_candidates, threshold=args.threshold)
        print(canonical(result))
        return 0
    except (MemoryError, OSError, ValueError, sqlite3.Error, ImportError):
        print('{"error":"hybrid operation refused; check model, source index and vectors"}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
