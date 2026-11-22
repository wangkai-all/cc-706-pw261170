/* Corpus Lake UI.
   Vanilla JS, no external dependencies.
   Data flow: every mutation goes through the JSON API, then the affected
   views are re-fetched. Similarity numbers shown here always come from the
   server's exact Jaccard computation; the client never estimates. */

'use strict';

/* ------------------------------------------------------------------ utils */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtInt(n) {
  return Number(n || 0).toLocaleString('zh-CN');
}

function fmtScore(x) {
  return (Number(x) * 100).toFixed(1) + '%';
}

function fmtScore2(x) {
  return Number(x).toFixed(4);
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleString('zh-CN', { hour12: false });
}

function shortId(id) {
  return String(id || '').replace(/^(doc|task|grp|grun)_/, '');
}

function truncate(s, n) {
  s = String(s || '');
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

function toast(message, bad = false) {
  const el = $('#toast');
  el.textContent = message;
  el.className = 'toast show' + (bad ? ' bad' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.className = 'toast'; }, bad ? 5200 : 2800);
}

async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET', headers: {} };
  if (opts.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, init);
  const ctype = resp.headers.get('Content-Type') || '';
  let data = null;
  if (ctype.includes('application/json')) data = await resp.json();
  else data = await resp.text();
  if (!resp.ok) {
    const err = new Error((data && data.error) || ('HTTP ' + resp.status));
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  return data;
}

function notice(kind, icon, html) {
  return `<div class="notice ${kind}"><span class="ico" aria-hidden="true">${icon}</span><div>${html}</div></div>`;
}

function badge(text, kind = '', icon = '') {
  return `<span class="badge ${kind}">${icon ? `<span class="ico" aria-hidden="true">${icon}</span>` : ''}${esc(text)}</span>`;
}

function meter(score, threshold, opts = {}) {
  const pct = Math.max(0, Math.min(1, Number(score))) * 100;
  const cls = opts.warnAt && score < opts.warnAt ? '' : '';
  const thr = threshold != null && threshold > 0
    ? `--threshold-pos:${(threshold * 100).toFixed(1)}%` : '';
  return `<span class="meter ${threshold != null ? 'threshold' : ''}" style="${thr}">
    <span class="meter-track" role="img" aria-label="Jaccard ${fmtScore(score)}${threshold != null ? '，阈值 ' + fmtScore(threshold) : ''}">
      <span class="meter-fill ${cls}" style="width:${pct.toFixed(2)}%"></span>
    </span>
    <span class="meter-value">${fmtScore(score)}</span>
  </span>`;
}

function modeBadge(task) {
  if (!task) return badge('无任务', 'muted');
  return task.mode === 'approx'
    ? badge('近似模式 · 可能漏召', 'warn', '≈')
    : badge('精确模式 · 可保证找全', 'ok', '✓');
}

function statusBadge(status) {
  const map = {
    completed: ['完成', 'ok', '✓'],
    running: ['运行中', 'info', '▶'],
    queued: ['排队', 'muted', '·'],
    stopped: ['已停止 · 可继续', 'warn', 'Ⅱ'],
    failed: ['失败', 'bad', '!'],
  };
  const [label, kind, icon] = map[status] || [status, '', ''];
  return badge(label, kind, icon);
}

function decisionBadge(decision) {
  if (decision === 'same_group') return badge('关联到同组', 'ok', '✓');
  if (decision === 'keep_separate') return badge('保留为独立', 'info', '∥');
  return badge('未审阅', 'muted', '·');
}

/* --------------------------------------------------------------- painting */

/**
 * Paint character ranges over a text without innerHTML injection.
 * ranges: [{start, end, classes:Set, seg:number|null}]
 * Returns escaped HTML with <mark> wrappers.
 */
function paintRanges(text, ranges) {
  const clean = (ranges || [])
    .filter(r => r && r.end > r.start && r.start >= 0 && r.end <= text.length)
    .sort((a, b) => a.start - b.start || a.end - b.end);
  if (!clean.length) return esc(text);
  const bounds = new Set([0, text.length]);
  for (const r of clean) { bounds.add(r.start); bounds.add(r.end); }
  const points = Array.from(bounds).sort((a, b) => a - b);
  let out = '';
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i], b = points[i + 1];
    if (b <= a) continue;
    const active = clean.filter(r => r.start <= a && r.end >= b);
    const chunk = esc(text.slice(a, b));
    if (!active.length) { out += chunk; continue; }
    const classes = new Set();
    let seg = null, title = '';
    for (const r of active) {
      (r.classes || []).forEach(c => classes.add(c));
      if (r.seg != null) seg = r.seg;
      if (r.title) title = r.title;
    }
    const classAttr = classes.size ? ` class="${Array.from(classes).sort().join(' ')}"` : '';
    const segAttr = seg != null ? ` data-seg="${seg}"` : '';
    const titleAttr = title ? ` title="${esc(title)}"` : '';
    out += `<mark${classAttr}${segAttr}${titleAttr}>${chunk}</mark>`;
  }
  return out;
}

/* ------------------------------------------------------------------ state */

const State = {
  view: 'overview',
  stats: null,
  task: null,
  tasks: [],
  docs: [],
  groupsData: null,
  edge: null,
  edgeList: [],
  filters: { minScore: 0, decision: '', doc: '' },
  pollTimer: null,
  compareSelection: null,
  includeWithdrawn: true,
};

/* ---------------------------------------------------------------- routing */

