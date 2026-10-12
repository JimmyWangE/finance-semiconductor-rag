"""Bounded closed-loop concurrency and open-loop arrival-rate experiments."""
from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict, deque

import httpx

from semi_rag.eval_metrics import summarize_load


def classify_response(response: httpx.Response, endpoint: str) -> tuple[str, dict]:
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if response.status_code == 429 or (response.status_code == 503 and
        response.headers.get('retry-after') and 'busy' in str(data.get('detail', '')).lower()):
        return 'rejected', data
    if response.status_code != 200:
        return 'http_error', data
    if endpoint == '/search':
        hits = data.get('hits')
        valid = (isinstance(hits, list) and isinstance(data.get('generation'), str)
                 and all(isinstance(hit, dict) and isinstance(hit.get('chunk_id'), str)
                         and isinstance(hit.get('doc_id'), str) for hit in hits))
    else:
        valid = (type(data.get('answerable')) is bool and isinstance(data.get('answer'), str)
                 and isinstance(data.get('sources'), list) and isinstance(data.get('claims'), list)
                 and isinstance(data.get('generation'), str)
                 and data.get('status') in ('answered', 'insufficient_evidence', 'validation_failed', 'needs_clarification')
                 and data['answerable'] == (data['status'] == 'answered')
                 and (not data['answerable'] or bool(data['sources']) and bool(data['claims']))
                 and all(isinstance(s, dict) and isinstance(s.get('source_id'), str) and isinstance(s.get('text'), str) for s in data['sources'])
                 and all(isinstance(c, dict) and isinstance(c.get('text'), str) and isinstance(c.get('citations'), list)
                         and all(isinstance(cite, dict) and isinstance(cite.get('source_id'), str)
                                 and isinstance(cite.get('quote'), str) for cite in c['citations']) for c in data['claims']))
    return ('success' if valid else 'invalid_response'), data


async def send_request(client, endpoint, payload, query_id, scheduled, phase_started, number):
    started = time.monotonic()
    record = {'number': number, 'query_id': query_id, 'started_s': started - phase_started,
              'schedule_lag_ms': max(0., (started - scheduled) * 1000), 'status_code': None,
              'answer_status': None, 'generation': None, 'retry_after': None}
    try:
        response = await client.post(endpoint, json=payload)
        outcome, data = classify_response(response, endpoint)
        record.update(outcome=outcome, status_code=response.status_code,
                      answer_status=data.get('status') if endpoint == '/ask' else None,
                      generation=data.get('generation'), retry_after=response.headers.get('retry-after'))
        if outcome != 'success':
            record['error'] = str(data.get('detail', 'HTTP or response schema error'))[:200]
    except httpx.TimeoutException:
        record.update(outcome='timeout', error='Client timeout; server may still be processing')
    except httpx.HTTPError as exc:
        record.update(outcome='transport_error', error=type(exc).__name__)
    record['latency_ms'] = (time.monotonic() - started) * 1000
    record['completed_s'] = time.monotonic() - phase_started
    return record


async def run_phase(client, workload, *, endpoint, kind, value, duration, requests,
                    max_inflight, max_requests, min_interval, error_stop, min_error_samples, sink=None):
    """No automatic retries: rejection latency must not create an unbounded retry storm."""
    started = time.monotonic()
    deadline = started + duration
    records, active = [], set()
    peak, next_number = 0, 0
    abort = None
    recent_sent = deque(maxlen=min_error_samples)

    def finished(record):
        nonlocal abort
        records.append(record)
        if sink:
            sink(record)
        if record['outcome'] != 'client_dropped':
            recent_sent.append(record)
        if len(recent_sent) >= min_error_samples:
            fraction = sum(row['outcome'] not in ('success', 'rejected') for row in recent_sent) / len(recent_sent)
            if fraction >= error_stop:
                abort = 'hard_error_threshold'

    async def execute(number, scheduled):
        entry = workload[number % len(workload)]
        record = await send_request(client, endpoint, entry['payload'], entry['id'], scheduled, started, number)
        finished(record)

    async def worker():
        nonlocal peak, next_number
        while not abort:
            if requests is not None and next_number >= requests:
                break
            if time.monotonic() >= deadline or next_number >= max_requests:
                break
            number = next_number
            next_number += 1
            scheduled = time.monotonic()
            task = asyncio.current_task()
            active.add(task)
            peak = max(peak, len(active))
            try:
                await execute(number, scheduled)
            finally:
                active.discard(task)
            await asyncio.sleep(max(0., min_interval - (time.monotonic() - scheduled)))

    if kind == 'closed':
        await asyncio.gather(*(worker() for _ in range(int(value))))
    elif kind == 'open':
        # Schedule arrivals independently of response completion. Drop instead of queueing
        # when the client in-flight bound is reached; report every dropped arrival.
        count = min(max_requests, math.ceil(duration * value))
        for number in range(count):
            scheduled = started + number / value
            await asyncio.sleep(max(0., scheduled - time.monotonic()))
            if abort:
                break
            active = {task for task in active if not task.done()}
            if len(active) >= max_inflight or time.monotonic() - scheduled > max(1., 2 / value):
                finished({'number': number, 'query_id': workload[number % len(workload)]['id'],
                    'started_s': time.monotonic() - started, 'completed_s': time.monotonic() - started,
                    'schedule_lag_ms': (time.monotonic() - scheduled) * 1000, 'status_code': None,
                    'latency_ms': 0., 'outcome': 'client_dropped', 'answer_status': None, 'generation': None})
            else:
                active.add(asyncio.create_task(execute(number, scheduled)))
                peak = max(peak, len(active))
            next_number = number + 1
        if active:
            await asyncio.gather(*active)
        if not abort:
            await asyncio.sleep(max(0., deadline - time.monotonic()))
    else:
        raise ValueError('Unknown load kind')
    records.sort(key=lambda row: row['number'])
    wall = time.monotonic() - started
    summary = summarize_load(records, wall, scheduled_seconds=duration, peak_inflight=peak)
    summary.update(kind=kind, value=value, endpoint=endpoint, abort_reason=abort,
                   configured_requests=requests, reached_request_cap=next_number >= max_requests,
                   intended_arrivals=math.ceil(duration * value) if kind == 'open' else requests,
                   arrival_schedule_truncated=kind == 'open' and math.ceil(duration * value) > max_requests,
                   requested_count_completed=requests is None or len(records) >= requests)
    windows = defaultdict(lambda: {'success': 0, 'rejected': 0, 'errors': 0, 'client_dropped': 0})
    for row in records:
        category = row['outcome'] if row['outcome'] in ('success', 'rejected', 'client_dropped') else 'errors'
        windows[int(row['completed_s'])][category] += 1
    summary['completion_windows_1s'] = [{'second': second, **counts} for second, counts in sorted(windows.items())]
    return summary, records
