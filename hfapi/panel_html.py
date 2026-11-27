"""面板前端模板（纯静态 HTML + 原生 JS，无构建步骤）。

单独成文件只为让 panel.py 专注服务端逻辑。约束：
  · 所有动态内容走 textContent，绝不拼 innerHTML —— 账号名与 prompt 都是用户输入。
  · cookie 值永远不回显，页面只看得到 cookie 名与条数。
  · 面板不提供 ALLOW_BILLING 开关（改计费必须落 .env 并重启）。
"""

DASHBOARD = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>higgsfield2api 面板</title><style>
*{box-sizing:border-box}
:root{--bg:#f5f6f8;--card:#fff;--line:#e3e6ea;--ink:#1c1f23;--muted:#6b7280;--accent:#2f6fed;
--ok:#0f8a4d;--okbg:#e8f6ee;--warn:#a8620a;--warnbg:#fdf3e3;--bad:#b42318;--badbg:#fdecec;--run:#1f6fb2;--runbg:#e8f2fb}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 24px;background:#fff;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:9}
header h1{margin:0;font-size:15px;letter-spacing:.2px}
.sp{flex:1}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.pill.ok{background:var(--okbg);color:var(--ok)}.pill.warn{background:var(--warnbg);color:var(--warn)}
.pill.bad{background:var(--badbg);color:var(--bad)}.pill.run{background:var(--runbg);color:var(--run)}
.pill.off{background:#f0f1f3;color:var(--muted)}
.ghost{border:1px solid var(--line);background:#fff;color:#374151;padding:6px 12px;border-radius:8px;font:inherit;font-size:12.5px;cursor:pointer}
.ghost:hover{border-color:#c3c9d2;background:#fafbfc}
.ghost[disabled]{opacity:.5;cursor:default}
.mini{padding:3px 9px;font-size:11.5px}
main{max-width:1180px;margin:0 auto;padding:22px 24px 60px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:18px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.kpi .k{font-size:11.5px;color:var(--muted);letter-spacing:.3px;text-transform:uppercase}
.kpi .v{font-size:19px;font-weight:650;margin-top:5px}
.kpi .d{font-size:11.5px;color:var(--muted);margin-top:3px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:16px;overflow:hidden}
section > h2{margin:0;padding:12px 16px;font-size:13px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px}
section > h2 .tag{font-weight:400;color:var(--muted);font-size:11.5px}
.pad{padding:16px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;padding:9px 16px;background:#fafbfc;color:var(--muted);font-weight:600;font-size:11.5px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:10px 16px;border-bottom:1px solid #f1f3f5;vertical-align:top}
tr:last-child td{border-bottom:0}
tr.row:hover td{background:#fafbfd}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11.5px}
td.nw,th.nw{white-space:nowrap}
td.nw{width:1%}
.prompt{max-width:360px;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;color:#374151}
.empty{padding:26px 16px;text-align:center;color:var(--muted);font-size:12.5px}
.ops{display:flex;gap:6px;flex-wrap:wrap}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:900px){.cols{grid-template-columns:1fr}}
label.f{display:block;font-size:11.5px;color:var(--muted);margin:0 0 5px}
textarea,input.t{width:100%;border:1px solid #d6dae1;border-radius:8px;padding:9px 11px;font:inherit;font-size:12.5px;outline:none;background:#fff}
textarea:focus,input.t:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(47,111,237,.1)}
textarea{min-height:92px;resize:vertical;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.actions{display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap}
.btn{border:0;border-radius:8px;background:var(--accent);color:#fff;padding:8px 14px;font:600 13px/1 inherit;cursor:pointer}
.btn:hover{background:#255ed4}.btn[disabled]{opacity:.6;cursor:default}
pre.out{margin:12px 0 0;padding:12px;background:#0f1420;color:#d7e3f4;border-radius:8px;font-size:11.5px;line-height:1.6;
overflow:auto;max-height:280px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
.hint{font-size:11.5px;color:var(--muted);margin-top:8px}
.note{padding:10px 16px;background:var(--warnbg);color:var(--warn);font-size:12px;border-bottom:1px solid var(--line)}
ol.steps{margin:0;padding-left:18px;font-size:12.5px;line-height:1.9;color:#374151}
ol.steps code{font-size:11.5px}
footer{max-width:1180px;margin:0 auto;padding:0 24px 40px;color:#9aa1ac;font-size:11.5px}
code{background:#f2f4f7;padding:1px 5px;border-radius:4px;font-size:11.5px}
</style></head><body>
<header>
  <h1>higgsfield2api</h1>
  <span id="hdrPool" class="pill off">号池读取中…</span>
  <span id="hdrFlags" class="hint"></span>
  <span class="sp"></span>
  <button class="ghost" onclick="refresh()">刷新</button>
  <form method="post" action="/panel/logout" style="margin:0"><button class="ghost" type="submit">退出</button></form>
</header>
<main>
  <div class="kpis">
    <div class="kpi"><div class="k">号池</div><div class="v" id="kPool">—</div><div class="d" id="kPoolSub"></div></div>
    <div class="kpi"><div class="k">计费</div><div class="v" id="kBilling">—</div><div class="d" id="kBillingSub"></div></div>
    <div class="kpi"><div class="k">本地台账</div><div class="v" id="kTasks">—</div><div class="d" id="kTasksSub"></div></div>
    <div class="kpi"><div class="k">护栏</div><div class="v" id="kGuard">—</div><div class="d" id="kGuardSub"></div></div>
  </div>

  <section>
    <h2>号池 <span class="tag" id="poolTag"></span><span class="sp"></span>
      <button class="ghost" id="probeAllBtn">全部自检</button></h2>
    <div class="note" id="poolWarn" hidden></div>
    <div id="poolWrap"><div class="empty">加载中…</div></div>
    <div class="pad" style="border-top:1px solid var(--line);background:#fcfcfd">
      <label class="f" for="accName">新增账号</label>
      <input class="t" id="accName" placeholder="备注名（可留空，例如 号1 / 免费额度A）" style="margin-bottom:8px">
      <textarea id="accCurl" placeholder="curl 'https://fnf-api-gw.higgsfield.ai/fnf/jobs?size=1' -H 'cookie: __client=...; __client_uat=...' ..."></textarea>
      <div class="actions">
        <button class="btn" id="addBtn">导入并自检</button>
        <button class="ghost" id="cancelTarget" hidden>取消替换</button>
        <span class="hint" id="addHint" style="margin-top:0">支持一次粘贴多个号的 Copy as cURL（按行首 curl 自动切分）</span>
      </div>
      <pre class="out" id="addOut" hidden></pre>
    </div>
  </section>

  <section>
    <h2>任务台账 <span class="tag" id="taskTag">最近 50 条</span></h2>
    <div id="taskWrap"><div class="empty">加载中…</div></div>
  </section>

  <div class="cols">
    <section>
      <h2>报文干跑 <span class="tag">零计费</span></h2>
      <div class="pad">
        <label class="f" for="prompt">prompt</label>
        <textarea id="prompt" placeholder="一只橘猫在雨天的霓虹街头慢跑">一只橘猫在雨天的霓虹街头慢跑</textarea>
        <div class="grid2" style="margin-top:10px">
          <div><label class="f" for="dur">duration（4–30）</label><input class="t" id="dur" type="number" value="4" min="4" max="30"></div>
          <div><label class="f" for="ratio">ratio</label>
            <select class="t" id="ratio">
              <option>auto</option><option>16:9</option><option>9:16</option><option>1:1</option>
              <option>4:3</option><option>3:4</option><option>21:9</option>
            </select></div>
        </div>
        <div class="actions"><button class="btn" id="dryBtn">翻译并校验</button>
          <span class="hint" style="margin-top:0">只走本地纯函数，不碰上游</span></div>
        <pre class="out" id="dryOut" hidden></pre>
      </div>
    </section>

    <section>
      <h2>怎么加号 <span class="tag">Copy as cURL 是唯一能拿到 HttpOnly cookie 的路</span></h2>
      <div class="pad">
        <ol class="steps">
          <li>浏览器登录 <code>higgsfield.ai</code>（每个号用独立浏览器 profile 或隐身窗口，避免 cookie 互相覆盖）。</li>
          <li>F12 → Network → 筛 <code>_clerk</code> → 点 <code>v1/client</code> 那条请求 → 右键 <b>Copy as cURL</b>。<br>
              <span class="hint">别从 <code>fnf-api-gw</code> 的请求上复制：那上面只有 <code>Bearer</code> JWT（60 秒就过期），没有 <code>__client</code>。</span></li>
          <li>粘贴到上方「新增账号」→ 导入并自检。返回 <code>✓</code> 即可用。</li>
          <li>多个号：重复 2–3，或一次性粘贴多段 cURL（每段以 <code>curl</code> 开头），批量导入。</li>
        </ol>
        <p class="hint">浏览器里的 <code>document.cookie</code> 拿不到 <code>__client</code>（HttpOnly），粘那个一定失败。</p>
        <p class="hint">实在找不到 <code>_clerk</code> 请求：F12 → Application → Cookies → <code>https://higgsfield.ai</code> → 复制 <code>__client</code> 的值，按 <code>__client=…</code> 形式粘贴（DevTools 能看到 HttpOnly cookie）。</p>
        <p class="hint">号被判定失效时不用删：点「换 cookie」贴新的，统计与历史任务归属都保留。</p>
      </div>
    </section>
  </div>

  <section>
    <h2>只读配置 <span class="tag">改 .env 后 docker compose up -d 生效</span></h2>
    <div class="pad mono" id="cfg" style="line-height:1.9">读取中…</div>
  </section>
</main>
<footer>计费开关不在面板里做：<code>ALLOW_BILLING</code> 只能改 .env 并重启，避免误点一下就花钱。</footer>
<script>
const $ = s => document.querySelector(s);
let ACC = {};        // id -> 账号公开信息
let target = null;   // 非空 = 正在替换该号的 cookie

const PST = {ok:['可用','ok'], new:['未验证','off'], busy:['在飞','run'],
             cooling:['冷却','warn'], invalid:['失效','bad'], disabled:['停用','off']};
const ST  = {queued:['排队','run'], running:['生成中','run'], succeeded:['成功','ok'],
             failed:['失败','bad'], cancelled:['已取消','off'], expired:['过期','warn']};

async function api(path, opts = {}) {
  const r = await fetch(path, {credentials: 'same-origin', ...opts});
  if (r.status === 401) { location.href = '/panel/login'; throw new Error('未登录'); }
  const txt = await r.text();
  let data; try { data = JSON.parse(txt); } catch { data = {raw: txt}; }
  if (!r.ok) throw new Error((data.error && data.error.message) || ('HTTP ' + r.status));
  return data;
}

function pill(text, cls) {
  const s = document.createElement('span');
  s.className = 'pill ' + cls; s.textContent = text; return s;
}

function mkBtn(text, fn, danger) {
  const b = document.createElement('button');
  b.className = 'ghost mini';
  b.textContent = text;
  if (danger) b.style.color = 'var(--bad)';
  b.onclick = fn;
  return b;
}

function ago(ts) {
  if (!ts) return '—';
  const d = Math.floor(Date.now() / 1000 - ts);
  if (d < 5) return '刚刚';
  if (d < 60) return d + ' 秒前';
  if (d < 3600) return Math.floor(d / 60) + ' 分钟前';
  if (d < 86400) return Math.floor(d / 3600) + ' 小时前';
  return Math.floor(d / 86400) + ' 天前';
}

// ------------------------------------------------------------------ 号池

function renderPool(accounts) {
  ACC = {};
  for (const a of accounts) ACC[a.id] = a;
  const wrap = $('#poolWrap');
  if (!accounts.length) {
    wrap.innerHTML = '<div class="empty">号池是空的 —— 在下面粘贴一段 Copy as cURL 就能加入第一个账号。</div>';
    return;
  }
  const t = document.createElement('table');
  const thead = document.createElement('thead');
  thead.innerHTML = '<tr><th>账号</th><th>状态</th><th>成功 / 失败</th><th>最近成功</th><th style="width:250px">操作</th></tr>';
  t.appendChild(thead);
  const tb = document.createElement('tbody');
  for (const a of accounts) {
    const tr = document.createElement('tr'); tr.className = 'row';

    const nm = document.createElement('td');
    const n1 = document.createElement('div'); n1.textContent = a.name; n1.style.fontWeight = '600';
    const n2 = document.createElement('div'); n2.className = 'mono'; n2.style.color = 'var(--muted)';
    n2.textContent = a.id + ' · ' + a.cookie_count + ' 项 cookie' +
      (a.missing.length ? ' · 缺 ' + a.missing.join(' ') : '');
    nm.append(n1, n2);

    const st = document.createElement('td');
    const [label, cls] = PST[a.state] || [a.state || '—', 'off'];
    st.appendChild(pill(label, cls));
    const reason = a.state === 'invalid' ? a.invalid_reason : a.last_error;
    if (reason && a.state !== 'ok') {
      const d = document.createElement('div');
      d.className = 'hint';
      d.style.cssText = 'margin-top:4px;max-width:230px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
      d.textContent = String(reason);
      d.title = reason;
      st.appendChild(d);
    }

    const cnt = document.createElement('td');
    cnt.className = 'mono';
    cnt.textContent = a.ok_count + ' / ' + a.fail_count;

    const last = document.createElement('td');
    last.className = 'mono'; last.textContent = ago(a.last_ok_at);

    const ops = document.createElement('td');
    const box = document.createElement('div'); box.className = 'ops';
    box.append(
      mkBtn('自检', () => probeOne(a.id)),
      mkBtn('换 cookie', () => startReplace(a)),
      mkBtn(a.enabled ? '停用' : '启用', () => toggleOne(a.id, !a.enabled)),
      mkBtn('删除', () => removeOne(a.id, a.name), true)
    );
    ops.appendChild(box);

    tr.append(nm, st, cnt, last, ops);
    tb.appendChild(tr);
  }
  t.appendChild(tb);
  wrap.replaceChildren(t);
}

async function probeOne(id) {
  try {
    const r = await api('/panel/api/accounts/' + encodeURIComponent(id) + '/probe', {method: 'POST'});
    const p = r.probe;
    alert(p.ok ? ('自检通过 · ' + p.ms + 'ms') : ('自检失败 ' + (p.status || '') + '\n' + (p.detail || '')));
    renderPool(r.accounts); refresh(false);
  } catch (e) { alert('自检出错：' + e.message); }
}

async function toggleOne(id, enabled) {
  try {
    const r = await api('/panel/api/accounts/' + encodeURIComponent(id) + '/toggle', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: enabled})});
    renderPool(r.accounts); refresh(false);
  } catch (e) { alert('操作失败：' + e.message); }
}

async function removeOne(id, name) {
  if (!confirm('删除账号「' + name + '」？\n\n已跑完的任务不受影响（上游查询是匿名可读的），但这个号的统计会被清掉。')) return;
  try {
    await api('/panel/api/accounts/' + encodeURIComponent(id), {method: 'DELETE'});
    refresh();
  } catch (e) { alert('删除失败：' + e.message); }
}

function startReplace(a) {
  target = a.id;
  $('#accName').value = a.name;
  $('#accName').disabled = true;
  $('#addBtn').textContent = '替换并自检';
  $('#cancelTarget').hidden = false;
  $('#addHint').textContent = '正在替换「' + a.name + '」的 cookie';
  $('#accCurl').value = '';
  $('#accCurl').scrollIntoView({behavior: 'smooth', block: 'center'});
  $('#accCurl').focus();
}

function clearReplace() {
  target = null;
  $('#accName').disabled = false;
  $('#accName').value = '';
  $('#addBtn').textContent = '导入并自检';
  $('#cancelTarget').hidden = true;
  $('#addHint').textContent = '支持一次粘贴多个号的 Copy as cURL（按行首 curl 自动切分）';
}

$('#cancelTarget').onclick = clearReplace;

$('#addBtn').onclick = async () => {
  const out = $('#addOut'); out.hidden = false;
  const txt = $('#accCurl').value.trim();
  if (!txt) { out.textContent = '先粘贴 Copy as cURL 的内容'; return; }
  const btn = $('#addBtn');
  btn.disabled = true;
  const replacing = target;
  btn.textContent = '处理中…';
  out.textContent = '解析并逐个自检中（每个号约 1–2 秒）…';
  try {
    if (replacing) {
      const r = await api('/panel/api/accounts/' + encodeURIComponent(replacing) + '/cookie', {
        method: 'POST', headers: {'Content-Type': 'text/plain'}, body: txt});
      const p = r.probe;
      out.textContent = (p.ok ? '✓ 替换成功，自检通过（' + p.ms + 'ms）'
                              : '✗ 已写入，但自检失败：' + (p.detail || p.status)) +
        '\n' + (r.missing.length ? '缺少 ' + r.missing.join(' ') + ' —— 大概率不是 Copy as cURL' : 'cookie 包含 __client');
      $('#accCurl').value = '';
      clearReplace();
    } else {
      const name = $('#accName').value.trim();
      const r = await api('/panel/api/accounts?name=' + encodeURIComponent(name), {
        method: 'POST', headers: {'Content-Type': 'text/plain'}, body: txt});
      const lines = ['已导入 ' + r.added + ' 个账号：'];
      for (const x of r.results) {
        const p = x.probe;
        if (!p) { lines.push('  · ' + x.account.name + ' — 已加入，未自检'); continue; }
        lines.push((p.ok ? '  ✓ ' : '  ✗ ') + x.account.name + ' — ' +
          (p.ok ? '自检通过（' + p.ms + 'ms）' : '自检失败 ' + (p.status || '') + '：' + (p.detail || '')));
      }
      if (r.pending) lines.push('  （还有 ' + r.pending + ' 个未自检，点右上「全部自检」）');
      for (const e of (r.errors || [])) lines.push('  ! 第 ' + e.index + ' 段解析失败：' + e.detail);
      out.textContent = lines.join('\n');
      $('#accCurl').value = '';
      $('#accName').value = '';
    }
    refresh(false);
  } catch (e) {
    out.textContent = '错误：' + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = target ? '替换并自检' : '导入并自检';
  }
};

$('#probeAllBtn').onclick = async () => {
  const b = $('#probeAllBtn');
  b.disabled = true; b.textContent = '自检中…';
  try {
    const r = await api('/panel/api/accounts/probe-all', {method: 'POST'});
    const bad = r.probe.filter(p => !p.ok).length;
    renderPool(r.accounts); refresh(false);
    alert('自检完成：' + (r.probe.length - bad) + ' 个通过，' + bad + ' 个异常');
  } catch (e) { alert('自检出错：' + e.message); }
  finally { b.disabled = false; b.textContent = '全部自检'; }
};

// ------------------------------------------------------------------ 台账

function renderTasks(tasks) {
  const wrap = $('#taskWrap');
  if (!tasks.length) {
    wrap.innerHTML = '<div class="empty">还没有任务。先在号池里导入至少一个号，再到干跑区核对报文。</div>';
    return;
  }
  const t = document.createElement('table');
  const thead = document.createElement('thead');
  thead.innerHTML = '<tr><th>创建时间</th><th>ID</th><th>账号</th><th>状态</th><th>时长/比例</th><th>prompt</th><th>产物</th></tr>';
  t.appendChild(thead);
  const tb = document.createElement('tbody');
  for (const x of tasks) {
    const tr = document.createElement('tr'); tr.className = 'row';

    const when = document.createElement('td');
    when.className = 'mono nw';
    when.textContent = new Date((x.created_at || 0) * 1000).toLocaleString('zh-CN', {hour12: false});

    const id = document.createElement('td');
    id.className = 'mono nw';
    id.textContent = (x.id || '').slice(0, 12) + ((x.id || '').length > 12 ? '…' : '');

    const acc = document.createElement('td');
    acc.className = 'mono nw';
    const a = ACC[x.account_id];
    const full = a ? a.name : (x.account_id || '—');
    acc.textContent = full.length > 12 ? full.slice(0, 12) + '…' : full;
    acc.title = a ? (a.name + ' · ' + a.id) : full;

    const status = document.createElement('td');
    const [label, cls] = ST[x.status] || [x.status || '—', 'off'];
    status.appendChild(pill(label, cls));
    if (x.error && x.error.message) {
      const e = document.createElement('div');
      e.className = 'hint';
      e.style.cssText = 'margin-top:4px;max-width:170px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
      e.textContent = String(x.error.message);
      e.title = String(x.error.message);
      status.appendChild(e);
    }

    const meta = document.createElement('td');
    meta.className = 'mono nw';
    meta.textContent = (x.duration || '—') + 's / ' + (x.ratio || '—') + ' / ' + (x.resolution || '—');

    const pr = document.createElement('td');
    const p = document.createElement('div'); p.className = 'prompt';
    p.textContent = x.prompt || '（无 prompt 记录）';
    pr.appendChild(p);

    const out = document.createElement('td');
    out.className = 'nw';
    const url = x.content && x.content.video_url;
    if (url && /^https?:\/\//i.test(url)) {
      const link = document.createElement('a');
      link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
      link.textContent = '视频 ↗'; link.style.fontSize = '12px';
      out.appendChild(link);
    } else if (x.id) {
      const b = document.createElement('button');
      b.className = 'ghost mini'; b.textContent = '刷新';
      b.onclick = async () => {
        b.disabled = true; b.textContent = '刷新中…';
        try { await api('/panel/api/task/' + encodeURIComponent(x.id)); }
        catch (e) { b.textContent = '失败'; }
        finally { setTimeout(() => { b.disabled = false; b.textContent = '刷新'; refresh(false); }, 500); }
      };
      out.appendChild(b);
    } else { out.textContent = '—'; }

    tr.append(when, id, acc, status, meta, pr, out);
    tb.appendChild(tr);
  }
  t.appendChild(tb);
  wrap.replaceChildren(t);
}

// ------------------------------------------------------------------ 总体

async function refresh(rerender = true) {
  try {
    const s = await api('/panel/api/state');
    const f = s.flags, p = s.pool;

    renderPool(s.accounts);
    if (rerender) renderTasks(s.tasks);

    $('#kPool').textContent = p.usable + ' / ' + p.total + ' 可用';
    $('#kPool').style.color = p.usable ? 'var(--ok)' : 'var(--bad)';
    const bits = [];
    if (p.invalid) bits.push('失效 ' + p.invalid);
    if (p.disabled) bits.push('停用 ' + p.disabled);
    if (p.cooling) bits.push('冷却 ' + p.cooling);
    $('#kPoolSub').textContent = bits.length ? bits.join(' · ') : (p.total ? '全部正常' : '还没加号');

    const hp = $('#hdrPool');
    hp.className = 'pill ' + (p.usable ? 'ok' : (p.total ? 'bad' : 'off'));
    hp.textContent = p.usable ? (p.usable + '/' + p.total + ' 号可用')
                              : (p.total ? '号池无可用账号' : '号池为空');
    $('#poolTag').textContent = '策略：' + p.strategy + ' · 建任务最多试 ' + p.retry + ' 个号';

    const warn = $('#poolWarn');
    if (p.warning) { warn.hidden = false; warn.textContent = '⚠ ' + p.warning; }
    else { warn.hidden = true; }

    $('#kBilling').textContent = f.billing ? '已开启' : '关闭';
    $('#kBilling').style.color = f.billing ? 'var(--warn)' : 'var(--ok)';
    $('#kBillingSub').textContent = f.billing ? '建任务会真实消耗额度' : '建任务返回 403，只翻译不花钱';

    $('#kTasks').textContent = s.tasks.length;
    const c = s.counts;
    $('#kTasksSub').textContent = '成功 ' + c.succeeded + ' · 失败 ' + c.failed + ' · 进行中 ' + (c.queued + c.running);

    $('#kGuard').textContent = '≤' + f.max_duration + 's / ≤' + f.max_batch + ' 条';
    $('#kGuardSub').textContent = (f.auth ? 'API 鉴权已开' : '⚠ API 无鉴权') + ' · 免费额度优先 ' + (f.use_free_gens ? '是' : '否');
    $('#hdrFlags').textContent = f.billing ? '计费已开' : '计费关闭';

    const cfg = $('#cfg');
    cfg.textContent = '';
    const rows = {
      'SERVICE_TOKEN': f.auth ? '已设置（面板与 API 共用同一口令）' : '未设置（危险：API 无鉴权）',
      'ALLOW_BILLING': String(f.billing),
      'MAX_DURATION': f.max_duration + ' 秒',
      'MAX_BATCH': String(f.max_batch),
      'USE_FREE_GENS': String(f.use_free_gens),
      'EXECUTION_EXPIRES_AFTER': f.expires_after + ' 秒',
      '号池文件': 'data/accounts.json（0600，含 cookie，勿外传）',
      '服务器时间': new Date(s.server_time * 1000).toLocaleString('zh-CN', {hour12: false})
    };
    for (const [k, v] of Object.entries(rows)) {
      const d = document.createElement('div');
      d.textContent = k + ' = ' + v;
      cfg.appendChild(d);
    }
  } catch (e) {
    console.error(e);
  }
}

$('#dryBtn').onclick = async () => {
  const out = $('#dryOut'); out.hidden = false; out.textContent = '计算中…';
  const body = {
    model: 'magi_2',
    content: [{type: 'text', text: $('#prompt').value || 'x'}],
    duration: Number($('#dur').value || 4),
    ratio: $('#ratio').value
  };
  try {
    out.textContent = JSON.stringify(await api('/panel/api/dry-run', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}), null, 2);
  } catch (e) { out.textContent = '错误：' + e.message; }
};

refresh();
setInterval(() => refresh(true), 15000);
</script></body></html>"""