function parseRoute() {
  const hash = location.hash.replace(/^#\/?/, '');
  const [view, arg] = hash.split('/');
  return { view: view || 'overview', arg: arg || null };
}

function navigate(view, arg) {
  const hash = '#/' + view + (arg ? '/' + arg : '');
  if (location.hash === hash) render();
  else location.hash = hash;
}

window.addEventListener('hashchange', () => render());

/* -------------------------------------------------------------- chrome/nav */

function setTabs(view) {
  $$('.tab').forEach(tab => {
    const active = tab.dataset.view === view ||
      (view === 'compare' && tab.dataset.view === 'pairs');
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-current', active ? 'page' : 'false');
  });
}

function setBanner(html) {
  $('#banner-slot').innerHTML = html || '';
}

async function refreshHealth() {
  try {
    const health = await api('/api/health');
    $('#health-pill').innerHTML = `<span class="dot good"></span>本地服务正常`;
    return health;
  } catch (err) {
    $('#health-pill').innerHTML = `<span class="dot bad"></span>服务不可用`;
    throw err;
  }
}

/* ------------------------------------------------------------------ render */

async function render() {
  const { view, arg } = parseRoute();
  State.view = view;
  setTabs(view);
  setBanner('');
  stopPolling();
  try {
    const main = $('#main');
    if (view === 'overview') await renderOverview(main);
    else if (view === 'docs') await renderDocs(main);
    else if (view === 'pairs') await renderPairs(main);
    else if (view === 'compare') await renderCompare(main, arg);
    else if (view === 'groups') await renderGroups(main);
    else if (view === 'task') await renderTask(main);
    else { main.innerHTML = `<p>未知视图</p>`; }
  } catch (err) {
    console.error(err);
    $('#main').innerHTML = notice('bad', '!',
      `<strong>加载失败。</strong> ${esc(err.message)}`);
  }
}

async function loadBasics() {
  State.stats = await api('/api/stats');
  State.task = State.stats.task;
  $('#db-pill').textContent = 'db: ' + State.stats.db_path;
  $('#db-pill').title = State.stats.db_path;
  if (State.stats.task) {
    const r = State.stats.task.rules;
    $('#rules-foot').textContent =
      `${r.tokenizer.version} · ${r.phrases.version} · n=${r.phrases.n} · ${r.fingerprint}`;
  }
}

/* --------------------------------------------------------------- overview */

async function renderOverview(main) {
  await loadBasics();
  const stats = State.stats;
  const task = stats.task;
  main.innerHTML = `
    <section class="section">
      <div class="section-head">
        <div>
          <h2>总览</h2>
          <p>上传文章 → 按版本化规则切片 → 前缀索引 + 长度过滤生成候选 → 精确 Jaccard 复核 → 并排查看相同片段 → 决定保留或关联。</p>
        </div>
        ${task ? modeBadge(task) : ''}
      </div>
      <div class="kpi-grid">
        <div class="kpi">
          <span class="label">文档（有效）</span>
          <span class="value">${fmtInt(stats.documents - stats.withdrawn)}</span>
          <span class="hint">共 ${fmtInt(stats.documents)} 篇 · 空文本 ${fmtInt(stats.empty_documents)} 篇 · 已撤回 ${fmtInt(stats.withdrawn)} 篇</span>
        </div>
        <div class="kpi">
          <span class="label">相似边（精确 Jaccard）</span>
          <span class="value">${task ? fmtInt(stats.edges) : '—'}</span>
          <span class="hint">${task ? `阈值 ≥ ${fmtScore(task.threshold)} · 未审阅 ${fmtInt(task.unreviewed_edges)}` : '尚未建立索引'}</span>
        </div>
        <div class="kpi">
          <span class="label">分组（边连通分量）</span>
          <span class="value">${task ? fmtInt(stats.groups) : '—'}</span>
          <span class="hint">同组 ≠ 任意两篇相似</span>
        </div>
        <div class="kpi">
          <span class="label">当前模式</span>
          <span class="value" style="font-size:17px;line-height:1.5;margin-top:4px">${task ? (task.mode === 'approx' ? '近似（可能漏召）' : '精确（可保证找全）') : '未建索引'}</span>
          <span class="hint">${task ? esc(task.rules.phrases.description.split('；')[0]) : '先上传文档再建立任务'}</span>
        </div>
      </div>
    </section>

    ${notice('info', 'ℹ',
      `<strong>相似关系不是传递关系。</strong> A 像 B、B 像 C，不代表 A 像 C。
       分组只是“接受边的连通分量”，用于导航；页面与导出始终保留每一条边，并列出组内没有直接边的文档对。`)}

    <section class="section grid-2" style="margin-top:18px">
      <div class="card">
        <h3>上传文章</h3>
        <p class="sub">原文会完整保存；相同文本不会静默合并，会先询问你。</p>
        <form id="upload-form" class="stack">
          <label class="field"><span>标题（可选）</span>
            <input class="input" name="title" maxlength="200" placeholder="例如：会议纪要 2026-11">
          </label>
          <label class="field"><span>正文</span>
            <textarea class="textarea" name="text" rows="6" placeholder="粘贴正文…"></textarea>
          </label>
          <div class="row">
            <button class="btn primary" type="submit">加入语料</button>
            <span class="muted small">空文本会被接受并标记为“空文档”，不参与相似计算。</span>
          </div>
        </form>
        <hr class="hr">
        <form id="import-form" class="stack">
          <label class="field"><span>从服务器本地路径批量导入（.txt .md .jsonl .csv .json）</span>
            <input class="input" name="path" placeholder="/path/to/corpus 或 /path/to/file.jsonl">
          </label>
          <div class="row">
            <button class="btn" type="submit">导入路径</button>
            <span class="muted small">仅读取文本，不执行文件内容。</span>
          </div>
        </form>
      </div>

      <div class="card">
        <h3>建立索引任务</h3>
        <p class="sub">规则与词项排序都带版本。修改任意规则会生成新的规则集指纹与新的任务，不复用旧索引。</p>
        <form id="task-form" class="stack">
          <div class="field-row">
            <label class="field"><span>相似度阈值</span>
              <input class="input" type="number" name="threshold" min="0.05" max="1" step="0.01" value="${task ? task.threshold : 0.6}">
            </label>
            <label class="field"><span>词组长度 n</span>
              <input class="input" type="number" name="phrase_n" min="1" max="8" step="1" value="${task ? task.rules.phrases.n : 3}">
            </label>
            <label class="field"><span>候选生成模式</span>
              <select class="select" name="mode">
                <option value="exact" ${!task || task.mode === 'exact' ? 'selected' : ''}>精确（前缀索引 + 长度过滤，无漏召）</option>
                <option value="approx" ${task && task.mode === 'approx' ? 'selected' : ''}>近似（MinHash，可能漏召）</option>
              </select>
            </label>
          </div>
          <div class="row">
            <button class="btn primary" type="submit">创建并运行任务</button>
            <a class="btn ghost" href="#/task">查看任务与校验</a>
          </div>
        </form>
        <hr class="hr">
        <h4>规则摘要</h4>
        <dl class="kv" style="margin-top:8px">
          <dt>分词器</dt><dd class="mono">${task ? esc(task.rules.tokenizer.version) : 'corpuslake.tokenizer.v1'}</dd>
          <dt>词组切片</dt><dd class="mono">${task ? esc(task.rules.phrases.version) : 'corpuslake.phrases.v1'}</dd>
          <dt>全局词项排序</dt><dd class="mono">${task ? esc(task.rules.vocabulary.version) : 'corpuslake.vocab.v1'}</dd>
          <dt>规则指纹</dt><dd class="mono">${task ? esc(task.rules.fingerprint) : '—'}</dd>
        </dl>
      </div>
    </section>

    <section class="section">
      <div class="section-head">
        <h3>最近的相似边</h3>
        <a href="#/pairs">全部相似对 →</a>
      </div>
      <div id="recent-edges" class="table-wrap"></div>
    </section>
  `;

  bindUploadForm();
  bindImportForm();
  bindTaskForm();
  if (task) await loadRecentEdges();
  else $('#recent-edges').innerHTML = `<div class="empty-state">尚无索引任务。</div>`;
  if (task && task.status === 'running') startPolling();
}

async function loadRecentEdges() {
  const data = await api(`/api/tasks/${encodeURIComponent(State.task.id)}/edges?limit=8`);
  const host = $('#recent-edges');
  if (!host) return;
  if (!data.edges.length) {
    host.innerHTML = `<div class="empty-state">没有达到阈值的文档对。</div>`;
    return;
  }
  host.innerHTML = `<table>
    <thead><tr><th>文档对</th><th class="num">Jaccard</th><th>候选来源</th><th>审阅</th><th></th></tr></thead>
    <tbody>${data.edges.map(e => `
      <tr>
        <td><div class="trunc" title="${esc(e.doc_a_title)}">A · ${esc(e.doc_a_title)}</div>
            <div class="trunc muted" title="${esc(e.doc_b_title)}">B · ${esc(e.doc_b_title)}</div></td>
        <td class="num">${meter(e.jaccard, data.task.threshold)}</td>
        <td class="small muted">${esc(e.candidate_reason || e.provenance)}</td>
        <td>${decisionBadge(e.decision)}</td>
        <td class="num"><a class="btn small" href="#/compare/${e.edge_id}">并排查看</a></td>
      </tr>`).join('')}
    </tbody></table>`;
}

/* ------------------------------------------------------------------ forms */

function bindUploadForm() {
  const form = $('#upload-form');
  if (!form) return;
  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    const fd = new FormData(form);
    const title = (fd.get('title') || '').trim();
    const text = fd.get('text') || '';
    try {
      const created = await api('/api/documents', { method: 'POST', body: { title, text } });
      toast('已加入语料：' + created.document.title);
      form.reset();
      render();
    } catch (err) {
      if (err.status === 409 && err.data && err.data.identical) {
        showIdenticalDialog(title, text, err.data.existing);
      } else {
        toast(err.message, true);
      }
    }
  });
}

function showIdenticalDialog(title, text, existing) {
  showDialog({
    title: '完全相同的文本已存在',
    bodyHtml: `<p>这段文本与已有文档 <strong>${esc(existing.title)}</strong>
      （<span class="mono">${esc(existing.id)}</span>，${fmtInt(existing.char_count)} 字符）
      内容哈希完全相同。</p>
      <p class="muted small">按“保留原文与文档身份”的原则，不会自动合并。你可以打开已有文档，或把它作为新的独立身份入库（会显式记录 duplicate_of）。</p>`,
    actions: [
      { label: '取消', onClick: closeDialog },
      { label: '打开已有文档', onClick: () => { closeDialog(); navigate('docs'); } },
      {
        label: '作为独立文档保存', primary: true, onClick: async () => {
          try {
            const created = await api('/api/documents', {
              method: 'POST', body: { title, text, duplicate_of: existing.id },
            });
            closeDialog();
            toast('已作为独立文档保存');
            navigate('docs');
          } catch (err) { toast(err.message, true); }
        },
      },
    ],
  });
}

function bindImportForm() {
  const form = $('#import-form');
  if (!form) return;
  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    const path = new FormData(form).get('path');
    try {
      const result = await api('/api/import', { method: 'POST', body: { path } });
      toast(`导入完成：新增 ${result.imported}，跳过重复 ${result.skipped}，错误 ${result.errors.length}`);
      form.reset();
      render();
    } catch (err) { toast(err.message, true); }
  });
}

