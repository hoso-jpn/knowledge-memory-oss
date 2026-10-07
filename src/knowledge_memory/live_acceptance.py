"""Record selected-source acceptance in a new private workspace, without remote writes."""
from __future__ import annotations

import argparse
import asyncio
import copy
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import platform
import sys

import yaml

from .core import ID, MemoryError, build_index, get_note, read_notes, snapshot
from .evaluation import load_questions, run_evaluation
from .operations import probe_mcp
from .sync import digest, encoded, load_config, request, sync


def run(config_path, questions_path, workspace, include_candidates=False, with_mcp=False, fetch=request):
    if not include_candidates:
        raise MemoryError('explicit candidate evaluation is required for fresh imported sources')
    config = load_config(config_path)
    questions = load_questions(questions_path)
    expected = set()
    for item in questions:
        ids = item.get('expect_ids', [])
        if (not isinstance(item['query'], str) or not item['query'].strip() or len(item['query']) > 512
                or not isinstance(ids, list) or any(not isinstance(i, str) or not ID.fullmatch(i) for i in ids)
                or len(ids) != len(set(ids))):
            raise MemoryError('questions require valid query text and distinct expected IDs')
        expected.update(ids)
    if not expected:
        raise MemoryError('include at least one question with expected source IDs')
    if workspace.exists() or workspace.is_symlink():
        raise MemoryError('acceptance workspace must not exist')
    workspace = workspace.resolve()
    if any((parent / '.git').exists() for parent in (workspace, *workspace.parents)):
        raise MemoryError('acceptance workspace must be outside a Git repository')
    workspace.mkdir(parents=True, mode=0o700)
    vault, state, database = workspace / 'vault', workspace / 'sync-state', workspace / 'index.sqlite3'
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'automated_status': 'failed',
              'stage': 'prepare', 'scope': config['scope'], 'include_candidates': True,
              'python': platform.python_version(), 'package': importlib.metadata.version('knowledge-memory'),
              'checks': {}, 'sync_runs': {}, 'mcp': {'status': 'not_run', 'llm_initiated': False},
              'manual_checks': {key: 'not_run' for key in (
                  'original_content_tables_and_sources', 'natural_source_update',
                  'real_client_instructions_loaded', 'llm_search_get_grounded_answer',
                  'long_context_get_fallback', 'unavailable_source_and_unknown_answer')}}
    try:
        # Freeze the resolved mapping and questions so edits to operator files cannot widen
        # the selection between dry-run, initial sync and repeated sync.
        inputs = workspace / 'inputs'
        inputs.mkdir()
        frozen = copy.deepcopy(config)
        for number, source in enumerate(frozen['sources']):
            if 'import_config' in source:
                name = f'import-{number}.yaml'
                (inputs / name).write_text(yaml.safe_dump(source['import_config'], allow_unicode=True))
                source['import_config'] = name
        fixed_config, fixed_questions = inputs / 'sync.yaml', inputs / 'questions.yaml'
        fixed_config.write_text(yaml.safe_dump(frozen, allow_unicode=True))
        fixed_questions.write_text(yaml.safe_dump({'questions': questions}, allow_unicode=True))
        report.update(config_sha256=digest(encoded(config).encode()),
                      questions_sha256=digest(fixed_questions.read_bytes()))
        initial_notes = None
        for stage in ('dry_run', 'initial_sync', 'repeat_sync'):
            report['stage'] = stage
            result = sync(fixed_config, vault, state, dry_run=stage == 'dry_run', fetch=fetch)
            report['sync_runs'][stage] = result
            good = (bool(result['sources'])
                    and all(e['status'] == 'complete' for e in result['enumerations'])
                    and all(s['status'] in {'created', 'revised', 'unchanged'} for s in result['sources'])
                    and (stage == 'dry_run' or result['complete']))
            if not good:
                raise MemoryError('sync did not fully succeed with nonempty selected sources')
            if stage == 'dry_run':
                report['checks']['dry_run_no_source_writes'] = not vault.exists() and not state.exists()
            elif stage == 'initial_sync':
                initial_notes = snapshot(vault)
            else:
                report['checks']['repeat_unchanged'] = (
                    all(s['status'] == 'unchanged' for s in result['sources'])
                    and initial_notes == snapshot(vault))
            if not all(report['checks'].values()):
                raise MemoryError('sources changed during the acceptance run; inspect and retry')
        report['stage'] = 'index_and_retrieval'
        count = build_index(vault, database)
        notes = read_notes(database, config['scope'], include_candidates=True)
        report['records'] = count
        report['checks']['candidate_source_records'] = count > 0 and count == len(notes) and all(
            n['metadata']['status'] == 'candidate' and n['metadata']['claim_kind'] == 'source_report' for n in notes)
        report['checks']['expected_sources_present'] = expected <= {n['id'] for n in notes}
        report['checks']['default_excludes_candidates'] = not read_notes(database, config['scope'])
        provenance = []
        for note in notes:
            raw = get_note(database, note['id'], config['scope'], include_candidates=True)
            if raw['sha256'] != note['sha256']:
                raise MemoryError('search and original source differ')
            meta, origin = raw['metadata'], raw['metadata'].get('origin', {})
            provenance.append({'id': note['id'], 'path': note['path'], 'sha256': note['sha256'],
                'status': meta['status'], 'claim_kind': meta['claim_kind'],
                'source_edited_at': origin.get('source_edited_at'),
                'document_date': origin.get('source_metadata', {}).get('document_date'),
                'origin': {key: origin.get(key) for key in ('record_id', 'source_url', 'retrieved_at',
                    'content_sha256', 'semantic_sha256', 'data_source_id', 'database_id')}})
        report['provenance'] = provenance
        report['checks']['original_get_verified'] = bool(provenance)
        report['evaluation'] = run_evaluation(database, fixed_questions, config['scope'], include_candidates=True)
        report['checks']['fixed_questions_passed'] = not (report['evaluation']['miss'] or report['evaluation']['false_hit'])
        if not all(report['checks'].values()):
            raise MemoryError('retrieval acceptance failed; inspect the recorded checks')
        if with_mcp:
            report['stage'] = 'sdk_probe'
            query = next(q['query'] for q in questions if q.get('expect_ids'))
            report['mcp'] = asyncio.run(asyncio.wait_for(
                probe_mcp(vault, database, config['scope'], query, include_candidates=True), 30))
            report['checks']['sdk_retrieval_verified'] = report['mcp']['retrieval_verified']
        report['automated_status'] = 'passed' if all(report['checks'].values()) else 'failed'
        report['stage'] = 'finished'
    except Exception:
        # Do not echo response bodies, credentials or arbitrary transport exceptions.
        report['error'] = 'acceptance stage failed; inspect the private report and sync evidence'
    finally:
        (workspace / 'report.json').write_text(encoded(report))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--questions', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--include-candidates', action='store_true', required=True)
    parser.add_argument('--with-mcp', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = run(args.config, args.questions, args.workspace, args.include_candidates, args.with_mcp)
        print(json.dumps({'automated_status': result['automated_status'], 'stage': result['stage'],
                          'manual_checks': 'not_run', 'report': str(args.workspace.resolve() / 'report.json')}))
        return 0 if result['automated_status'] == 'passed' else 2
    except (MemoryError, OSError, ValueError, TypeError):
        print('{"error":"acceptance refused; check inputs and select a new workspace outside Git"}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
