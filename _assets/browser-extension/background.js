/**
 * 小助下载器 · 媒体嗅探扩展 —— 后台服务
 * ==========================================
 * 职责：
 *   1. 监听所有网络请求，识别视频/音频/流媒体（m3u8 / mp4 / mp3 / flv …）
 *   2. 按标签页归档嗅探结果，徽标显示数量
 *   3. 把链接发送给本机下载器（http://127.0.0.1:8763），自动获取并缓存令牌
 *   4. 右键菜单："用小助下载器下载此链接"
 * 隐私：所有数据只存在本机浏览器与本地下载器进程内，不上报任何服务器。
 */

const DEFAULT_PORT = 8763;
const PORT_RANGE = 12;          // 下载器端口被占用时会自动 +1，向后探测若干位
const MAX_PER_TAB = 120;

// 需要嗅探的扩展名（按 IDM 的做法，覆盖常见媒体与流格式）
const MEDIA_EXT = [
  'm3u8', 'mpd', 'mp4', 'webm', 'mkv', 'flv', 'mov', 'avi', 'ts', 'm4v',
  'mp3', 'm4a', 'aac', 'flac', 'wav', 'ogg', 'opus', 'wma',
  'zip', 'rar', '7z', 'iso', 'exe', 'msi', 'apk', 'pdf', 'dmg'
];
const VIDEO_EXT = ['m3u8', 'mpd', 'mp4', 'webm', 'mkv', 'flv', 'mov', 'ts', 'm4v'];
const AUDIO_EXT = ['mp3', 'm4a', 'aac', 'flac', 'wav', 'ogg', 'opus', 'wma'];

// 媒体类 Content-Type
const MEDIA_MIME = [
  'video/', 'audio/', 'application/vnd.apple.mpegurl', 'application/x-mpegurl',
  'application/octet-stream', 'application/dash+xml'
];

const state = {
  byTab: {},            // tabId -> { items: [], page: '' }
  token: '',
  port: DEFAULT_PORT,
  connected: false,
  takeover: 'all',      // off | media | all —— 接管浏览器下载的模式（默认全部接管）
  tasks: [],            // 当前活动任务快照（popup 轮询展示）
  history: []           // 已完成下载记录（popup 展示 + 右键打开）
};

// ------------------------- 持久化 -------------------------
async function loadState() {
  try {
    const s = await chrome.storage.local.get(['sniff', 'token', 'port', 'takeover']);
    state.byTab = s.sniff || {};
    state.token = s.token || '';
    state.port = s.port || DEFAULT_PORT;
    state.takeover = s.takeover || 'all';
  } catch (e) { /* ignore */ }
}
function saveState() {
  try {
    chrome.storage.local.set({
      sniff: state.byTab, token: state.token,
      port: state.port, takeover: state.takeover
    });
  } catch (e) { /* ignore */ }
}

function tabStore(tabId) {
  if (!state.byTab[tabId]) state.byTab[tabId] = { items: [], page: '' };
  return state.byTab[tabId];
}

// ------------------------- 嗅探判定 -------------------------
function getExt(url) {
  try {
    const u = new URL(url);
    const path = u.pathname;
    const i = path.lastIndexOf('.');
    if (i < 0) return { ext: '', query: u.search };
    return { ext: path.slice(i + 1).toLowerCase().split('/').pop(), query: u.search };
  } catch (e) {
    return { ext: '', query: '' };
  }
}

function isMediaUrl(url) {
  if (!url || !/^https?:/i.test(url)) return null;
  const { ext } = getExt(url);
  if (VIDEO_EXT.includes(ext)) {
    return { type: ext === 'm3u8' || ext === 'mpd' ? 'hls' : 'video', ext };
  }
  if (AUDIO_EXT.includes(ext)) return { type: 'audio', ext };
  if (MEDIA_EXT.includes(ext)) return { type: 'file', ext };
  return null;
}

