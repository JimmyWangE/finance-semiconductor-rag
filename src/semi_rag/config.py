from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_config(path: str | Path | None = None) -> dict:
    path = Path(path or os.environ.get('RAG_CONFIG', ROOT / 'configs/rag.json'))
    config = json.loads(path.read_text())
    for key in ('data_dir', 'index_dir', 'pdf_dir', 'embedding_model',
                'reranker_model', 'llm_tokenizer', 'llm_api_key_file'):
        value = Path(config[key])
        config[key] = str(value if value.is_absolute() else ROOT / value)
    for key in ('device', 'llm_base_url', 'llm_model', 'index_dir'):
        if f'RAG_{key.upper()}' in os.environ:
            config[key] = os.environ[f'RAG_{key.upper()}']
    if not 0 <= config['chunk_overlap'] < config['chunk_chars']:
        raise ValueError('chunk_overlap must be smaller than chunk_chars')
    return config