function bindTaskForm() {
  const form = $('#task-form');
  if (!form) return;
  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    const fd = new FormData(form);
    try {
      const body = {
        threshold: Number(fd.get('threshold')),
        phrase_n: Number(fd.get('phrase_n')),
        mode: fd.get('mode'),
      };
      const result = await api('/api/tasks', { method: 'POST', body });
      toast(result.task.mode === 'approx'
        ? '任务已启动（近似模式：不保证找全）'
        : '任务已启动（精确模式）');
      navigate('task');
    } catch (err) { toast(err.message, true); }
  });
}

/* -------------------------------------------------------------- documents */

async function renderDocs(main) {
  await loadBasics();
  State.docs = (await api('/api/documents'))?.documents || [];
  const active = State.docs.filter(d => !d.withdrawn_at);
  const withdrawn = State.docs.filter(d => d.withdrawn_at);

  main.innerHTML = `
    <section class="section">
      <div class="section-head">
        <div><h2>文档</h2>
          <p>原文与文档身份都保留。撤回不会删除文本；可以随时恢复，恢复后重新分组。</p></div>
        <div class="row">
          <label class="checkline"><input type="checkbox" id="show-withdrawn" ${State.includeWithdrawn ? 'checked' : ''}>显示已撤回</label>
          <span class="pill">${fmtInt(active.length)} 有效 · ${fmtInt(withdrawn.length)} 撤回</span>
        </div>
      </div>
      <div class="doc-list" id="doc-list"></div>
    </section>
  `;
  $('#show-withdrawn').addEventListener('change', ev => {
    State.includeWithdrawn = ev.target.checked;
    paintDocs();
  });
  paintDocs();
}

function paintDocs() {
  const host = $('#doc-list');
  const list = State.includeWithdrawn ? State.docs : State.docs.filter(d => !d.withdrawn_at);
  if (!list.length) {
    host.innerHTML = `<div class="empty-state">暂无文档。可在总览页上传或导入。</div>`;
    return;
  }
  host.innerHTML = list.map(d => {
    const empty = d.is_empty;
    const short = !empty && d.phrase_count === 0;
    const tags = [];
    if (empty) tags.push(badge('空文档 · 不参与相似计算', 'warn', '∅'));
    if (short) tags.push(badge('短于一个词组', 'warn', '…'));
    if (d.duplicate_of) tags.push(badge('完全相同文本的独立副本', 'info', '⧉'));
    if (d.withdrawn_at) tags.push(badge('已撤回', 'muted', '↩'));
    const action = d.withdrawn_at
      ? `<button class="btn small" data-restore="${esc(d.id)}">恢复</button>`
      : `<button class="btn small danger" data-withdraw="${esc(d.id)}">撤回</button>`;
    return `<article class="doc-row ${d.withdrawn_at ? 'withdrawn' : ''}">
      <div>
        <div class="doc-title">${esc(d.title)} ${tags.join(' ')}</div>
        <div class="doc-meta mono">${esc(d.id)} · ${fmtInt(d.char_count)} 字符 · ${fmtInt(d.token_count)} 词元 · ${fmtInt(d.phrase_count)} 词组 · 哈希 ${esc(d.content_hash.slice(0, 12))}…</div>
        <div class="doc-preview">${esc(d.text_preview || '（空）')}</div>
      </div>
      <div class="doc-actions">
        <button class="btn small" data-text="${esc(d.id)}">查看原文</button>
        <button class="btn small" data-pairs="${esc(d.id)}">相似对</button>
        ${action}
      </div>
    </article>`;
  }).join('');

  $$('[data-withdraw]', host).forEach(btn => btn.addEventListener('click', () => withdrawDoc(btn.dataset.withdraw)));
  $$('[data-restore]', host).forEach(btn => btn.addEventListener('click', () => restoreDoc(btn.dataset.restore)));
  $$('[data-text]', host).forEach(btn => btn.addEventListener('click', () => showText(btn.dataset.text)));
  $$('[data-pairs]', host).forEach(btn => btn.addEventListener('click', () => {
    State.filters.doc = btn.dataset.pairs;
    navigate('pairs');
  }));
}

async function withdrawDoc(docId) {
  const doc = State.docs.find(d => d.id === docId);
  showDialog({
    title: '撤回文档？',
    bodyHtml: `<p>将撤回 <strong>${esc(doc ? doc.title : docId)}</strong>。原文保留，可恢复。</p>
      <p class="muted small">撤回后重新计算受影响的组：连通分量可能拆分、收缩或解散。相似边本身作为历史证据保留。</p>`,
    actions: [
      { label: '取消', onClick: closeDialog },
      {
        label: '撤回并重算分组', primary: true, onClick: async () => {
          try {
            const res = await api(`/api/documents/${encodeURIComponent(docId)}/withdraw`, {
              method: 'POST', body: { reason: 'manual withdraw' },
            });
            closeDialog();
            const rb = res.group_rebuild;
            toast(rb ? `已撤回；重建 ${rb.groups} 组，拆分 ${rb.splits.length} 处` : '已撤回');
            render();
          } catch (err) { toast(err.message, true); }
        },
      },
    ],
  });
}

async function restoreDoc(docId) {
  try {
    const res = await api(`/api/documents/${encodeURIComponent(docId)}/restore`, {
      method: 'POST', body: {},
    });
    toast('已恢复' + (res.group_rebuild ? `；重建 ${res.group_rebuild.groups} 组` : ''));
    render();
  } catch (err) { toast(err.message, true); }
}

async function showText(docId) {
  try {
    const doc = await api(`/api/documents/${encodeURIComponent(docId)}`);
    showDialog({
      title: doc.title,
      bodyHtml: `<div class="mono small muted" style="margin-bottom:6px">${esc(doc.id)} · ${fmtInt(doc.char_count)} 字符
        ${doc.revision ? ` · 词组 ${fmtInt(doc.revision.phrase_count)}（n=${doc.revision.phrase_n}，规则 ${esc(doc.revision.rules_fp)}）` : ''}</div>
        <div class="pane-body" style="max-height:52vh;border:1px solid var(--border);border-radius:8px">${esc(doc.text || '（空文档）')}</div>`,
      actions: [{ label: '关闭', onClick: closeDialog }],
      wide: true,
    });
  } catch (err) { toast(err.message, true); }
}

/* ------------------------------------------------------------------ pairs */

async function renderPairs(main) {
  await loadBasics();
  if (!State.task) {
    main.innerHTML = notice('warn', '!', '尚未建立索引任务。请先在总览页创建任务。');
    return;
  }
  const task = State.task;
  const q = new URLSearchParams();
  q.set('limit', '1000');
  if (State.filters.minScore) q.set('min_score', String(State.filters.minScore));
  if (State.filters.decision) q.set('decision', State.filters.decision);
  if (State.filters.doc) q.set('doc', State.filters.doc);
  const data = await api(`/api/tasks/${encodeURIComponent(task.id)}/edges?${q}`);
  State.edgeList = data.edges;

  const docFilter = State.filters.doc
    ? `<span class="pill">仅看文档 ${esc(shortId(State.filters.doc))}
         <button class="btn ghost small" id="clear-doc">清除</button></span>` : '';

  main.innerHTML = `
    <section class="section">
      <div class="section-head">
        <div><h2>相似对</h2>
          <p>每行是一对文档的精确 Jaccard（词组集合交集 / 并集）。候选生成只决定“要不要精确算”，不决定分数。</p></div>
        ${modeBadge(task)}
      </div>
      ${task.mode === 'approx' ? notice('warn', '≈',
        `<strong>这是近似模式。</strong> 候选由 MinHash/LSH 生成，可能漏掉达到阈值的文档对；
         表中分数是精确重算的，但列表<strong>不能声称找全</strong>。需要保证完整性请用精确模式。`) : ''}
      <div class="card tight" style="margin-bottom:14px">
        <div class="row">
          <label class="field" style="flex:0 0 220px"><span>最低分数</span>
            <input class="input" id="f-min" type="number" min="0" max="1" step="0.01" value="${State.filters.minScore}">
          </label>
          <label class="field" style="flex:0 0 180px"><span>审阅状态</span>
            <select class="select" id="f-decision">
              <option value="">全部</option>
              <option value="unreviewed" ${State.filters.decision === 'unreviewed' ? 'selected' : ''}>未审阅</option>
              <option value="same_group" ${State.filters.decision === 'same_group' ? 'selected' : ''}>关联到同组</option>
              <option value="keep_separate" ${State.filters.decision === 'keep_separate' ? 'selected' : ''}>保留为独立</option>
            </select>
          </label>
          ${docFilter}
          <span class="spacer"></span>
          <span class="muted small">显示 ${fmtInt(data.edges.length)} / ${fmtInt(data.total)} 对 · 阈值 ${fmtScore(task.threshold)}</span>
        </div>
      </div>
      <div class="table-wrap" id="edge-table"></div>
    </section>`;

  const min = $('#f-min');
  min.addEventListener('change', () => {
    State.filters.minScore = Math.max(0, Math.min(1, Number(min.value) || 0));
    render();
  });
  $('#f-decision').addEventListener('change', ev => {
    State.filters.decision = ev.target.value;
    render();
  });
  const clear = $('#clear-doc');
  if (clear) clear.addEventListener('click', () => { State.filters.doc = ''; render(); });

  paintEdgeTable();
}

