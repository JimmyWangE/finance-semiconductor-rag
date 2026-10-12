"""Download pinned BGE checkpoints with resumable, SHA256-checked files."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

import httpx


def download(repo: str, root: Path, endpoint: str) -> None:
    dest = root / repo.split('/')[-1]
    dest.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        response = client.get(f'{endpoint}/api/models/{repo}', params={'blobs': 'true'})
        response.raise_for_status()
        info = response.json()
        revision = info['sha']
        wanted = {'config.json', 'tokenizer.json', 'tokenizer_config.json',
                  'special_tokens_map.json', 'sentencepiece.bpe.model'}
        names = {entry['rfilename'] for entry in info['siblings']}
        wanted.add('model.safetensors' if 'model.safetensors' in names else 'pytorch_model.bin')
        for entry in info['siblings']:
            name = entry['rfilename']
            if name not in wanted:
                continue
            target = dest / name
            expected = entry.get('lfs', {}).get('sha256')
            size = entry.get('size') or entry.get('lfs', {}).get('size')
            def valid(path: Path) -> bool:
                if not path.exists() or (size and path.stat().st_size != size):
                    return False
                if not expected:
                    return True
                with path.open('rb') as stream:
                    return hashlib.file_digest(stream, 'sha256').hexdigest() == expected
            if valid(target):
                continue
            partial = target.with_suffix(target.suffix + '.partial')
            for attempt in range(6):
                start = partial.stat().st_size if partial.exists() else 0
                if size and start >= size:
                    if valid(partial):
                        partial.replace(target)
                        break
                    partial.rename(partial.with_name(partial.name + f'.invalid-{time.time_ns()}'))
                    start = 0
                try:
                    url = f'{endpoint}/{repo}/resolve/{revision}/{name}'
                    with client.stream('GET', url, headers={'Range': f'bytes={start}-'} if start else {}) as r:
                        r.raise_for_status()
                        mode = 'ab' if start and r.status_code == 206 else 'wb'
                        with partial.open(mode) as stream:
                            for block in r.iter_bytes(4 * 1024 * 1024):
                                stream.write(block)
                    if not valid(partial):
                        raise RuntimeError(f'Integrity check failed: {name}')
                    partial.replace(target)
                    print(f'{repo}: {name} verified', flush=True)
                    break
                except (httpx.HTTPError, RuntimeError) as exc:
                    print(f'{repo}: retry {attempt + 1}: {type(exc).__name__}', flush=True)
                    if attempt == 5:
                        raise
                    time.sleep(2)
        (dest / 'rag_model_manifest.json').write_text(json.dumps({
            'repo': repo, 'revision': revision,
            'files': [s for s in info['siblings'] if s['rfilename'] in wanted],
        }, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(os.environ.get('RAG_MODEL_ROOT', 'models/rag')))
    parser.add_argument('--endpoint', default='https://hf-mirror.com')
    parser.add_argument('--repo', default='BAAI/bge-m3')
    args = parser.parse_args()
    download(args.repo, args.root, args.endpoint.rstrip('/'))
