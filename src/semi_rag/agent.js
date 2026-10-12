(() => {
  const form = document.querySelector('#agent-form');
  const tickerInput = document.querySelector('#ticker');
  const stockOptions = document.querySelector('#stock-options');
  const stockHint = document.querySelector('#stock-hint');
  const dateInput = document.querySelector('#trade-date');
  const dateSource = document.querySelector('#date-source');
  const error = document.querySelector('#form-error');
  const body = document.querySelector('#result-body');
  const status = document.querySelector('#job-status');
  const list = document.querySelector('#job-list');
  const submit = document.querySelector('#run-analysis');
  const cancelButton = document.querySelector('#cancel-analysis');
  const retryButton = document.querySelector('#retry-analysis');
  const snapshots = new Map();
  const statuses = {queued: '排队中', running: '分析中', completed: '已完成', failed: '失败', cancelled: '已中断'};
  const reportTitles = {
    market_report: '市场分析', news_report: '新闻分析', fundamentals_report: '基本面分析',
    sentiment_report: '情绪分析', investment_debate_state: '多空研究',
    risk_debate_state: '风险辩论', final_trade_decision: '研究结论'
  };
  let selected = null;
  let selectedJob = null;
  let stream = null;
  let streamId = null;
  let busy = false;
  let healthy = false;
  let dateEdited = false;
  let chosen = null;
  let matches = [];
  let activeOption = -1;
  let searchTimer = null;
  let searchVersion = 0;
  const savedJobKey = 'finance-agent:selected-job';
  try { selected = sessionStorage.getItem(savedJobKey); } catch (_) { /* Storage may be disabled. */ }

  const today = new Date();
  dateInput.max = today.toISOString().slice(0, 10);
  while (today.getDay() === 0 || today.getDay() === 6) today.setDate(today.getDate() - 1);
  dateInput.value = today.toISOString().slice(0, 10);
  dateInput.addEventListener('input', () => { dateEdited = true; });

  async function request(path, options) {
    const response = await fetch(path, {credentials: 'same-origin', ...options});
    const data = (response.headers.get('content-type') || '').includes('application/json')
      ? await response.json() : null;
    if (!response.ok) {
      if (response.status === 401) throw new Error('登录已过期，请重新登录');
      throw new Error(typeof data?.detail === 'string' ? data.detail : `请求失败 (${response.status})，请稍后重试`);
    }
    if (data === null) throw new Error('服务返回了无法识别的内容，请稍后重试');
    return data;
  }

  function paragraph(text, className) {
    const p = document.createElement('p');
    p.className = className;
    p.textContent = text;
    return p;
  }

  function closeOptions() {
    stockOptions.hidden = true;
    tickerInput.setAttribute('aria-expanded', 'false');
    tickerInput.removeAttribute('aria-activedescendant');
    activeOption = -1;
  }

  function choose(stock) {
    chosen = stock;
    tickerInput.value = `${stock.name} (${stock.exchange}${stock.ticker})`;
    stockHint.textContent = `已确认：${stock.name} · ${stock.exchange}${stock.ticker}`;
    closeOptions();
    dateEdited = false;
    resolveTradeDate(stock.ticker);
  }

  function renderOptions() {
    stockOptions.replaceChildren();
    if (!matches.length) { closeOptions(); return; }
    matches.forEach((stock, index) => {
      const option = document.createElement('button');
      option.type = 'button';
      option.id = `stock-option-${index}`;
      option.className = `stock-option${index === activeOption ? ' active' : ''}`;
      option.setAttribute('role', 'option');
      option.setAttribute('aria-selected', String(index === activeOption));
      const name = document.createElement('span');
      name.textContent = stock.name;
      const code = document.createElement('small');
      code.textContent = `${stock.exchange}${stock.ticker}`;
      option.append(name, code);
      option.addEventListener('mousedown', event => event.preventDefault());
      option.addEventListener('click', () => choose(stock));
      stockOptions.append(option);
    });
    stockOptions.hidden = false;
    tickerInput.setAttribute('aria-expanded', 'true');
    if (activeOption >= 0) tickerInput.setAttribute('aria-activedescendant', `stock-option-${activeOption}`);
    else tickerInput.removeAttribute('aria-activedescendant');
  }

  async function searchStocks(value) {
    const version = ++searchVersion;
    if (value.trim().length < 2) {
      matches = [];
      closeOptions();
      stockHint.textContent = '至少输入 2 个字符';
      return;
    }
    stockHint.textContent = '正在查找标的…';
    try {
      const data = await request(`/agent/stocks?q=${encodeURIComponent(value.trim())}`);
      if (version !== searchVersion || tickerInput.value !== value) return;
      matches = data.matches;
      activeOption = -1;
      renderOptions();
      stockHint.textContent = matches.length ? `找到 ${matches.length}${matches.length === 20 ? '+' : ''} 个候选，请选择标的` : '未找到匹配的 A 股，请检查名称或代码';
    } catch (exc) {
      if (version !== searchVersion) return;
      stockHint.textContent = exc.message;
      closeOptions();
    }
  }

  tickerInput.addEventListener('input', () => {
    chosen = null;
    dateEdited = false;
    error.textContent = '';
    clearTimeout(searchTimer);
    const value = tickerInput.value;
    searchTimer = setTimeout(() => searchStocks(value), 250);
  });
  tickerInput.addEventListener('keydown', event => {
    if (stockOptions.hidden) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      activeOption = (activeOption + (event.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length;
      renderOptions();
      stockOptions.querySelector('.active')?.scrollIntoView({block: 'nearest'});
    } else if (event.key === 'Enter' && activeOption >= 0) {
      event.preventDefault();
      choose(matches[activeOption]);
    } else if (event.key === 'Escape') closeOptions();
  });
  tickerInput.addEventListener('blur', () => setTimeout(closeOptions, 150));
  tickerInput.addEventListener('focus', () => { if (matches.length && !chosen) renderOptions(); });

  async function resolveTradeDate(ticker) {
    if (!ticker || dateEdited) return;
    dateSource.textContent = '查询交易日中';
    try {
      const data = await request(`/agent/latest-date/${ticker}`);
      if (chosen?.ticker !== ticker || dateEdited) return;
      dateInput.value = data.trade_date;
      dateSource.textContent = `最近可用交易日 ${data.trade_date} · ${data.source}`;
    } catch (exc) {
      dateSource.textContent = '交易日查询失败，请核对分析日期';
    }
  }

  async function confirmStock() {
    if (chosen) return chosen;
    const value = tickerInput.value.trim();
    const data = await request(`/agent/stocks?q=${encodeURIComponent(value)}`);
    const normalized = value.toUpperCase().replace(/^(SH|SZ)\.?/, '');
    const exact = data.matches.filter(item => item.ticker === normalized || item.name === value
      || (value.length >= 4 && item.name.length >= 3 && value.includes(item.name)));
    if (exact.length === 1) {
      const prefix = value.toUpperCase().match(/^(SH|SZ)/)?.[1];
      if (prefix && prefix !== exact[0].exchange) throw new Error('股票代码前缀与交易所不一致');
      choose(exact[0]);
      return chosen;
    }
    if (!data.matches.length) throw new Error('未找到该 A 股标的，请检查代码或名称');
    matches = data.matches;
    renderOptions();
    throw new Error('名称匹配到多个候选，请从列表选择并确认标的');
  }

  function snapshot(id) {
    if (!snapshots.has(id)) snapshots.set(id, {steps: [], states: {}, text: {}, cursor: 0});
    return snapshots.get(id);
  }

  function applyEvent(id, event) {
    const view = snapshot(id);
    if (event.id && event.id <= view.cursor) return;
    view.cursor = event.id || view.cursor;
    if (event.type === 'plan') view.steps = event.steps || [];
    if (event.type === 'step') view.states[event.step] = {status: event.status, elapsed: event.elapsed, at: event.created_at};
    if (event.type === 'text') view.text[event.field] = (view.text[event.field] || '') + event.chunk;
    if (id === selected) render();
    if (event.type === 'status' && ['completed', 'failed', 'cancelled'].includes(event.status)) {
      if (streamId === id) stream?.close();
      refresh();
    }
  }

  function connectStream(id) {
    if (streamId === id) return;
    stream?.close();
    streamId = id;
    const view = snapshot(id);
    stream = new EventSource(`/agent/jobs/${id}/events?after=${view.cursor}`);
    stream.onmessage = event => {
      document.querySelector('.agent-reconnect')?.remove();
      applyEvent(id, JSON.parse(event.data));
    };
    stream.onerror = () => {
      if (selected !== id || ['completed', 'failed', 'cancelled'].includes(selectedJob?.status)) return;
      if (!document.querySelector('.agent-reconnect')) body.append(paragraph('连接中断，正在续接进度…', 'agent-reconnect'));
    };
  }

  function render() {
    const job = selectedJob;
    if (!job) return;
    const view = snapshot(job.id);
    const previousScroll = body.parentElement.scrollTop;
    status.textContent = statuses[job.status] || job.status;
    cancelButton.hidden = !['queued', 'running'].includes(job.status);
    retryButton.hidden = !['failed', 'cancelled'].includes(job.status);
    body.replaceChildren(paragraph(`${job.ticker} · ${job.trade_date} · ${job.analysts.join(' / ')}`, 'agent-meta'));
    if (view.steps.length) {
      const timeline = document.createElement('ol');
      timeline.className = 'agent-progress';
      for (const step of view.steps) {
        const state = view.states[step.id] || {status: 'waiting'};
        const row = document.createElement('li');
        row.className = `agent-step${state.status === 'running' ? ' current' : ''} ${state.status}`;
        const icon = document.createElement('span');
        icon.className = 'agent-step-icon';
        icon.textContent = state.status === 'completed' ? '✓' : state.status === 'running' ? '●' : '○';
        const label = document.createElement('span');
        label.textContent = `${step.title} · ${{waiting: '等待中', running: '进行中', completed: '已完成'}[state.status] || state.status}`;
        const elapsed = document.createElement('time');
        elapsed.dataset.elapsed = String(state.elapsed || 0);
        if (state.status === 'running') elapsed.dataset.started = state.at || '';
        elapsed.textContent = `${Math.floor(state.elapsed || 0)}s`;
        row.append(icon, label, elapsed);
        timeline.append(row);
      }
      body.append(timeline);
    }
    if (job.status === 'failed') body.append(paragraph(job.error || '分析失败，已保留进度；可点击重试', 'agent-failure'));
    if (job.status === 'cancelled') body.append(paragraph('分析已中断，已生成内容保留在下方。', 'agent-empty'));
    const reports = job.status === 'completed' && job.result ? job.result : view.text;
    let reportCount = 0;
    for (const [key, title] of Object.entries(reportTitles)) {
      const value = reports[key];
      if (!value || (typeof value === 'object' && !Object.keys(value).length)) continue;
      const section = document.createElement('section');
      section.className = 'agent-report';
      const heading = document.createElement('h3');
      heading.textContent = title;
      const pre = document.createElement('pre');
      pre.textContent = typeof value === 'string' ? value : Object.entries(value).map(([k, v]) => `${k}\n${v}`).join('\n\n');
      section.append(heading, pre);
      body.append(section);
      reportCount += 1;
    }
    if (!reportCount && ['queued', 'running'].includes(job.status)) body.append(paragraph(job.status === 'queued' ? '等待执行' : '正在获取数据并运行分析节点…', 'agent-empty'));
    body.parentElement.scrollTop = previousScroll;
  }

  async function refresh() {
    try {
      const health = await request('/agent/health');
      healthy = health.ready;
      submit.disabled = !healthy || busy;
      document.querySelector('#service-state').textContent = healthy ? '就绪' : '运行环境未就绪';
      const jobs = await request('/agent/jobs');
      list.replaceChildren();
      document.querySelector('#job-count').textContent = String(jobs.length);
      for (const job of jobs) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `session${job.id === selected ? ' active' : ''}`;
        button.innerHTML = '<span class="session-title"></span><small></small>';
        button.querySelector('.session-title').textContent = `${job.ticker} · ${job.trade_date}`;
        button.querySelector('small').textContent = statuses[job.status] || job.status;
        button.addEventListener('click', () => selectJob(job));
        list.append(button);
      }
      const current = jobs.find(job => job.id === selected) || jobs[0];
      if (current) selectJob(current);
      else if (selected) {
        selected = null;
        selectedJob = null;
        stream?.close();
        streamId = null;
        try { sessionStorage.removeItem(savedJobKey); } catch (_) { /* Storage may be disabled. */ }
      }
    } catch (exc) {
      document.querySelector('#service-state').textContent = '连接失败';
      error.textContent = exc.message;
    }
  }

  function selectJob(job) {
    selected = job.id;
    selectedJob = job;
    try { sessionStorage.setItem(savedJobKey, job.id); } catch (_) { /* Storage may be disabled. */ }
    render();
    connectStream(job.id);
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    error.textContent = '';
    const analysts = [...form.querySelectorAll('input[name="analyst"]:checked')].map(item => item.value);
    if (!analysts.length) { error.textContent = '请至少选择一位分析角色'; return; }
    busy = true;
    submit.disabled = true;
    try {
      const stock = await confirmStock();
      if (!dateEdited) await resolveTradeDate(stock.ticker);
      const job = await request('/agent/jobs', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ticker: stock.ticker, trade_date: dateInput.value, analysts})
      });
      selectJob(job);
      await refresh();
    } catch (exc) { error.textContent = exc.message; tickerInput.focus(); }
    finally { busy = false; submit.disabled = !healthy; }
  });

  cancelButton.addEventListener('click', async () => {
    if (!selectedJob) return;
    cancelButton.disabled = true;
    try {
      const job = await request(`/agent/jobs/${selectedJob.id}/cancel`, {method: 'POST'});
      selectJob(job);
      await refresh();
    } catch (exc) { error.textContent = exc.message; }
    finally { cancelButton.disabled = false; }
  });
  retryButton.addEventListener('click', async () => {
    if (!selectedJob) return;
    retryButton.disabled = true;
    try {
      const job = await request(`/agent/jobs/${selectedJob.id}/retry`, {method: 'POST'});
      selectJob(job);
      await refresh();
    } catch (exc) { error.textContent = exc.message; }
    finally { retryButton.disabled = false; }
  });

  document.querySelector('#toggle-history').addEventListener('click', event => {
    const open = document.body.classList.toggle('history-open');
    event.currentTarget.setAttribute('aria-expanded', String(open));
  });
  refresh();
  const poll = setInterval(refresh, 5000);
  const clock = setInterval(() => {
    body.querySelectorAll('time[data-started]').forEach(node => {
      const start = Date.parse(node.dataset.started);
      if (Number.isFinite(start)) node.textContent = `${Math.max(0, Math.floor((Date.now() - start) / 1000))}s`;
    });
  }, 1000);
  window.addEventListener('pagehide', () => { clearInterval(poll); clearInterval(clock); stream?.close(); });
})();
