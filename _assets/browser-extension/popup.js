/** 小助下载器 · 扩展弹窗逻辑（v3.0.7） */
'use strict';

const $ = (id) => document.getElementById(id);
let currentTab = null;
let currentTabItems = [];
let currentTasks = [];
let currentHistory = [];
let activeTab = 'media';
// ★ v3.1.10：轮询与手动刷新会并发，旧响应可能后到并覆盖新视图。
//   每次刷新领一个序号，只有"最新一次请求"的响应才允许写入界面。
let refreshSeq = 0;

function fmtSize(n) {
  if (!n) return '';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + ' ' + u[i];
}

function fmtSpeed(n) {
  if (!n) return '';
  return fmtSize(n) + '/s';
}

function tagOf(type) {
  return type === 'hls' ? 'HLS' : type === 'video' ? '视频'
    : type === 'audio' ? '音频' : type === 'file' ? '文件' : '媒体';
}

function stateClass(state) {
  if (state === 'done') return 'done';
  if (state === 'error') return 'err';
  return '';
}

function stateText(state) {
  const map = {
    pending: '准备中', waiting: '排队中', downloading: '下载中',
    paused: '已暂停', done: '已完成', error: '出错', cancelled: '已取消'
  };
  return map[state] || state;
}

async function send(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, (r) => {
      resolve(chrome.runtime.lastError ? { ok: false, error: chrome.runtime.lastError.message } : r);
    });
  });
}

function setTab(name) {
  activeTab = name;
  document.querySelectorAll('.tabs button').forEach((b) => {
    b.classList.toggle('active', b.dataset.tab === name);
  });
  const foot = $('foot');
  const btnAll = $('btn-all');
  const hint = $('hint');
  if (name === 'media') {
    btnAll.style.display = '';
    hint.textContent = '嗅探到的媒体均可直接下载';
  } else {
    btnAll.style.display = 'none';
    hint.textContent = name === 'tasks' ? '点击任务可查看详情' : '右键扩展图标可打开最近文件/目录';
  }
  render();
}

function renderStatus(res) {
  const status = $('status');
  if (res && res.connected) {
    status.textContent = `已连接下载器 · 127.0.0.1:${res.port}`;
    status.className = 'status ok';
    const wr = $('wake-row');
    if (wr) wr.style.display = 'none';
  } else {
    status.textContent = '未检测到下载器，点下方按钮启动';
    status.className = 'status warn';
    const wr = $('wake-row');
    if (wr) wr.style.display = 'block';
  }
}

function render() {
  const list = $('list');
  list.innerHTML = '';
  $('count').textContent = '';

  if (activeTab === 'media') {
    renderMedia(list);
  } else if (activeTab === 'tasks') {
    renderTasks(list);
  } else {
    renderHistory(list);
  }
}

function renderMedia(list) {
  const items = currentTabItems || [];
  $('count').textContent = items.length ? `（${items.length}）` : '';
  if (!items.length) {
    list.innerHTML = '<div class="empty">暂未嗅探到媒体<br><span>播放一下视频，或点上方「分析页面」</span></div>';
    return;
  }
  items.forEach((it) => {
    const row = document.createElement('div');
    row.className = 'row';
    const tag = document.createElement('span');
    tag.className = 'tag ' + (it.type || 'media');
    tag.textContent = tagOf(it.type);
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = it.filename || it.url;
    name.title = it.url;
    const frame = document.createElement('span');
    if (it.fromFrame) {
      frame.className = 'frame';
      frame.textContent = '内嵌框架';
      frame.title = '来自内嵌框架（iframe）';
    }
    const size = document.createElement('span');
    size.className = 'size';
    size.textContent = fmtSize(it.size);
    const btn = document.createElement('button');
    btn.className = 'primary';
    btn.textContent = '下载';
    btn.addEventListener('click', async () => {
      btn.textContent = '…';
      const r = await send({
        cmd: 'download', url: it.url, filename: it.filename,
        referer: it.referer || (currentTab ? currentTab.url : ''),
        kind: it.type === 'hls' ? 'hls' : 'auto'
      });
      btn.textContent = r && r.ok ? '已发送' : '失败';
      $('hint').textContent = r && r.ok ? '已加入下载队列'
        : ('失败：' + ((r && r.error) || '未知'));
      setTimeout(() => { btn.textContent = '下载'; }, 2200);
    });
    row.append(tag, name, frame, size, btn);
    list.appendChild(row);
  });
}