function paintEdgeTable() {
  const host = $('#edge-table');
  const edges = State.edgeList;
  if (!edges.length) {
    host.innerHTML = `<div class="empty-state">
      ${State.filters.minScore || State.filters.decision || State.filters.doc
        ? '当前筛选下没有相似对。' : '没有达到阈值的文档对。可尝试降低阈值或重建任务。'}
    </div>`;
    return;
  }
  host.innerHTML = `<table>
    <thead><tr>
      <th>文档 A</th><th>文档 B</th><th class="num">Jaccard</th>
      <th class="num">交集/并集</th><th>候选来源</th><th>审阅</th><th></th>
    </tr></thead>
    <tbody>${edges.map(e => `
      <tr>
        <td><div class="trunc" title="${esc(e.doc_a_title)}">${esc(e.doc_a_title)}</div>
            <div class="mono small muted">${esc(shortId(e.doc_a))}${e.doc_a_withdrawn ? ' · 已撤回' : ''}</div></td>
        <td><div class="trunc" title="${esc(e.doc_b_title)}">${esc(e.doc_b_title)}</div>
            <div class="mono small muted">${esc(shortId(e.doc_b))}${e.doc_b_withdrawn ? ' · 已撤回' : ''}</div></td>
        <td class="num">${meter(e.jaccard, State.task.threshold)}</td>
        <td class="num">${fmtInt(e.intersection)} / ${fmtInt(e.union_size)}</td>
        <td class="small muted mono">${esc(e.candidate_reason || e.provenance)}</td>
        <td>${decisionBadge(e.decision)}</td>
        <td class="num nowrap">
          <a class="btn small" href="#/compare/${e.edge_id}">并排查看</a>
          <button class="btn small" data-quick="${e.edge_id}" data-dec="keep_separate">独立</button>
          <button class="btn small" data-quick="${e.edge_id}" data-dec="same_group">同组</button>
        </td>
      </tr>`).join('')}
    </tbody></table>`;
  $$('[data-quick]', host).forEach(btn => btn.addEventListener('click', async () => {
    try {
      await api(`/api/edges/${btn.dataset.quick}/decision`, {
        method: 'POST', body: { decision: btn.dataset.dec },
      });
      toast(btn.dataset.dec === 'same_group' ? '已标记为同组' : '已标记为独立');
      render();
    } catch (err) { toast(err.message, true); }
  }));
}

/* ---------------------------------------------------------------- compare */

async function renderCompare(main, edgeId) {
  if (!edgeId) { navigate('pairs'); return; }
  const data = await api(`/api/edges/${encodeURIComponent(edgeId)}`);
  State.edge = data;
  const edge = data.edge;
  const task = data.task;
  const segs = data.alignment.segments;
  State.compareSelection = { seg: null, phraseKey: null };

  const panes = `
    <div class="compare" id="panes">
      ${paneHtml('A', data.doc_a, 'a')}
      ${paneHtml('B', data.doc_b, 'b')}
    </div>`;

  main.innerHTML = `
    <section class="section">
      <div class="compare-toolbar">
        <div>
          <h2 style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
            并排查看
            ${data.edge.provenance === 'approx-candidate' ? badge('近似候选', 'warn', '≈') : ''}
            ${decisionBadge(edge.decision)}
          </h2>
          <p class="muted small" style="margin-top:3px">
            边 <span class="mono">#${edge.edge_id}</span> · 规则 <span class="mono">${esc(edge_task_rules(data))}</span>
          </p>
        </div>
        <div class="row">
          <a class="btn" href="#/pairs">← 返回列表</a>
          <button class="btn" id="dec-independent">保留为独立文档</button>
          <button class="btn primary" id="dec-same">关联到同一近重复组</button>
        </div>
      </div>

      <div class="grid-3" style="margin-bottom:14px">
        <div class="kpi">
          <span class="label">精确 Jaccard</span>
          <span class="value">${fmtScore(edge.jaccard)}</span>
          <span class="hint">阈值 ${fmtScore(task.threshold)} · ${edge.above_threshold === false ? '低于阈值' : '达到阈值'}</span>
        </div>
        <div class="kpi">
          <span class="label">词组交集 / 并集</span>
          <span class="value">${fmtInt(edge.intersection)}<span class="muted"> / </span>${fmtInt(edge.union_size)}</span>
          <span class="hint">A ${fmtInt(data.phrase_counts.a)} 词组 · B ${fmtInt(data.phrase_counts.b)} 词组</span>
        </div>
        <div class="kpi">
          <span class="label">同组关系</span>
          <span class="value" style="font-size:16px;line-height:1.5;margin-top:5px">
            ${data.group_context.groups_shared.length ? esc(data.group_context.groups_shared.map(g => g.label).join('、')) : '不在同一组'}
          </span>
          <span class="hint">${esc(data.group_context.note.split('；')[0])}</span>
        </div>
      </div>

      ${task.approximate ? notice('warn', '≈',
        `<strong>近似模式任务。</strong> 该边的分数是精确计算的，但“没有列出的文档对”不等于不相似——候选生成可能漏召。`) : ''}

      ${data.doc_a.withdrawn_at || data.doc_b.withdrawn_at ? notice('warn', '↩',
        `其中一篇文档已撤回。边仍作为历史证据保留；分组时会排除已撤回文档。`) : ''}

      ${panes}

      <div class="grid-2" style="margin-top:16px">
        <div class="card tight">
          <div class="row" style="justify-content:space-between;margin-bottom:8px">
            <h3>相同片段（按词元对齐）</h3>
            <span class="muted small">${fmtInt(segs.length)} 段</span>
          </div>
          <p class="muted small" style="margin-bottom:8px">
            对齐是逐词元的相同连续区段，跨过很短的差异会合并。点选一段，两侧同时定位并高亮。
          </p>
          <div class="seg-list" id="seg-list"></div>
        </div>
        <div class="stack">
          <div class="card tight">
            <h3>共有词组（${fmtInt(data.shared_phrase_total)}）</h3>
            <p class="sub">n=${task && data.rules ? (data.shared_phrases.length ? (data.rules.phrase_n) : data.rules.phrase_n) : '—'} 的连续词元。点击可在两侧高亮首次出现位置。</p>
            <div class="phrase-cloud" id="shared-phrases"></div>
          </div>
          <div class="card tight">
            <h3>独立复核</h3>
            <p class="sub">从原文重新分词与切片，不使用已存索引；用于核对分数与长度/前缀过滤。</p>
            <div id="independent-check"></div>
          </div>
          <div class="card tight">
            <h3>差异词组</h3>
            <div class="small secondary" style="margin-top:6px">仅 A 有 ${fmtInt(data.only_in_a.total)} 个</div>
            <div class="phrase-cloud" style="margin:5px 0 10px">${data.only_in_a.sample.slice(0, 18).map(p => `<span class="phrase-chip">${esc(p)}</span>`).join('') || '<span class="muted small">无</span>'}</div>
            <div class="small secondary">仅 B 有 ${fmtInt(data.only_in_b.total)} 个</div>
            <div class="phrase-cloud" style="margin-top:5px">${data.only_in_b.sample.slice(0, 18).map(p => `<span class="phrase-chip">${esc(p)}</span>`).join('') || '<span class="muted small">无</span>'}</div>
          </div>
        </div>
      </div>
    </section>`;

  $('#dec-independent').addEventListener('click', () => decideEdge(edge.edge_id, 'keep_separate'));
  $('#dec-same').addEventListener('click', () => decideEdge(edge.edge_id, 'same_group'));
  paintSegments();
  paintSharedPhrases();
  paintIndependentCheck(data.independent_check);
  paintPanes();

  if (State.compareSelection.seg == null && segs.length && segs[0].a) {
    selectSegment(0);
  }
}

