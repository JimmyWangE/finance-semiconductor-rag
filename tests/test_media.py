from __future__ import annotations

import base64
import io

import pytest
from fastapi.testclient import TestClient

from semi_rag.api import create_app
from semi_rag.media import MediaStore, content_parts, token_safe_messages
from test_rag import FakeAnswerer, FakeRetriever


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
)


def test_media_store_validates_content_and_owner(tmp_path):
    store = MediaStore({'data_dir': str(tmp_path)})
    owner_a, owner_b = 'a' * 64, 'b' * 64
    saved = store.save(owner_a, io.BytesIO(PNG), '../chart.png', 'image/png')
    assert saved['name'] == 'chart.png' and saved['kind'] == 'image'
    metadata, path = store.get(owner_a, saved['id'])
    assert path.read_bytes() == PNG and metadata['mime'] == 'image/png'
    with pytest.raises(KeyError):
        store.get(owner_b, saved['id'])
    with pytest.raises(ValueError, match='格式不一致'):
        store.save(owner_a, io.BytesIO(b'not png'), 'fake.png', 'image/png')
    with pytest.raises(ValueError, match='仅支持'):
        store.save(owner_a, io.BytesIO(b'text'), 'note.txt', 'text/plain')


def test_multimodal_content_uses_data_urls_and_token_placeholders(tmp_path):
    path = tmp_path / 'chart.png'
    path.write_bytes(PNG)
    attachments = [{'path': path, 'kind': 'image', 'mime': 'image/png'}]
    content = content_parts('分析图表', attachments)
    assert content[0] == {'type': 'text', 'text': '分析图表'}
    assert content[1]['image_url']['url'] == 'data:image/png;base64,' + base64.b64encode(PNG).decode()
    messages = token_safe_messages([{'role': 'user', 'content': [
        {'type': 'text', 'text': '视频'},
        {'type': 'video_url', 'video_url': {'url': 'data:video/mp4;base64,AA=='}},
    ]}])
    assert messages[0]['content'][1] == {'type': 'video', 'video': '<video>'}


def test_media_api_upload_preview_and_session_isolation(tmp_path):
    app = create_app({'data_dir': str(tmp_path), 'pdf_dir': str(tmp_path)}, FakeRetriever(), FakeAnswerer())
    with TestClient(app) as first:
        response = first.post('/media', files={'file': ('chart.png', PNG, 'image/png')})
        assert response.status_code == 201
        item = response.json()
        preview = first.get(item['url'])
        assert preview.status_code == 200 and preview.content == PNG
        invalid = first.post('/media', files={'file': ('chart.png', b'bad', 'image/png')})
        assert invalid.status_code == 422
    with TestClient(app) as second:
        assert second.get(item['url']).status_code == 404
