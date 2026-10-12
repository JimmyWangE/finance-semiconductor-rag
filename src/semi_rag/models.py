from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import numpy as np
import torch
from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer


def model_fingerprint(path: str) -> str:
    root = Path(path)
    manifest = root / 'rag_model_manifest.json'
    files = sorted(p for p in root.iterdir() if p.is_file() and p.suffix in {'.json', '.bin', '.safetensors', '.model'})
    signature = [(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in files]
    return hashlib.sha256(json.dumps([str(root.resolve()), signature,
        manifest.read_text() if manifest.exists() else '']).encode()).hexdigest()


class Encoder:
    def __init__(self, config: dict):
        self.config = config
        self.device = config['device']
        self.lock = threading.Lock()
        torch.set_num_threads(8)
        self.tokenizer = AutoTokenizer.from_pretrained(config['embedding_model'], local_files_only=True)
        self.model = AutoModel.from_pretrained(config['embedding_model'], local_files_only=True,
            dtype=torch.float16 if self.device.startswith('cuda') else torch.float32,
            attn_implementation='eager').to(self.device).eval()

    def encode(self, texts: list[str]) -> np.ndarray:
        result = []
        with self.lock, torch.inference_mode():
            for start in range(0, len(texts), self.config['batch_size']):
                tokens = self.tokenizer(texts[start:start + self.config['batch_size']], padding=True,
                    truncation=True, max_length=self.config['embedding_max_tokens'], return_tensors='pt').to(self.device)
                # BGE-M3 dense embeddings use normalized CLS, not mean pooling.
                vectors = self.model(**tokens).last_hidden_state[:, 0].float()
                vectors = torch.nn.functional.normalize(vectors, p=2, dim=1)
                result.append(vectors.cpu().numpy())
        return np.concatenate(result).astype('float32')


class Reranker:
    def __init__(self, config: dict):
        self.config = config
        self.lock = threading.Lock()
        self.tokenizer = AutoTokenizer.from_pretrained(config['reranker_model'], local_files_only=True)
        self.model = AutoModelForSequenceClassification.from_pretrained(config['reranker_model'],
            local_files_only=True, dtype=torch.float16 if config['device'].startswith('cuda') else torch.float32,
            attn_implementation='eager').to(config['device']).eval()

    def score(self, query: str, texts: list[str]) -> list[float]:
        scores = []
        with self.lock, torch.inference_mode():
            for start in range(0, len(texts), self.config['batch_size']):
                pairs = [[query, text] for text in texts[start:start + self.config['batch_size']]]
                tokens = self.tokenizer(pairs, padding=True, truncation='only_second',
                    max_length=self.config['reranker_max_tokens'], return_tensors='pt').to(self.config['device'])
                scores.extend(torch.sigmoid(self.model(**tokens).logits.view(-1).float()).cpu().tolist())
        return scores