function edge_task_rules(data) {
  const r = State.task && State.task.rules;
  return r ? `${r.tokenizer.version} / ${r.phrases.version} / n=${r.phrases.n}` : (data.edge.provenance || '');
}

function paneHtml(label, doc, side) {
  const withdrawn = doc.withdrawn_at ? badge('已撤回', 'muted', '↩') : '';
  return `<article class="pane" data-side="${side}">
    <header class="pane-head">
      <h3 title="${esc(doc.title)}">${label} · ${esc(doc.title)}</h3>
      <span class="row" style="gap:6px">${withdrawn}
        <span class="mono small muted">${esc(shortId(doc.id))}</span></span>
    </header>
    <div class="pane-body" id="pane-${side}"></div>
    <footer class="pane-foot">${fmtInt(doc.char_count)} 字符 · ${fmtInt(doc.token_count)} 词元 · ${fmtInt(doc.phrase_count)} 词组</footer>
  </article>`;
}

function paintPanes() {
  const data = State.edge;
  if (!data) return;
  const sel = State.compareSelection;
  const segs = data.alignment.segments;
  for (const side of ['a', 'b']) {
    const doc = side === 'a' ? data.doc_a : data.doc_b;
    const ranges = [];
    segs.forEach((s, i) => {
      const part = s[side];
      if (!part) return;
      const classes = ['shared'];
      if (sel.seg === i) classes.push('active');
      ranges.push({ start: part.start, end: part.end, classes, seg: i });
    });
    if (!data.alignment.available) {
      $('#pane-' + side).textContent = doc.text || '（空文档）';
      continue;
    }
    $('#pane-' + side).innerHTML = paintRanges(doc.text || '', ranges) || '<span class="muted">（空文档）</span>';
  }
  $$('.pane-body mark[data-seg]').forEach(mark => {
    mark.addEventListener('click', () => selectSegment(Number(mark.dataset.seg)));
  });
}

function paintSegments() {
  const host = $('#seg-list');
  const data = State.edge;
  const segs = data.alignment.segments;
  if (!data.alignment.available) {
    host.innerHTML = notice('warn', '!', esc(data.alignment.reason || '无法对齐'));
    return;
  }
  if (!segs.length) {
    host.innerHTML = `<div class="empty-state">没有逐词元相同的连续片段。分数来自词组集合的重叠（顺序可能不同），请看右侧共有词组。</div>`;
    return;
  }
  host.innerHTML = segs.map((s, i) => `
    <button class="seg ${State.compareSelection.seg === i ? 'active' : ''}" data-seg="${i}">
      <span class="seg-meta">段 ${i + 1} · ${fmtInt(s.a.token_count)} 词元 · 含 ${fmtInt(s.shared_phrase_count)} 个共有词组
        ${s.is_full_text ? badge('全文一致', 'ok', '=') : ''}</span>
      <span class="quote-a">A: ${esc(truncate(s.a.text.replace(/\s+/g, ' '), 110))}</span>
      <span class="quote-b">B: ${esc(truncate(s.b.text.replace(/\s+/g, ' '), 110))}</span>
    </button>`).join('');
  $$('.seg', host).forEach(btn => btn.addEventListener('click', () => selectSegment(Number(btn.dataset.seg))));
}