function renderTasks(list) {
  const tasks = currentTasks || [];
  $('count').textContent = tasks.length ? `（${tasks.length}）` : '';
  if (!tasks.length) {
    list.innerHTML = '<div class="empty">暂无进行中的下载<br><span>下完的任务会自动移到「已完成」</span></div>';
    return;
  }
  tasks.forEach((t) => {
    const row = document.createElement('div');
    row.className = 'row task';
    const st = document.createElement('span');
    st.className = 'state ' + stateClass(t.state);
    st.textContent = t.state_cn || stateText(t.state);
    const wrap = document.createElement('div');
    wrap.className = 'progress-wrap';
    const name = document.createElement('div');
    name.className = 'name';
    name.textContent = t.filename || t.url || '未知文件';
    name.title = t.err || '';
    const barWrap = document.createElement('div');
    barWrap.className = 'progress-bar';
    const bar = document.createElement('div');
    bar.className = stateClass(t.state);
    bar.style.width = Math.max(0, Math.min(100, (t.progress || 0) * 100)) + '%';
    barWrap.appendChild(bar);
    const meta = document.createElement('div');
    meta.className = 'meta';
    const parts = [];
    if (t.size) parts.push(fmtSize(t.size));
    if (t.speed) parts.push(fmtSpeed(t.speed));
    if (t.eta) parts.push('约' + t.eta);
    if (t.phase && t.state === 'downloading') parts.push(t.phase);
    meta.textContent = parts.join(' · ');
    wrap.append(name, barWrap, meta);
    row.append(st, wrap);
    list.appendChild(row);
  });
}

function renderHistory(list) {
  const items = currentHistory || [];
  $('count').textContent = items.length ? `（${items.length}）` : '';
  if (!items.length) {
    list.innerHTML = '<div class="empty">暂无已完成记录<br><span>下载完成后会自动出现在这里</span></div>';
    return;
  }
  items.slice().reverse().forEach((it) => {
    const row = document.createElement('div');
    row.className = 'row';
    const tag = document.createElement('span');
    tag.className = 'tag done';
    tag.textContent = '完成';
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = it.filename || it.url;
    name.title = it.final || it.url;
    const size = document.createElement('span');
    size.className = 'size';
    size.textContent = fmtSize(it.size);
    const acts = document.createElement('div');
    acts.className = 'acts-hist';

    // 文件已被移动/删除时给出视觉提示，不让用户点了才发现
    const gone = (it.exists === false);
    const btnOpen = document.createElement('button');
    btnOpen.textContent = '打开';
    if (gone) {
      btnOpen.disabled = true;
      btnOpen.title = '文件已不在原位置';
      btnOpen.style.opacity = '0.45';
      btnOpen.style.cursor = 'not-allowed';
    }
    btnOpen.addEventListener('click', async (ev) => {
      if (gone) { showHint('文件已不在原位置，可点「目录」查看', true); return; }
      const btn = ev.currentTarget;
      btn.textContent = '…';
      const r = await send({ cmd: 'openFile', tid: it.tid, mode: 'file' });
      btn.textContent = '打开';
      showHint(r && r.ok ? '已打开文件' : ('打开失败：' + ((r && r.error) || '未知')), !(r && r.ok));
    });

    const btnDir = document.createElement('button');
    btnDir.textContent = '目录';
    btnDir.addEventListener('click', async (ev) => {
      const btn = ev.currentTarget;
      btn.textContent = '…';
      const r = await send({ cmd: 'openFile', tid: it.tid, mode: 'folder' });
      btn.textContent = '目录';
      if (r && r.ok) {
        showHint(r.note || '已打开目录', false);
      } else {
        showHint('打开失败：' + ((r && r.error) || '未知'), true);
      }
    });

    acts.append(btnOpen, btnDir);
    row.append(tag, name, size, acts);
    if (gone) row.title = '文件已不在原位置（仍可打开所在目录）';
    list.appendChild(row);
  });
}

/** 底部提示：err=true 时用醒目样式，避免"点了没反应"的错觉 */
function showHint(text, err) {
  const h = $('hint');
  if (!h) return;
  h.textContent = text;
  h.className = err ? 'hint err' : 'hint';
  h.style.color = err ? '#d93025' : '';
  clearTimeout(showHint._t);
  showHint._t = setTimeout(() => {
    h.textContent = activeTab === 'media' ? '嗅探到的媒体均可直接下载' : '点击任务可查看详情';
    h.className = 'hint';
    h.style.color = '';
  }, 6000);
}

async function refreshMedia(seq) {
  const res = await send({ cmd: 'getItems', tabId: currentTab ? currentTab.id : null });
  if (seq !== refreshSeq) return;          // 已有更新的刷新在途，丢弃本次旧数据
  if (res && res.ok) {
    currentTabItems = res.items || [];
    currentTasks = res.tasks || [];
    renderStatus(res);
  }
}