function filenameOf(url) {
  try {
    const u = new URL(url);
    const raw = u.pathname.split('/').filter(Boolean).pop() || '';
    let name = decodeURIComponent(raw.split(';')[0]);
    if (!name || name.length > 120) name = 'media_' + Date.now();
    return name.replace(/[\\/:*?"<>|]/g, '_');
  } catch (e) {
    return 'media_' + Date.now();
  }
}

function sizeOf(headers) {
  if (!headers) return null;
  for (const h of headers) {
    const n = (h.name || '').toLowerCase();
    if (n === 'content-length') {
      const v = parseInt(h.value, 10);
      if (v > 0) return v;
    }
    if (n === 'content-range') {
      const m = /\/(\d+)\s*$/.exec(h.value || '');
      if (m) return parseInt(m[1], 10);
    }
  }
  return null;
}

/** 记录一条嗅探结果 */
function record(tabId, url, opts = {}) {
  if (!tabId || tabId < 0) return false;
  const store = tabStore(tabId);
  if (store.items.some((it) => it.url === url)) return false;
  const info = isMediaUrl(url) || { type: opts.type || 'media', ext: '' };
  const item = {
    url,
    type: info.type,
    ext: info.ext || '',
    filename: opts.filename || filenameOf(url),
    size: opts.size || null,
    page: store.page || '',
    referer: opts.referer || store.page || '',
    from: opts.from || 'network',
    frameId: opts.frameId != null ? opts.frameId : null,
    fromFrame: !!opts.fromFrame,
    ts: Date.now()
  };
  store.items.unshift(item);
  if (store.items.length > MAX_PER_TAB) store.items.length = MAX_PER_TAB;
  updateBadge(tabId);
  saveState();
  return true;
}

function updateBadge(tabId) {
  const store = state.byTab[tabId];
  const n = store ? store.items.length : 0;
  try {
    chrome.action.setBadgeText({ tabId, text: n ? String(n) : '' });
    chrome.action.setBadgeBackgroundColor({ tabId, color: '#2f6fed' });
  } catch (e) { /* ignore */ }
}

// ------------------------- 网络监听 -------------------------
// 监听所有响应头，凡扩展名或 Content-Type 命中即记录
chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const url = details.url;
    const ctype = (details.responseHeaders || [])
      .filter((h) => (h.name || '').toLowerCase() === 'content-type')
      .map((h) => (h.value || '').toLowerCase())[0] || '';

    const hit = isMediaUrl(url);
    const mimeHit = MEDIA_MIME.some((m) => ctype.includes(m)) &&
                    !ctype.includes('image/') && !ctype.includes('text/html');
    if (!hit && !mimeHit) return;

    const store = tabStore(details.tabId);
    if (!store.page) store.page = details.initiator || '';
    record(details.tabId, url, {
      type: hit ? hit.type : (ctype.startsWith('video') ? 'video'
            : ctype.startsWith('audio') ? 'audio' : 'media'),
      size: sizeOf(details.responseHeaders),
      referer: details.initiator || store.page,
      from: hit ? 'network' : 'mime',
      frameId: details.frameId,
      fromFrame: details.frameId !== 0
    });
  },
  { urls: ['<all_urls>'] },
  ['responseHeaders']
);

// ------------------------- 标签页生命周期 -------------------------
chrome.tabs.onRemoved.addListener((tabId) => {
  delete state.byTab[tabId];
  saveState();
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'loading' && changeInfo.url) {
    // 换了地址（含 SPA 跳转）→ 清空该标签页旧结果
    state.byTab[tabId] = { items: [], page: changeInfo.url };
    updateBadge(tabId);
    saveState();
  } else if (changeInfo.status === 'complete' && tab && tab.url) {
    const store = tabStore(tabId);
    store.page = tab.url;
    saveState();
  }
});

// ------------------------- 与本地下载器通信 -------------------------
/**
 * ★ v3.1.9 修复：所有本地接口请求必须带超时。
 *   此前用裸 fetch —— 下载器正在下载大量任务时，/status 偶发要
 *   1.8~2 秒才应答（GIL 争抢）；若服务端卡住，fetch 会**永远挂起**，
 *   用户点「打开 / 目录」后按钮一直没反应，也看不到任何错误提示。
 *   这里统一加 AbortController 超时，超时即抛错 → 上层能给出明确提示。
 */
