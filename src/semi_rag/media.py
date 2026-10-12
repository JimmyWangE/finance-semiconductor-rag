from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

ALLOWED = {
    'image/jpeg': ('image', '.jpg'),
    'image/png': ('image', '.png'),
    'image/webp': ('image', '.webp'),
    'video/mp4': ('video', '.mp4'),
    'video/webm': ('video', '.webm'),
    'video/quicktime': ('video', '.mov'),
}
IMAGE_MAX = 10 * 1024 * 1024
VIDEO_MAX = 30 * 1024 * 1024
MAX_ATTACHMENTS = 4


def _valid_header(data: bytes, mime: str) -> bool:
    if mime == 'image/jpeg':
        return data.startswith(b'\xff\xd8\xff')
    if mime == 'image/png':
        return data.startswith(b'\x89PNG\r\n\x1a\n')
    if mime == 'image/webp':
        return data.startswith(b'RIFF') and data[8:12] == b'WEBP'
    if mime in ('video/mp4', 'video/quicktime'):
        return len(data) >= 12 and data[4:8] == b'ftyp'
    if mime == 'video/webm':
        return data.startswith(b'\x1aE\xdf\xa3')
    return False


class MediaStore:
    def __init__(self, config: dict):
        self.root = Path(config['data_dir']) / 'rag/media'
        self.root.mkdir(parents=True, exist_ok=True)

    def _owner_root(self, owner: str) -> Path:
        if not re.fullmatch(r'[0-9a-f]{64}', owner):
            raise ValueError('Invalid media owner')
        root = self.root / owner
        root.mkdir(exist_ok=True)
        return root

    def save(self, owner: str, stream, filename: str, mime: str) -> dict:
        if mime not in ALLOWED:
            raise ValueError('仅支持 JPG、PNG、WebP、MP4、WebM 和 MOV')
        kind, suffix = ALLOWED[mime]
        maximum = IMAGE_MAX if kind == 'image' else VIDEO_MAX
        data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError(f'{"图片" if kind == "image" else "视频"}超过 {maximum // 1024 // 1024} MB')
        if not data or not _valid_header(data, mime):
            raise ValueError('文件内容与声明的媒体格式不一致')
        media_id = hashlib.sha256(data).hexdigest()
        root = self._owner_root(owner)
        path = root / f'{media_id}{suffix}'
        path.write_bytes(data)
        metadata = {'id': media_id, 'name': Path(filename.replace('\\', '/')).name[:160] or f'附件{suffix}',
                    'mime': mime, 'kind': kind, 'size': len(data), 'suffix': suffix}
        (root / f'{media_id}.json').write_text(json.dumps(metadata, ensure_ascii=False))
        return self.public(metadata)

    def get(self, owner: str, media_id: str) -> tuple[dict, Path]:
        if not re.fullmatch(r'[0-9a-f]{64}', media_id):
            raise KeyError(media_id)
        root = self._owner_root(owner)
        try:
            metadata = json.loads((root / f'{media_id}.json').read_text())
        except (OSError, ValueError):
            raise KeyError(media_id) from None
        path = root / f'{media_id}{metadata["suffix"]}'
        if not path.is_file():
            raise KeyError(media_id)
        return metadata, path

    def resolve(self, owner: str, references: list[dict]) -> list[dict]:
        if len(references) > MAX_ATTACHMENTS:
            raise ValueError(f'每次最多添加 {MAX_ATTACHMENTS} 个媒体文件')
        resolved, seen = [], set()
        for reference in references:
            media_id = str(reference.get('id') or '')
            if media_id in seen:
                continue
            metadata, path = self.get(owner, media_id)
            seen.add(media_id)
            resolved.append({**metadata, 'path': path})
        if sum(item['kind'] == 'video' for item in resolved) > 1:
            raise ValueError('每次最多添加 1 个视频')
        return resolved

    @staticmethod
    def public(metadata: dict) -> dict:
        return {key: metadata[key] for key in ('id', 'name', 'mime', 'kind', 'size')} | {
            'url': f'/media/{metadata["id"]}'
        }


def content_parts(text: str, attachments: list[dict]) -> list[dict] | str:
    if not attachments:
        return text
    parts = [{'type': 'text', 'text': text}]
    for item in attachments:
        encoded = base64.b64encode(item['path'].read_bytes()).decode('ascii')
        parts.append({'type': f'{item["kind"]}_url', f'{item["kind"]}_url': {
            'url': f'data:{item["mime"]};base64,{encoded}'
        }})
    return parts


def token_safe_messages(messages: list[dict]) -> list[dict]:
    """Keep vision placeholders while avoiding tokenizer-specific video URL schemas."""
    result = []
    for message in messages:
        content = message.get('content')
        if not isinstance(content, list):
            result.append(message)
            continue
        normalized = []
        for part in content:
            if part.get('type') == 'video_url':
                normalized.append({'type': 'video', 'video': '<video>'})
            elif part.get('type') == 'image_url':
                normalized.append({'type': 'image', 'image': '<image>'})
            else:
                normalized.append(part)
        result.append({**message, 'content': normalized})
    return result
