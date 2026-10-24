/** 小助下载器 · 扩展弹窗逻辑 */
'use strict';

const $ = (id) => document.getElementById(id);
let currentTab = null;

function fmtSize(n) {
  if (!n) return '';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + ' ' + u[i];
}

function tagOf(type) {
  return type === 'hls' ? 'HLS' : type === 'video' ? '视频'
    : type === 'audio' ? '音频' : type === 'file' ? '文件' : '媒体';
}

async function send(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, (r) => {
      resolve(chrome.runtime.lastError ? { ok: false, error: chrome.runtime.lastError.message } : r);
    });
  });
}

function render(res) {
  const status = $('status');
  const list = $('list');
  if (!res || !res.ok) {
    status.textContent = '读取失败';
    status.className = 'status warn';
    return;
  }
  const items = res.items || [];
  $('count').textContent = items.length ? `（${items.length}）` : '';
  if (res.connected) {
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
  list.innerHTML = '';
  if (!items.length) {
    list.innerHTML = '<div class="empty">暂未嗅探到媒体<br>' +
      '<span>播放一下视频，或点上方「分析页面」</span></div>';
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

async function refresh() {
  const res = await send({ cmd: 'getItems', tabId: currentTab ? currentTab.id : null });
  render(res);
}

function setup() {
  $('btn-scan').addEventListener('click', async () => {
    $('status').textContent = '正在分析页面…';
    $('status').className = 'status';
    const r = await send({ cmd: 'scanPage', url: currentTab.url });
    if (r && r.ok) {
      $('hint').textContent = `分析完成，共发现 ${(r.items || []).length} 个媒体`;
    } else {
      $('hint').textContent = '分析失败：' + ((r && r.error) || '未知');
    }
    setTimeout(refresh, 400);
  });
  $('btn-clear').addEventListener('click', async () => {
    await send({ cmd: 'clear', tabId: currentTab ? currentTab.id : null });
    refresh();
  });
  $('btn-all').addEventListener('click', async () => {
    const res = await send({ cmd: 'getItems', tabId: currentTab ? currentTab.id : null });
    const items = (res && res.items) || [];
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
    $('hint').textContent = r && r.ok ? `已发送 ${items.length} 个任务`
      : ('发送失败：' + ((r && r.error) || '请先启动下载器'));
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
          '（请手动双击「启动下载器.bat」）';
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
  // 读取接管模式
  const st = await send({ cmd: 'getSettings' });
  if (st && st.ok) {
    const sel = $('takeover');
    if (sel) sel.value = st.takeover || 'media';
  }
  await refresh();
}

document.addEventListener('DOMContentLoaded', boot);