const API_TIMEOUT_MS = 6000;

async function xzdFetch(url, options = {}, timeoutMs = API_TIMEOUT_MS) {
  const ctl = new AbortController();
  const timer = setTimeout(() => {
    try { ctl.abort(); } catch (e) { /* ignore */ }
  }, timeoutMs);
  try {
    return await fetch(url, Object.assign({}, options, { signal: ctl.signal }));
  } catch (e) {
    if (e && (e.name === 'AbortError' || e.name === 'TimeoutError')) {
      throw new Error(`请求超时（超过 ${Math.round(timeoutMs / 1000)} 秒无响应）`);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

async function probePort() {
  // 依次探测端口，找到正在运行的下载器
  for (let p = DEFAULT_PORT; p < DEFAULT_PORT + PORT_RANGE; p++) {
    try {
      const r = await xzdFetch(`http://127.0.0.1:${p}/ping`, { method: 'GET' }, 1200);
      if (!r.ok) continue;
      const j = await r.json();
      if (j && j.ok && j.app) {
        state.port = p;
        if (j.token) state.token = j.token;
        state.connected = true;
        saveState();
        return true;
      }
    } catch (e) { /* 该端口无服务 */ }
  }
  state.connected = false;
  return false;
}

async function ensureToken() {
  if (state.token) {
    // 校验令牌是否仍有效，同时刷新活动任务列表
    try {
      const r = await xzdFetch(`http://127.0.0.1:${state.port}/status`, {
        headers: { 'X-XZD-Token': state.token }
      });
      if (r.ok) {
        const j = await r.json().catch(() => ({}));
        state.tasks = j.tasks || [];
        state.connected = true;
        return true;
      }
    } catch (e) { /* 端口可能变了 */ }
    state.token = '';
  }
  return await probePort();
}

async function fetchHistory(limit = 100) {
  const ok = await ensureToken();
  if (!ok) { state.history = []; return; }
  try {
    const r = await xzdFetch(`http://127.0.0.1:${state.port}/history?limit=${limit}&token=${state.token}`);
    const j = await r.json().catch(() => ({}));
    state.history = j.items || [];
  } catch (e) { state.history = []; }
}

async function sendToDownloader(payload) {
  const ok = await ensureToken();
  if (!ok) return { ok: false, error: '未检测到小助下载器，请先启动它' };
  try {
    const r = await xzdFetch(`http://127.0.0.1:${state.port}${payload.path}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-XZD-Token': state.token
      },
      body: JSON.stringify(payload.body)
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) {           // 令牌失效 → 重新握手一次
      state.token = '';
      if (await probePort()) return await sendToDownloader(payload);
    }
    return j;
  } catch (e) {
    state.connected = false;
    return { ok: false, error: '无法连接下载器：' + e.message };
  }
}

// ------------------------- 消息路由 -------------------------
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    switch (msg && msg.cmd) {
      case 'getItems': {
        const tabId = msg.tabId != null ? msg.tabId
          : (sender.tab ? sender.tab.id : -1);
        const store = state.byTab[tabId] || { items: [], page: '' };
        const ok = await ensureToken();
        reply({ ok: true, items: store.items, page: store.page,
                connected: ok, port: state.port, tasks: state.tasks });
        break;
      }
      case 'getTasks': {
        const ok = await ensureToken();
        reply({ ok: true, connected: ok, port: state.port, tasks: state.tasks });
        break;
      }
      case 'getHistory': {
        await fetchHistory(msg.limit || 100);
        reply({ ok: true, items: state.history });
        break;
      }
      case 'openFile': {
        const tid = msg.tid || '';
        const mode = msg.mode || 'file';   // file | folder
        const ok = await ensureToken();
        if (!ok) { reply({ ok: false, error: '未连接下载器' }); break; }
        try {
          const r = await xzdFetch(`http://127.0.0.1:${state.port}/open`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-XZD-Token': state.token },
            body: JSON.stringify({ tid, mode })
          });
          const j = await r.json().catch(() => ({}));
          reply(j);
        } catch (e) {
          reply({ ok: false, error: e.message });
        }
        break;
      }
      case 'addFound': {   // 内容脚本发现的媒体（js 里拼接的地址等）
        const tabId = sender.tab ? sender.tab.id : msg.tabId;
        const store = tabStore(tabId);
        let n = 0;
        (msg.items || []).forEach((it) => {
          if (record(tabId, it.url, {
            type: it.type,
            filename: it.filename,
            from: 'dom',
            frameId: sender.frameId != null ? sender.frameId : null,
            fromFrame: !!it.fromFrame
          })) n++;
        });
        reply({ ok: true, added: n, total: store.items.length });
        break;
      }
      case 'download': {
        const page = msg.page || '';
        const r = await sendToDownloader({
          path: '/add',
          body: {
            url: msg.url,
            filename: msg.filename || '',
            referer: msg.referer || page,
            cookie: msg.cookie || '',
            kind: msg.kind || 'auto'
          }
        });
        reply(r);
        break;
      }
      case 'downloadBatch': {
        const r = await sendToDownloader({
          path: '/batch',
          body: { items: msg.items || [], page: msg.page || '' }
        });
        reply(r);
        break;
      }
      case 'scanPage': {   // 让下载器代为分析网页
        const r = await sendToDownloader({
          path: '/scan?url=' + encodeURIComponent(msg.url) + '&token=' + state.token,
          body: null
        });
        reply(r);
        break;
      }
      case 'probe': {
        const ok = await probePort();
        reply({ ok, port: state.port, connected: ok });
        break;
      }
      case 'getSettings': {
        reply({ ok: true, takeover: state.takeover, port: state.port,
                connected: state.connected });
        break;
      }
      case 'setTakeover': {
        state.takeover = ['off', 'media', 'all'].includes(msg.mode) ? msg.mode : 'all';
        saveState();
        reply({ ok: true, takeover: state.takeover });
        break;
      }
      case 'wake': {
        // 手动唤起下载器（扩展弹窗按钮）
        const okProbe = await probePort();
        if (okProbe) { reply({ ok: true, port: state.port, already: true }); break; }
        await wakeDownloader();
        let ok = false;
        for (let i = 0; i < 30; i++) {
          await new Promise((res) => setTimeout(res, 500));
          if (await probePort()) { ok = true; break; }
        }
        reply(ok ? { ok: true, port: state.port } : { ok: false, error: '启动超时' });
        break;
      }
      case 'clear': {
        const tabId = msg.tabId;
        if (tabId != null) {
          state.byTab[tabId] = { items: [], page: (state.byTab[tabId] || {}).page || '' };
          updateBadge(tabId);
          saveState();
        }
        reply({ ok: true });
        break;
      }
      default:
        reply({ ok: false, error: 'unknown cmd' });
    }
  })();
  return true;   // 异步回复
});

