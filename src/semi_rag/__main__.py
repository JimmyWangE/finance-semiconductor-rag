from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from semi_rag.config import load_config


def main():
    parser = argparse.ArgumentParser(description='Semiconductor report RAG')
    parser.add_argument('--config', type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('build')
    sub.add_parser('stats')
    for name in ('search', 'ask'):
        query = sub.add_parser(name)
        query.add_argument('query')
        query.add_argument('--org')
        query.add_argument('--date-from')
        query.add_argument('--date-to')
        query.add_argument('--as-of')
        query.add_argument('--doc-id', action='append', dest='doc_ids')
        query.add_argument('--kind', choices=['text', 'table', 'chart'])
        query.add_argument('--top-k', type=int, default=6)
        if name == 'ask':
            query.add_argument('--answer-policy', choices=['strict', 'best_effort'])
        if name == 'search':
            query.add_argument('--mode', choices=['bm25', 'dense', 'hybrid', 'rerank'], default='rerank')
    server = sub.add_parser('serve')
    server.add_argument('--host', default='127.0.0.1')
    server.add_argument('--port', type=int, default=8090)
    server.add_argument('--trusted-network', action='store_true',
                        help='allow an unauthenticated bind outside localhost for an approved private network')
    evaluate = sub.add_parser('evaluate')
    evaluate.add_argument('--dataset', type=Path, required=True)
    evaluate.add_argument('--output', type=Path, required=True)
    evaluate.add_argument('--limit', type=int, default=0)
    prepare = sub.add_parser('prepare-eval')
    prepare.add_argument('--output', type=Path, default=Path('data/rag/eval/questions.jsonl'))
    prepare.add_argument('--limit', type=int, default=60)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = load_config(args.config)
    if args.command == 'build':
        from semi_rag.index import build_index
        result = build_index(config)
    elif args.command == 'stats':
        result = json.loads((Path(config['index_dir']) / 'current/manifest.json').read_text())
    elif args.command == 'serve':
        import uvicorn
        from semi_rag.api import create_app
        if args.host not in ('127.0.0.1', 'localhost', '::1') and not (
            os.environ.get('RAG_API_KEY') or args.trusted_network
        ):
            parser.error('Set RAG_API_KEY or use --trusted-network before binding beyond localhost')
        uvicorn.run(create_app(config), host=args.host, port=args.port, workers=1)
        return
    elif args.command == 'prepare-eval':
        from semi_rag.evaluate import prepare_dataset
        result = prepare_dataset(config, args.output, args.limit)
    elif args.command == 'evaluate':
        from semi_rag.evaluate import evaluate
        result = evaluate(config, args.dataset, args.output, args.limit)
    else:
        from semi_rag.retrieve import Retriever
        retriever = Retriever(config)
        filters = {key: getattr(args, key) for key in ('org', 'date_from', 'date_to', 'as_of', 'doc_ids', 'kind')
                   if getattr(args, key) is not None}
        if args.command == 'search':
            result = retriever.search(args.query, filters, args.top_k, args.mode)
        else:
            from semi_rag.answer import Answerer
            answerer = Answerer(config, retriever)
            try:
                result = answerer.ask(args.query, filters, args.top_k, args.answer_policy)
            finally:
                answerer.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