function selectSegment(index) {
  State.compareSelection.seg = index;
  $$('#seg-list .seg').forEach(el => el.classList.toggle('active', Number(el.dataset.seg) === index));
  $$('.pane-body').forEach(pane => {
    $$('mark.active', pane).forEach(m => m.classList.remove('active'));
    const mark = pane.querySelector(`mark[data-seg="${index}"]`);
    if (mark) {
      mark.classList.add('active');
      mark.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  });
}

function paintSharedPhrases() {
  const host = $('#shared-phrases');
  const data = State.edge;
  const phrases = data.shared_phrases;
  if (!phrases.length) {
    host.innerHTML = '<span class="muted small">没有共有词组（分数为 0 或文本为空）。</span>';
    return;
  }
  host.innerHTML = phrases.map(p => `
    <button class="phrase-chip" data-key="${esc(p.key)}" data-start-a="${p.span_a ? p.span_a.start : ''}"
      data-start-b="${p.span_b ? p.span_b.start : ''}"
      title="A 出现 ${p.count_a} 次 · B 出现 ${p.count_b} 次">${esc(p.display)}</button>`).join('');
  $$('.phrase-chip', host).forEach(chip => chip.addEventListener('click', () => {
    const key = chip.dataset.key;
    const p = phrases.find(x => x.key === key);
    if (!p) return;
    State.compareSelection.phraseKey = key;
    // Select the segment that contains this phrase in A (fallback: nearest).
    const segs = data.alignment.segments;
    let target = null;
    if (p.span_a) {
      target = segs.findIndex(s => s.a.start <= p.span_a.start && s.a.end >= p.span_a.end);
    }
    if (target == null || target < 0) {
      target = segs.findIndex(s => s.a.start >= (p.span_a ? p.span_a.start : 0));
    }
    if (target >= 0) selectSegment(target);
    toast(`词组「${p.display}」：A ${p.count_a} 次 · B ${p.count_b} 次`);
  }));
}

function paintIndependentCheck(check) {
  if (!check) { $('#independent-check').innerHTML = ''; return; }
  const match = Math.abs(check.jaccard - State.edge.edge.jaccard) < 1e-9;
  const rows = [
    ['重算 Jaccard', fmtScore2(check.jaccard)],
    ['与存储分值', match ? badge('一致', 'ok', '✓') : badge('不一致！', 'bad', '!')],
    ['交集 / 并集', `${fmtInt(check.intersection)} / ${fmtInt(check.union_size)}`],
    ['A 词组 · 前缀长度', `${fmtInt(check.phrase_count_a)} · ${fmtInt(check.prefix_length_a)}`],
    ['B 词组 · 前缀长度', `${fmtInt(check.phrase_count_b)} · ${fmtInt(check.prefix_length_b)}`],
    ['长度过滤', check.length_filter_would_admit ? '可通过（不会被长度剪枝）' : '会被长度剪枝'],
    ['完全相同文本', check.identical_text ? '是（哈希一致，分值为 1）' : '否'],
  ];
  $('#independent-check').innerHTML = `<dl class="kv">${rows.map(([k, v]) =>
    `<dt>${esc(k)}</dt><dd>${typeof v === 'string' && v.startsWith('<') ? v : esc(v)}</dd>`).join('')}</dl>`;
}

async function decideEdge(edgeId, decision) {
  try {
    const res = await api(`/api/edges/${edgeId}/decision`, { method: 'POST', body: { decision } });
    const rb = res.group_rebuild;
    toast((decision === 'same_group' ? '已关联到同组' : '已保留为独立文档') +
      (rb ? `；重建 ${rb.groups} 组，拆分 ${rb.splits.length} 处` : ''));
    render();
  } catch (err) { toast(err.message, true); }
}

/* ----------------------------------------------------------------- groups */

async function renderGroups(main) {
  await loadBasics();
  if (!State.task) {
    main.innerHTML = notice('warn', '!', '尚未建立索引任务。请先在总览页创建任务。');
    return;
  }
  const data = await api(`/api/tasks/${encodeURIComponent(State.task.id)}/groups`);
  State.groupsData = data;
  const runs = data.runs || [];
  const latest = runs[0];
  const splits = latest ? latest.splits.splits || [] : [];
  const dissolved = latest ? latest.splits.dissolved || [] : [];

  main.innerHTML = `
    <section class="section">
      <div class="section-head">
        <div><h2>分组</h2>
          <p>组 = 接受边的连通分量。边是唯一事实；组只用于导航。删除/撤回文档或把某条边标为独立后，组会重建，可能拆成多个组。</p></div>
        <button class="btn" id="rebuild-groups">重新计算分组</button>
      </div>

      ${notice('info', 'ℹ',
        `<strong>组内不保证两两相似。</strong> 若 A~B、B~C 而 A 与 C 低于阈值，三篇仍可能在同一连通分量里。
         每个组都列出全部成员对：有直接边的显示分值，没有的明确标注“无直接边”。`)}

      ${splits.length ? notice('warn', '⇄',
        `<strong>上次重建发生了拆分：</strong>` +
        splits.map(s => `旧组 ${esc(shortId(s.old_group))}（${fmtInt(s.members.length)} 篇）拆为 ${fmtInt(s.new_groups.length)} 个组`).join('；')) : ''}
      ${dissolved.length ? notice('warn', '⊘',
        `<strong>上次重建有组解散：</strong>` +
        dissolved.map(d => `旧组 ${esc(shortId(d.old_group))}（${fmtInt(d.members.length)} 篇）`).join('；')) : ''}

      <div class="stack" id="group-list" style="margin-top:14px"></div>
    </section>`;

  $('#rebuild-groups').addEventListener('click', async () => {
    try {
      const report = await api(`/api/tasks/${encodeURIComponent(State.task.id)}/groups/rebuild`, {
        method: 'POST', body: {},
      });
      toast(`已重建：${report.groups} 组，拆分 ${report.splits.length} 处`);
      render();
    } catch (err) { toast(err.message, true); }
  });

  const host = $('#group-list');
  if (!data.groups.length) {
    host.innerHTML = `<div class="empty-state">当前没有含 2 篇及以上文档的组。</div>`;
    return;
  }
  host.innerHTML = data.groups.map((g, idx) => groupCardHtml(g, idx)).join('');
  data.groups.forEach(group => bindGroupCard(group));
}

function groupCardHtml(group, idx) {
  const members = group.members;
  const pairs = group.pairs;
  const missing = pairs.filter(p => !p.edge).length;
  return `<article class="card group-card" data-group="${esc(group.group.id)}">
    <div class="row" style="justify-content:space-between">
      <div>
        <h3>组 ${idx + 1} · ${fmtInt(members.length)} 篇</h3>
        <p class="muted small">${fmtInt(pairs.length)} 个成员对 · ${fmtInt(pairs.length - missing)} 对有直接边 · ${fmtInt(missing)} 对无直接边</p>
      </div>
      <div class="row">
        ${badge('连通分量 · ' + (group.group.algorithm.split(':')[1] || 'accepted'), 'muted')}
        <span class="mono small muted">${esc(shortId(group.group.id))}</span>
      </div>
    </div>
    <div id="diagram-${esc(group.group.id)}"></div>
    <div class="pair-chip-row">
      ${pairs.map(p => {
        const a = members.find(m => m.id === p.doc_a) || { title: p.doc_a };
        const b = members.find(m => m.id === p.doc_b) || { title: p.doc_b };
        if (p.edge) {
          return `<a class="pair-chip has-edge" href="#/compare/${p.edge.edge_id}"
            title="${esc(a.title)} ↔ ${esc(b.title)}">${esc(truncate(a.title, 14))} ↔ ${esc(truncate(b.title, 14))} · ${fmtScore(p.edge.jaccard)}</a>`;
        }
        return `<span class="pair-chip" title="${esc(a.title)} 与 ${esc(b.title)} 没有达到阈值的直接边">
          ${esc(truncate(a.title, 12))} × ${esc(truncate(b.title, 12))} · 无直接边</span>`;
      }).join('')}
    </div>
    ${(group.splits && (group.splits.splits || []).length) ? notice('warn', '⇄',
      `<strong>这个任务的最近一次重建有过拆分。</strong> 旧组拆分后，成员可能落在不同组或成为独立文档。`) : ''}
    <details class="disclosure">
      <summary>成员与边（表格）</summary>
      <div class="table-wrap">
        <table>
          <thead><tr><th>文档</th><th class="num">词元</th><th class="num">词组</th><th>哈希</th><th></th></tr></thead>
          <tbody>${members.map(m => `<tr>
            <td>${esc(m.title)} ${m.withdrawn_at ? badge('已撤回', 'muted', '↩') : ''}</td>
            <td class="num">${fmtInt(m.token_count)}</td>
            <td class="num">${fmtInt(m.phrase_count)}</td>
            <td class="mono small">${esc(m.content_hash.slice(0, 12))}…</td>
            <td class="num"><button class="btn small" data-doc-text="${esc(m.id)}">原文</button></td>
          </tr>`).join('')}</tbody>
        </table>
      </div>
      <div class="table-wrap" style="margin-top:10px">
        <table>
          <thead><tr><th>边</th><th class="num">Jaccard</th><th>决定</th><th>来源</th><th></th></tr></thead>
          <tbody>${group.edges.length ? group.edges.map(e => `<tr>
            <td class="mono small">${esc(shortId(e.doc_a))} ↔ ${esc(shortId(e.doc_b))}</td>
            <td class="num">${meter(e.jaccard, State.task.threshold)}</td>
            <td>${decisionBadge(e.decision)}</td>
            <td class="small muted mono">${esc(e.candidate_reason || e.provenance)}</td>
            <td class="num"><a class="btn small" href="#/compare/${e.edge_id}">并排查看</a></td>
          </tr>`).join('') : '<tr><td colspan="5" class="muted">组内没有直接边（不应发生）</td></tr>'}</tbody>
        </table>
      </div>
    </details>
  </article>`;
}

function bindGroupCard(group) {
  const host = document.querySelector(`[data-group="${group.group.id}"]`);
  if (!host) return;
  const diag = host.querySelector(`#diagram-${CSS.escape(group.group.id)}`);
  if (diag) diag.innerHTML = edgeDiagram(group);
  host.querySelectorAll('[data-doc-text]').forEach(btn => {
    btn.addEventListener('click', () => showText(btn.dataset.docText));
  });
}

/**
 * Truthful edge diagram: solid line per edge (width by score, label = score),
 * dashed gray line per pair WITHOUT an edge. No implied cliques.
 */
function edgeDiagram(group) {
  const members = group.members;
  const n = members.length;
  if (n > 9) {
    return `<p class="muted small">成员 ${n} 篇，图略；请使用下方成员对与边表格查看完整关系。</p>`;
  }
  const W = 660, H = 220;
  const cx = W / 2, cy = 108;
  const R = n <= 2 ? 150 : Math.min(160, 44 * n);
  const pos = {};
  members.forEach((m, i) => {
    if (n === 1) { pos[m.id] = { x: cx, y: cy }; return; }
    if (n === 2) {
      pos[m.id] = { x: i === 0 ? cx - R : cx + R, y: cy };
      return;
    }
    const angle = -Math.PI / 2 + (2 * Math.PI * i) / n;
    pos[m.id] = { x: cx + R * Math.cos(angle), y: cy + R * Math.sin(angle) };
  });
  const edges = new Map(group.pairs.filter(p => p.edge).map(p => [p.doc_a + '|' + p.doc_b, p.edge]));
  const scores = Array.from(edges.values()).map(e => e.jaccard);
  const minS = scores.length ? Math.min(...scores) : 0;
  const maxS = scores.length ? Math.max(...scores) : 0;
  const labelPairs = new Set();
  if (scores.length && n > 3) {
    // label selectively: lowest and highest scoring edge
    group.pairs.forEach(p => { if (p.edge && (p.edge.jaccard === minS || p.edge.jaccard === maxS)) labelPairs.add(p.doc_a + '|' + p.doc_b); });
  }
  let svg = `<svg class="edge-diagram" viewBox="0 0 ${W} ${H}" role="img"
      aria-label="组内 ${n} 篇文档与 ${edges.size} 条直接边的关系图">
    <title>组内文档与直接相似边（实线为有边，虚线为无直接边）</title>`;
  // links first
  for (const p of group.pairs) {
    const a = pos[p.doc_a], b = pos[p.doc_b];
    if (!a || !b) continue;
    if (p.edge) {
      const w = 1.2 + 3.2 * p.edge.jaccard;
      svg += `<line class="link" x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}" stroke-width="${w.toFixed(2)}"/>`;
      const shouldLabel = n <= 3 || labelPairs.has(p.doc_a + '|' + p.doc_b);
      if (shouldLabel) {
        svg += `<text class="edge-label" x="${((a.x + b.x) / 2).toFixed(1)}" y="${((a.y + b.y) / 2 - 5).toFixed(1)}"
          text-anchor="middle">${fmtScore(p.edge.jaccard)}</text>`;
      }
    } else {
      svg += `<line class="link missing" x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}" stroke-width="1"/>`;
      svg += `<text class="edge-label" x="${((a.x + b.x) / 2).toFixed(1)}" y="${((a.y + b.y) / 2 - 4).toFixed(1)}"
        text-anchor="middle">无直接边</text>`;
    }
  }
  // nodes
  members.forEach(m => {
    const p = pos[m.id];
    if (!p) return;
    svg += `<circle class="node" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="14"/>`;
    svg += `<text class="node-label" x="${p.x.toFixed(1)}" y="${(p.y + 28).toFixed(1)}" text-anchor="middle">${esc(truncate(m.title, 16))}</text>`;
    svg += `<text class="node-label" x="${p.x.toFixed(1)}" y="${(p.y + 41).toFixed(1)}" text-anchor="middle"
      style="fill:var(--text-muted);font-size:10px">${esc(shortId(m.id))}</text>`;
  });
  svg += `</svg>
    <p class="highlight-hint" style="margin-top:2px">
      <span style="display:inline-block;width:22px;height:2px;background:var(--series-1);vertical-align:middle"></span> 有直接边（线宽 ∝ 分值）
      &nbsp;·&nbsp;
      <span style="display:inline-block;width:22px;height:0;border-top:1px dashed var(--baseline);vertical-align:middle"></span> 无直接边
    </p>`;
  return svg;
}

/* ------------------------------------------------------------------- task */

async function renderTask(main) {
  await loadBasics();
  const data = await api('/api/tasks');
  State.tasks = data.tasks;
  const current = State.task;

  main.innerHTML = `
    <section class="section">
      <div class="section-head">
        <div><h2>任务与校验</h2>
          <p>任务可中断并可继续：已提交的文档版本保留在库中，再次运行会从断点续做。</p></div>
        ${modeBadge(current)}
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>任务</th><th>状态</th><th>模式</th><th class="num">阈值</th>
            <th class="num">进度</th><th class="num">边</th><th>规则指纹</th><th></th>
          </tr></thead>
          <tbody>${State.tasks.map(t => `<tr>
            <td class="mono small">${esc(t.id)}<div class="muted">${esc(t.note || '')}</div></td>
            <td>${statusBadge(t.status)}</td>
            <td>${t.mode === 'approx' ? badge('近似', 'warn', '≈') : badge('精确', 'ok', '✓')}</td>
            <td class="num">${fmtScore(t.threshold)}</td>
            <td class="num">${fmtInt(t.done)} / ${fmtInt(t.total)}</td>
            <td class="num">${fmtInt(t.edge_count)}</td>
            <td class="mono small">${esc(t.rules_fp)}</td>
            <td class="num nowrap">
              ${['running', 'queued'].includes(t.status)
                ? `<button class="btn small" data-stop="${esc(t.id)}">停止</button>`
                : `<button class="btn small" data-run="${esc(t.id)}">${t.status === 'stopped' ? '继续' : '重跑'}</button>`}
              <button class="btn small" data-verify="${esc(t.id)}">校验</button>
            </td>
          </tr>`).join('') || '<tr><td colspan="8" class="muted">还没有任务。</td></tr>'}
          </tbody>
        </table>
      </div>
    </section>

    ${current ? taskDetailHtml(current) : ''}
    <section class="section" id="verify-section"></section>
  `;

  $$('[data-run]').forEach(btn => btn.addEventListener('click', async () => {
    try {
      const r = await api(`/api/tasks/${encodeURIComponent(btn.dataset.run)}/run`, { method: 'POST', body: {} });
      if (r.started === false) toast('任务已在运行');
      else toast('任务已启动' + (btn.dataset.run === (current && current.id) ? '' : ''));
      render();
    } catch (err) { toast(err.message, true); }
  }));
  $$('[data-stop]').forEach(btn => btn.addEventListener('click', async () => {
    try {
      await api(`/api/tasks/${encodeURIComponent(btn.dataset.stop)}/stop`, { method: 'POST', body: {} });
      toast('已请求停止；已完成的部分保留');
      render();
    } catch (err) { toast(err.message, true); }
  }));
  $$('[data-verify]').forEach(btn => btn.addEventListener('click', () => runVerify(btn.dataset.verify)));

  if (current) bindTaskDetail(current);
  if (State.tasks.some(t => t.status === 'running')) startPolling();
}

function taskDetailHtml(task) {
  const running = ['running', 'queued'].includes(task.status);
  const pct = task.total ? Math.round((task.done / task.total) * 100) : 0;
  return `<section class="section grid-2">
    <div class="card">
      <h3>当前任务</h3>
      <p class="sub mono">${esc(task.id)}</p>
      <dl class="kv" style="margin-bottom:12px">
        <dt>状态</dt><dd>${statusBadge(task.status)} ${task.progress ? `<span class="muted small">${esc(task.progress.phase)} · ${esc(task.progress.message || '')}</span>` : ''}</dd>
        <dt>模式</dt><dd>${task.mode === 'approx' ? badge('近似 · 可能漏召', 'warn', '≈') : badge('精确 · 无漏召', 'ok', '✓')}</dd>
        <dt>阈值</dt><dd>${fmtScore(task.threshold)}</dd>
        <dt>规则集</dt><dd class="mono small">${esc(task.rules_fp)}</dd>
        <dt>词项排序摘要</dt><dd class="mono small">${esc(task.vocab_digest || '—')}</dd>
        <dt>快照</dt><dd class="small">${task.snapshot ? `${fmtInt(Object.keys(task.snapshot).length)} 篇（记录内容哈希）` : '—'}</dd>
        <dt>边 / 未审阅</dt><dd>${fmtInt(task.edge_count)} / ${fmtInt(task.unreviewed_edges)}</dd>
        <dt>备注</dt><dd class="small">${esc(task.note || '—')}</dd>
      </dl>
      <div class="progress-track" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"
           aria-label="任务进度"><div class="progress-fill" style="width:${pct}%"></div></div>
      <div class="row" style="margin-top:12px">
        ${running
          ? `<button class="btn" data-detail-stop="${esc(task.id)}">停止</button>`
          : `<button class="btn primary" data-detail-run="${esc(task.id)}">${task.status === 'stopped' ? '从断点继续' : '重新运行'}</button>`}
        <button class="btn" data-detail-verify="${esc(task.id)}">全量两两校验</button>
      </div>
      <hr class="hr">
      <h4>完成度声明</h4>
      <p class="small secondary" style="margin-top:6px">${esc(task.completeness)}</p>
    </div>
    <div class="card">
      <h3>导出</h3>
      <p class="sub">导出入湖清单、关联边与分组；组以“组成员 + 组内全部成员对”导出，不展开为团。</p>
      <div class="stack" style="gap:8px">
        <a class="btn" href="/api/tasks/${encodeURIComponent(task.id)}/export?format=bundle">下载完整包（zip：manifest/edges/groups/documents/rules）</a>
        <a class="btn" href="/api/tasks/${encodeURIComponent(task.id)}/export?format=manifest" target="_blank" rel="noopener">查看入湖清单（manifest.json）</a>
        <a class="btn" href="/api/tasks/${encodeURIComponent(task.id)}/export?format=edges.csv">下载关联边（edges.csv）</a>
        <a class="btn" href="/api/tasks/${encodeURIComponent(task.id)}/export?format=links.nq">下载关联边（links.nq）</a>
      </div>
      <hr class="hr">
      <h4>规则说明</h4>
      <dl class="kv" style="margin-top:6px">
        <dt>分词器</dt><dd class="mono small">${esc(task.rules.tokenizer.version)}</dd>
        <dt>分词规则</dt><dd class="small">${esc(task.rules.tokenizer.description)}</dd>
        <dt>词组切片</dt><dd class="mono small">${esc(task.rules.phrases.version)} · n=${task.rules.phrases.n}</dd>
        <dt>切片规则</dt><dd class="small">${esc(task.rules.phrases.description)}</dd>
        <dt>全局词项排序</dt><dd class="small">${esc(task.rules.vocabulary.description)}</dd>
      </dl>
    </div>
  </section>`;
}

function bindTaskDetail(task) {
  const run = document.querySelector('[data-detail-run]');
  if (run) run.addEventListener('click', async () => {
    try {
      await api(`/api/tasks/${encodeURIComponent(task.id)}/run`, { method: 'POST', body: {} });
      toast('任务已启动，将从已提交的文档版本继续');
      render();
    } catch (err) { toast(err.message, true); }
  });
  const stop = document.querySelector('[data-detail-stop]');
  if (stop) stop.addEventListener('click', async () => {
    try {
      await api(`/api/tasks/${encodeURIComponent(task.id)}/stop`, { method: 'POST', body: {} });
      toast('已请求停止');
      render();
    } catch (err) { toast(err.message, true); }
  });
  const verify = document.querySelector('[data-detail-verify]');
  if (verify) verify.addEventListener('click', () => runVerify(task.id));
}

async function runVerify(taskId, full = false) {
  const host = $('#verify-section');
  host.innerHTML = `<div class="card"><p class="muted">正在做全量两两比较（不使用候选索引）…</p></div>`;
  try {
    const v = await api(`/api/tasks/${encodeURIComponent(taskId)}/verify`, { method: 'POST', body: { full } });
    const sampled = v.scope === 'sample';
    const verdict = v.verdict === 'pass'
      ? notice('good', '✓', `<strong>校验通过。</strong> ${esc(v.completeness_claim)}`)
      : v.verdict === 'approx-incomplete'
        ? notice('warn', '≈', `<strong>近似模式：发现漏召 ${fmtInt(v.missing_total)} 对。</strong> ${esc(v.completeness_claim)}`)
        : notice('bad', '!', `<strong>校验未通过。</strong> 缺少 ${fmtInt(v.missing_total)} 对，多余 ${fmtInt(v.extra_total)} 对，分数不一致 ${fmtInt(v.score_mismatch_total)} 对。`);
    const scopeNote = sampled
      ? `<div class="card" style="margin-top:12px"><p class="muted small">该任务有 ${fmtInt(v.documents_total)} 篇文档，本次按任务哈希确定性地抽取 ${fmtInt(v.documents)} 篇：样本内两两全量比较，样本外的文档对未校验。</p>
         <button class="btn small" id="verify-full">对全部 ${fmtInt(v.documents_total)} 篇做全量校验（${fmtInt(v.documents_total * (v.documents_total - 1) / 2)} 对）</button></div>`
      : '';
    host.innerHTML = `
      <div class="section-head"><h3>全量两两校验</h3>
        <span class="muted small">任务 <span class="mono">${esc(v.task_id)}</span> · ${sampled ? '确定性样本' : '全量范围'}</span></div>
      ${verdict}
      <div class="kpi-grid" style="margin-top:12px">
        <div class="kpi"><span class="label">比较的文档对</span><span class="value">${fmtInt(v.pairs_compared)}</span><span class="hint">${fmtInt(v.documents)} 篇文档${sampled ? ` / 共 ${fmtInt(v.documents_total)} 篇` : ''}</span></div>
        <div class="kpi"><span class="label">真值中达到阈值</span><span class="value">${fmtInt(v.pairs_above_threshold)}</span><span class="hint">阈值 ${fmtScore(v.threshold)}</span></div>
        <div class="kpi"><span class="label">已存边</span><span class="value">${fmtInt(v.stored_edges)}</span><span class="hint">${v.mode === 'approx' ? '近似模式' : '精确模式'}</span></div>
        <div class="kpi"><span class="label">漏召 / 多余 / 分数不符</span>
          <span class="value">${fmtInt(v.missing_total)} / ${fmtInt(v.extra_total)} / ${fmtInt(v.score_mismatch_total)}</span>
          <span class="hint">空文档 ${fmtInt(v.empty_documents.length)} 篇</span></div>
      </div>
      ${v.missing_total ? `<div class="card" style="margin-top:12px"><h4>缺失的文档对（前 ${Math.min(200, v.missing_total)}）</h4>
        <div class="table-wrap" style="margin-top:8px"><table><thead><tr><th>A</th><th>B</th><th class="num">真值</th></tr></thead>
        <tbody>${v.missing.map(m => `<tr><td>${esc(m.title_a)}</td><td>${esc(m.title_b)}</td><td class="num">${fmtScore(m.jaccard)}</td></tr>`).join('')}</tbody></table></div></div>` : ''}
      ${v.extra_total ? `<div class="card" style="margin-top:12px"><h4>多余的边</h4>
        <div class="table-wrap" style="margin-top:8px"><table><thead><tr><th>边</th><th>A</th><th>B</th><th class="num">存储</th><th class="num">重算</th></tr></thead>
        <tbody>${v.extra.map(e => `<tr><td class="mono">#${e.edge_id}</td><td class="mono small">${esc(shortId(e.doc_a))}</td><td class="mono small">${esc(shortId(e.doc_b))}</td><td class="num">${fmtScore2(e.stored)}</td><td class="num">${e.recomputed == null ? '—' : fmtScore2(e.recomputed)}</td></tr>`).join('')}</tbody></table></div></div>` : ''}
      ${v.score_mismatch_total ? `<div class="card" style="margin-top:12px"><h4>分数不一致</h4>
        <div class="table-wrap" style="margin-top:8px"><table><thead><tr><th>边</th><th class="num">存储</th><th class="num">重算</th></tr></thead>
        <tbody>${v.score_mismatch.map(e => `<tr><td class="mono">#${e.edge_id}</td><td class="num">${fmtScore2(e.stored)}</td><td class="num">${fmtScore2(e.recomputed)}</td></tr>`).join('')}</tbody></table></div></div>` : ''}
    `;
    host.scrollIntoView({ behavior: 'smooth', block: 'start' });
    const fullBtn = $('#verify-full');
    if (fullBtn) fullBtn.addEventListener('click', () => runVerify(taskId, true));
  } catch (err) {
    host.innerHTML = notice('bad', '!', `<strong>校验失败。</strong> ${esc(err.message)}`);
  }
}

/* --------------------------------------------------------------- polling */

function stopPolling() {
  if (State.pollTimer) { clearInterval(State.pollTimer); State.pollTimer = null; }
}

function startPolling() {
  stopPolling();
  State.pollTimer = setInterval(async () => {
    try {
      const stats = await api('/api/stats');
      State.stats = stats;
      if (!stats.task) return;
      const wasRunning = State.task && State.task.status === 'running';
      State.task = stats.task;
      if (stats.task.status === 'running') {
        const p = stats.task.progress;
        const banner = p ? `${esc(p.phase)} · ${fmtInt(p.done)}/${fmtInt(p.total)} ${esc(p.message || '')}` : '运行中…';
        setBanner(notice('info', '▶', `<strong>索引任务运行中。</strong> ${banner}`));
        const bar = document.querySelector('.progress-fill');
        if (bar && stats.task.total) bar.style.width = Math.round((stats.task.done / stats.task.total) * 100) + '%';
      } else if (wasRunning) {
        setBanner('');
        toast(stats.task.status === 'completed' ? '索引任务完成' : '索引任务已停止/失败');
        render();
      }
    } catch (err) { /* transient; keep polling */ }
  }, 900);
}

/* ---------------------------------------------------------------- dialog */

function showDialog({ title, bodyHtml, actions, wide }) {
  closeDialog();
  const backdrop = document.createElement('div');
  backdrop.className = 'dialog-backdrop';
  backdrop.innerHTML = `<div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title"
      style="${wide ? 'max-width:820px' : ''}">
    <h3 id="dlg-title">${esc(title)}</h3>
    <div>${bodyHtml}</div>
    <div class="row" style="justify-content:flex-end" id="dlg-actions"></div>
  </div>`;
  document.body.appendChild(backdrop);
  const actionsHost = $('#dlg-actions', backdrop);
  actions.forEach(a => {
    const btn = document.createElement('button');
    btn.className = 'btn' + (a.primary ? ' primary' : '');
    btn.textContent = a.label;
    btn.addEventListener('click', a.onClick);
    actionsHost.appendChild(btn);
  });
  backdrop.addEventListener('click', ev => { if (ev.target === backdrop) closeDialog(); });
  document.addEventListener('keydown', onDialogKey);
  const first = actionsHost.querySelector('button');
  if (first) first.focus();
}

function onDialogKey(ev) {
  if (ev.key === 'Escape') closeDialog();
}

function closeDialog() {
  const el = $('.dialog-backdrop');
  if (el) el.remove();
  document.removeEventListener('keydown', onDialogKey);
}

/* ------------------------------------------------------------------- boot */

(async function boot() {
  try {
    await refreshHealth();
  } catch (err) {
    $('#health-pill').innerHTML = `<span class="dot bad"></span>服务不可用`;
  }
  if (!location.hash) location.hash = '#/overview';
  await render();
})();