// ------------------------- 接管浏览器下载（IDM 的核心体验） -------------------------
// 浏览器发起下载 → 立即取消 → 转交本机下载器，实现多线程满速。
// 模式：off 不接管 / media 只接管音视频与压缩包 / all 全部接管（默认）

const TAKEOVER_EXT = [
  'm3u8', 'mpd', 'mp4', 'webm', 'mkv', 'flv', 'mov', 'avi', 'ts', 'm4v',
  'mp3', 'm4a', 'aac', 'flac', 'wav', 'ogg', 'opus', 'wma',
  'zip', 'rar', '7z', 'tar', 'gz', 'xz', 'iso',
  'exe', 'msi', 'apk', 'pdf', 'dmg', 'pkg'
];

// 这些一定不接管（浏览器自身协议、无法转交的临时流、本地页面）
const NEVER_TAKEOVER_SCHEME = [
  'blob:', 'data:', 'filesystem:', 'chrome:', 'chrome-extension:',
  'edge:', 'extension:', 'about:', 'devtools:', 'view-source:'
];

// 这些一定不接管
function neverTakeover(url, mime) {
  if (!url) return true;
  const low = url.toLowerCase();
  // 非 http(s) 一律不接管（blob/data 无法转交给外部程序）
  if (!/^https?:/i.test(url)) return true;
  for (const s of NEVER_TAKEOVER_SCHEME) {
    if (low.startsWith(s)) return true;
  }
  const { ext } = getExt(url);
  // 网页本体不接管（否则会破坏正常浏览）
  if (['html', 'htm', 'php', 'asp', 'aspx', 'jsp', 'shtml', ''].includes(ext)) return true;
  if (mime && (mime.includes('text/html') || mime.includes('application/json') ||
               mime.includes('text/plain'))) return true;
  // 本机地址不接管
  if (/^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])/i.test(url)) return true;
  return false;
}