async function refreshTasks(seq) {
  const res = await send({ cmd: 'getTasks' });
  if (seq !== refreshSeq) return;
  if (res && res.ok) {
    currentTasks = res.tasks || [];
    renderStatus(res);
  }
}

async function refreshHistory(seq) {
  const res = await send({ cmd: 'getHistory', limit: 100 });
  if (seq !== refreshSeq) return;
  if (res && res.ok) {
    currentHistory = res.items || [];
  }
}

async function refresh() {
  const seq = ++refreshSeq;
  if (activeTab === 'media') {
    await refreshMedia(seq);
  } else if (activeTab === 'tasks') {
    await refreshTasks(seq);
  } else {
    await refreshHistory(seq);
  }
  if (seq !== refreshSeq) return;
  render();
}

function setupTabs() {
  document.querySelectorAll('.tabs button').forEach((b) => {
    b.addEventListener('click', () => {
      setTab(b.dataset.tab);
      refresh();
    });
  });
}

function setup() {
  setupTabs();
  $('btn-scan').addEventListener('click', async () => {
    $('status').textContent = '正在分析页面…';
    $('status').className = 'status';
    // ★ v3.1.10：带上 tabId，让后台把结果写进"这个标签页"的列表；
    //   不带时后台只能靠 sender 推断，扩展弹窗场景会落到 -1 而丢弃结果。
    const r = await send({
      cmd: 'scanPage', url: currentTab.url,
      tabId: currentTab ? currentTab.id : null
    });
    const n = (r && r.items) ? r.items.length : 0;
    if (r && r.ok) {
      $('hint').textContent = n
        ? `分析完成，本页列表共 ${n} 个媒体`
        : '分析完成，本页未发现媒体';
    } else {
      $('hint').textContent = '分析失败：' + ((r && r.error) || '未知');
    }
    await refresh();
  });
  $('btn-clear').addEventListener('click', async () => {
    await send({ cmd: 'clear', tabId: currentTab ? currentTab.id : null });
    refresh();
  });
  $('btn-all').addEventListener('click', async () => {
    const items = currentTabItems || [];
    if (!items.length) { $('hint').textContent = '没有可下载的媒体'; return; }
    const payload = items.map((it) => ({
      url: it.url, filename: it.filename,
      referer: it.referer || (currentTab ? currentTab.url : ''),
      kind: it.type === 'hls' ? 'hls' : 'auto'
    }));
    const r = await send({
      cmd: 'downloadBatch', items: payload,
      page: currentTab ? currentTab.url : ''
    });
    // ★ v3.1.10：/batch 是逐项返回的，笼统按 r.ok 报"全部成功"会掩盖部分失败。
    if (r && r.ok && Array.isArray(r.results)) {
      const bad = r.results.filter((x) => !(x && x.ok));
      $('hint').textContent = bad.length
        ? `已发送 ${r.results.length - bad.length} 个，${bad.length} 个失败：` +
          bad.slice(0, 2).map((x) => (x && x.error) || '未知').join('；')
        : `已发送 ${r.results.length} 个任务`;
    } else {
      $('hint').textContent = r && r.ok ? `已发送 ${items.length} 个任务`
        : ('发送失败：' + ((r && r.error) || '请先启动下载器'));
    }
  });
  const sel = $('takeover');
  if (sel) {
    sel.addEventListener('change', async () => {
      await send({ cmd: 'setTakeover', mode: sel.value });
      const txt = sel.value === 'off' ? '已关闭接管，浏览器下载恢复原样'
        : sel.value === 'all' ? '已接管全部下载'
        : '已接管媒体与压缩包下载';
      $('hint').textContent = txt;
    });
  }
  const wk = $('btn-wake');
  if (wk) {
    wk.addEventListener('click', async () => {
      wk.textContent = '正在启动…';
      wk.disabled = true;
      const r = await send({ cmd: 'wake' });
      if (r && r.ok) {
        $('hint').textContent = '下载器已启动';
        await refresh();
      } else {
        $('hint').textContent = '启动失败：' + ((r && r.error) || '未知') +
          '（请手动打开小助下载器）';
      }
      wk.textContent = '启动小助下载器';
      wk.disabled = false;
    });
  }
}

async function boot() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  currentTab = tabs && tabs[0] ? tabs[0] : null;
  setup();
  const st = await send({ cmd: 'getSettings' });
  if (st && st.ok) {
    const sel = $('takeover');
    if (sel) sel.value = st.takeover || 'all';
  }
  await refresh();
  // 自动轮询：让下载进度/完成记录实时更新（媒体页除外，避免打断浏览）
  setInterval(() => { if (activeTab !== 'media') refresh(); }, 1200);
}

document.addEventListener('DOMContentLoaded', boot);
