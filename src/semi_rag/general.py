from __future__ import annotations

import json
import time
from collections.abc import Generator

from semi_rag.media import MediaStore, content_parts, token_safe_messages


SYSTEM = '''你是一个通用问答助手，可以使用模型训练中学到的一般知识完成分析、写作、编程和日常问答。
当前是通用问答模式，不连接研报知识库，也不把研究工作台的文档或引用当作上下文。
回答应直接、准确并清楚区分事实、推断和不确定信息。对于可能随时间变化的信息，明确说明知识可能过时，
不得声称已经联网、查询了实时数据或看到了用户未提供的资料。不要虚构来源、链接、数据或执行结果。'''


def messages(answerer, query: str, history: list[dict], attachments: list[dict] | None = None) -> tuple[list[dict], int]:
    limit = answerer.config.get('general_history_turns',
                                answerer.config.get('conversation_history_turns', 8))
    turns = history[-limit:] if limit > 0 else []
    result = [{'role': 'system', 'content': SYSTEM}]
    for turn in turns:
        question = turn.get('query')
        answer = turn.get('response', {}).get('answer')
        if isinstance(question, str) and isinstance(answer, str) and question.strip() and answer.strip():
            result.extend(({'role': 'user', 'content': question},
                           {'role': 'assistant', 'content': answer}))
    result.append({'role': 'user', 'content': content_parts(query, attachments or [])})
    used = (len(result) - 2) // 2
    budget = answerer.config.get('general_context_tokens',
                                 answerer.config.get('conversation_history_tokens', 5000))
    while used and answerer.token_count(token_safe_messages(result)) > budget:
        del result[1:3]
        used -= 1
    if answerer.token_count(token_safe_messages(result)) > budget:
        raise ValueError('问题超过通用问答上下文限制，请缩短后重试')
    return result, used


def answer_general(answerer, query: str, history: list[dict], attachments: list[dict] | None = None) -> dict:
    started = time.monotonic()
    prompt, history_used = messages(answerer, query, history, attachments)
    response = answerer.client.post('chat/completions', json={
        'model': answerer.config['llm_model'],
        'messages': prompt,
        'temperature': answerer.config.get('general_temperature', 0.3),
        'max_tokens': answerer.config.get('general_generation_max_tokens',
                                          answerer.config['generation_max_tokens']),
        'chat_template_kwargs': {'enable_thinking': False},
    })
    response.raise_for_status()
    completion = response.json()['choices'][0]
    answer = (completion.get('message', {}).get('content') or '').strip()
    if not answer:
        raise ValueError('生成服务返回了空回答，请重试')
    truncated = completion.get('finish_reason') == 'length'
    return {
        'query': query,
        'answer': answer,
        'answerable': True,
        'status': 'truncated' if truncated else 'answered',
        'mode': 'general',
        'model': answerer.config['llm_model'],
        'history_turns_used': history_used,
        'input_tokens': answerer.token_count(token_safe_messages(prompt)),
        'attachments': [MediaStore.public(item) for item in attachments or []],
        'sources': [],
        'claims': [],
        'latency_ms': round((time.monotonic() - started) * 1000, 1),
    }


def stream_general(answerer, query: str, history: list[dict], attachments: list[dict] | None = None) -> Generator[str, None, dict]:
    """Yield upstream text deltas and return the completed response metadata."""
    started = time.monotonic()
    prompt, history_used = messages(answerer, query, history, attachments)
    finish_reason = None
    parts = []
    with answerer.client.stream('POST', 'chat/completions', json={
        'model': answerer.config['llm_model'],
        'messages': prompt,
        'temperature': answerer.config.get('general_temperature', 0.3),
        'max_tokens': answerer.config.get('general_generation_max_tokens',
                                          answerer.config['generation_max_tokens']),
        'chat_template_kwargs': {'enable_thinking': False},
        'stream': True,
    }) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith('data:'):
                continue
            data = line[5:].strip()
            if not data or data == '[DONE]':
                continue
            payload = json.loads(data)
            choices = payload.get('choices') or []
            if not choices:
                continue
            choice = choices[0]
            finish_reason = choice.get('finish_reason') or finish_reason
            delta = choice.get('delta', {}).get('content') or ''
            if delta:
                parts.append(delta)
                yield delta
    answer = ''.join(parts).strip()
    if not answer:
        raise ValueError('生成服务返回了空回答，请重试')
    return {
        'query': query,
        'answer': answer,
        'answerable': True,
        'status': 'truncated' if finish_reason == 'length' else 'answered',
        'mode': 'general',
        'model': answerer.config['llm_model'],
        'history_turns_used': history_used,
        'input_tokens': answerer.token_count(token_safe_messages(prompt)),
        'attachments': [MediaStore.public(item) for item in attachments or []],
        'sources': [],
        'claims': [],
        'latency_ms': round((time.monotonic() - started) * 1000, 1),
    }