function shouldTakeover(url, mime) {
  if (state.takeover === 'off') return false;
  if (neverTakeover(url, mime)) return false;
  if (state.takeover === 'all') return true;
  const { ext } = getExt(url);
  return TAKEOVER_EXT.includes(ext);
}

let takeoverGuard = new Set();   // 防重入：同一下载已被处理

chrome.downloads.onCreated.addListener(async (item) => {
  const url = item.finalUrl || item.url;
  if (!shouldTakeover(url, item.mime || '')) return;
  const key = item.id + '|' + url;
  if (takeoverGuard.has(key)) return;
  takeoverGuard.add(key);
  setTimeout(() => takeoverGuard.delete(key), 8000);

  // 先取消浏览器自己的下载
  try {
    await chrome.downloads.cancel(item.id);
    await chrome.downloads.erase({ id: item.id });
  } catch (e) { /* 可能已完成，忽略 */ }

  // 转交下载器
  const filename = item.filename ? item.filename.split(/[\\/]/).pop() : '';
  const tabId = item.tabId;
  const store = tabId >= 0 ? state.byTab[tabId] : null;
  const referer = (store && store.page) || '';

  const r = await ensureSend({
    path: '/add',
    body: {
      url: url,
      filename: filename || '',
      referer: referer,
      cookie: item.cookie || '',
      kind: 'auto'
    }
  }, url);

  // 记录到嗅探列表，便于用户在面板看到
  if (tabId >= 0) {
    record(tabId, url, { filename: filename, referer: referer, from: 'takeover' });
  }
  notifyUser(r && r.ok ? '已转交下载' : '转交失败',
             r && r.ok ? (filename || url) : ((r && r.error) || '请确认下载器正在运行'));
});

// ------------------------- 自动唤起下载器 -------------------------
// 若下载器未运行，通过注册的 xzd:// 协议把它静默拉起来。
// 技术要点：Chrome/Edge 的扩展不能直接用 tabs.create 打开自定义协议，
// 必须由「网页上下文」发起导航，因此在当前活动标签页注入一次导航。

async function wakeDownloader() {
  try {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    let tab = tabs && tabs[0];
    // 活动页不是普通网页时，找一个可用的网页标签
    if (!tab || !/^https?:/i.test(tab.url || '')) {
      const all = await chrome.tabs.query({ url: ['http://*/*', 'https://*/*'] });
      tab = all && all[0];
    }
    if (!tab || !tab.id) return false;
    await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: false },
      func: () => {
        // 在网页里创建一个隐藏 iframe 指向自定义协议：
        // 浏览器会调起协议处理器，且不会让当前页面跳走
        const f = document.createElement('iframe');
        f.style.display = 'none';
        f.src = 'xzd://show';
        document.documentElement.appendChild(f);
        setTimeout(() => { try { f.remove(); } catch (e) {} }, 1500);
      }
    });
    return true;
  } catch (e) {
    // 备用方案：直接尝试（部分环境允许）
    try {
      await chrome.tabs.create({ url: 'xzd://show', active: false });
      return true;
    } catch (e2) {
      return false;
    }
  }
}

