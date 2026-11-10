"""Explicit offline migration and maintenance of canonical artifact generations.

Use `python -m utils.architecture_maintenance --help` from the repository root.
All actions are local; adoption, tombstones, rollback, and cleanup require --apply.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from ribosome.core.artifacts import ArtifactRepository, CorpusManifest
from ribosome.core.document import Document, adapt_semantics, source_revision


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('status', 'recover'): commands.add_parser(command)
    adopt = commands.add_parser('adopt')
    adopt.add_argument('semantics', type=Path)
    adopt.add_argument('--source', type=Path, required=True)
    adopt.add_argument('--corpus', default='default')
    adopt.add_argument('--document-id')
    adopt.add_argument('--apply', action='store_true')
    sync = commands.add_parser('sync')
    sync.add_argument('manifest', type=Path)
    sync.add_argument('--apply', action='store_true')
    cleanup = commands.add_parser('cleanup')
    cleanup.add_argument('--grace-seconds', type=float, default=7 * 86400)
    cleanup.add_argument('--keep-previous', type=int, default=1)
    cleanup.add_argument('--apply', action='store_true')
    rollback = commands.add_parser('rollback')
    rollback.add_argument('document_id')
    rollback.add_argument('generation')
    rollback.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    # Dry adoption validates inputs without creating a repository or allocating identity.
    if args.command == 'adopt' and not args.apply:
        document = Document(args.document_id or 'unassigned-preview', args.source.stem, args.corpus)
        revision = source_revision(document.id, str(args.source.resolve()), args.source.read_bytes())
        graph = adapt_semantics(json.loads(args.semantics.read_text()), document, revision)
        result = {'applied': False, 'valid': True, 'nodes': len(graph.nodes), 'summaries': len(graph.summaries),
                  'source_unchanged': True, 'coverage': 'coverage_unknown'}
    else:
        if args.command != 'adopt' and not (args.repository / 'catalog.sqlite3').is_file():
            raise FileNotFoundError('Repository does not exist; use explicit adoption first')
        repo = ArtifactRepository(args.repository)
        if args.command == 'status': result = repo.stats()
        elif args.command == 'recover': result = repo.recover()
        elif args.command == 'cleanup':
            result = repo.cleanup(dry_run=not args.apply, grace_seconds=args.grace_seconds, keep_previous=args.keep_previous)
        elif args.command == 'sync':
            result = repo.synchronize_manifest(CorpusManifest(**json.loads(args.manifest.read_text())), apply=args.apply)
        elif args.command == 'rollback':
            # Validate the target even for dry runs.
            repo.read(args.document_id, args.generation)
            if args.apply: repo.rollback(args.document_id, args.generation)
            result = {'applied': args.apply, 'document_id': args.document_id, 'generation': args.generation}
        elif args.command == 'adopt':
            document = repo.adopt_document(str(args.source.resolve()), document_id=args.document_id,
                                           title=args.source.stem, corpus_id=args.corpus)
            revision = source_revision(document.id, str(args.source.resolve()), args.source.read_bytes())
            graph = adapt_semantics(json.loads(args.semantics.read_text()), document, revision)
            publication = repo.prepare(graph, completeness={'coverage': 'coverage_unknown', 'migration': 'lossless-adoption'})
            result = {'applied': True, 'publication': asdict(repo.commit(publication.generation)), 'source_unchanged': True}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