/** 发送失败时尝试唤起下载器并重试一次 */
async function ensureSend(payload, retryUrl) {
  let r = await sendToDownloader(payload);
  if (r && r.ok) return r;

  // 未连接 → 尝试唤起
  const connected = await probePort();
  if (!connected) {
    await wakeDownloader();
    // 等待下载器就绪（最多 15 秒）
    for (let i = 0; i < 30; i++) {
      await new Promise((res) => setTimeout(res, 500));
      if (await probePort()) {
        r = await sendToDownloader(payload);
        if (r && r.ok) return r;
        break;
      }
    }
    if (!r || !r.ok) {
      return { ok: false, error: '下载器未能启动，请手动打开「小助下载器」' };
    }
  }
  return r;
}

// ------------------------- 轻提示 -------------------------
function notifyUser(title, message) {
  try {
    chrome.notifications.create('', {
      type: 'basic',
      iconUrl: 'icon128.png',
      title: '小助下载器 · ' + title,
      message: (message || '').slice(0, 200),
      silent: true
    }, () => void chrome.runtime.lastError);
  } catch (e) { /* ignore */ }
}

// ------------------------- 右键菜单 -------------------------
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: 'xzd-download',
      title: '用小助下载器下载此链接',
      contexts: ['link', 'video', 'audio']
    });
    chrome.contextMenus.create({
      id: 'xzd-scan',
      title: '用小助下载器分析本页媒体',
      contexts: ['page']
    });
    chrome.contextMenus.create({
      id: 'xzd-video',
      title: '用小助下载器下载此视频',
      contexts: ['video', 'audio']
    });
    // 下载历史记录右键菜单（父菜单）
    chrome.contextMenus.create({
      id: 'xzd-history',
      title: '小助下载器下载记录',
      contexts: ['action']
    });
    chrome.contextMenus.create({
      id: 'xzd-history-open',
      parentId: 'xzd-history',
      title: '打开最近的下载文件',
      contexts: ['action']
    });
    chrome.contextMenus.create({
      id: 'xzd-history-folder',
      parentId: 'xzd-history',
      title: '打开文件所在目录',
      contexts: ['action']
    });
  });
  loadState();
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === 'xzd-download') {
    await ensureSend({
      path: '/add',
      body: { url: info.linkUrl, referer: tab ? tab.url : '', kind: 'auto' }
    }, info.linkUrl);
  } else if (info.menuItemId === 'xzd-video') {
    await ensureSend({
      path: '/add',
      body: { url: info.srcUrl, referer: tab ? tab.url : '', kind: 'auto' }
    }, info.srcUrl);
  } else if (info.menuItemId === 'xzd-scan') {
    const r = await sendToDownloader({
      path: '/scan?url=' + encodeURIComponent(tab.url) + '&token=' + state.token,
      body: null
    });
    if (r && r.ok && r.items && r.items.length && tab) {
      for (const it of r.items.slice(0, 30)) {
        record(tab.id, it.url, { type: it.type, filename: it.filename, from: 'scan' });
      }
    }
  } else if (info.menuItemId === 'xzd-history-open' || info.menuItemId === 'xzd-history-folder') {
    await fetchHistory(1);
    const it = state.history[0];
    if (!it) {
      notifyUser('没有下载记录', '尚未完成过任何下载');
      return;
    }
    const mode = info.menuItemId === 'xzd-history-open' ? 'file' : 'folder';
    const r = await ensureToken();
    if (!r) { notifyUser('未连接', '请启动小助下载器'); return; }
    try {
      const resp = await xzdFetch(`http://127.0.0.1:${state.port}/open`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-XZD-Token': state.token },
        body: JSON.stringify({ tid: it.tid, mode })
      });
      const j = await resp.json().catch(() => ({}));
      notifyUser(j.ok ? '已打开' : '打开失败', j.ok ? it.filename : (j.error || '未知错误'));
    } catch (e) {
      notifyUser('打开失败', e.message);
    }
  }
});

// 启动时恢复状态
loadState().then(() => probePort());