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
  byTab: {},            // tabId -> { items: [], page: '' }，仅存元数据，不保存浏览内容
  pageUrls: {},         // tabId -> 顶层页面地址，供 Service Worker 唤醒后恢复
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
    const s = await chrome.storage.local.get(['sniff', 'pageUrls', 'token', 'port', 'takeover']);
    state.byTab = s.sniff || {};
    state.pageUrls = s.pageUrls || {};
    // 兼容旧版存储，并保证每个标签页的页面地址可恢复。
    for (const [tabId, store] of Object.entries(state.byTab)) {
      if (store && store.page) state.pageUrls[tabId] = store.page;
    }
    state.token = s.token || '';
    state.port = s.port || DEFAULT_PORT;
    state.takeover = s.takeover || 'all';
  } catch (e) { /* ignore */ }
}
function saveState() {
  try {
    chrome.storage.local.set({
      sniff: state.byTab, pageUrls: state.pageUrls, token: state.token,
      port: state.port, takeover: state.takeover
    });
  } catch (e) { /* ignore */ }
}

function tabStore(tabId) {
  if (!state.byTab[tabId]) state.byTab[tabId] = { items: [], page: '' };
  const store = state.byTab[tabId];
  if (!Array.isArray(store.items)) store.items = [];
  if (!store.page && state.pageUrls[tabId]) store.page = state.pageUrls[tabId];
  return store;
}

function bindPage(tabId, page, reset = false) {
  if (tabId == null || tabId < 0 || !/^https?:/i.test(page || '')) return null;
  const store = tabStore(tabId);
  const old = store.page || state.pageUrls[tabId] || '';
  if (reset || (old && old !== page)) store.items = [];
  store.page = page;
  state.pageUrls[tabId] = page;
  updateBadge(tabId);
  return old !== page ? null : store;
}

function senderPage(sender) {
  return sender && sender.tab && /^https?:/i.test(sender.tab.url || '')
    ? sender.tab.url : '';
}

function matchesPage(tabId, page) {
  if (!page) return true;
  const store = tabStore(tabId);
  const canonical = (u) => { try { const x = new URL(u); x.hash = ''; return x.href; } catch (e) { return u; } };
  return canonical(store.page || state.pageUrls[tabId] || '') === canonical(page);
}

async function scanAndRecord(tabId, url) {
  if (tabId == null || tabId < 0 || !/^https?:/i.test(url || '')) {
    return { ok: false, error: '无法确定要分析的当前页面' };
  }
  const r = await sendToDownloader({
    path: '/scan?url=' + encodeURIComponent(url) + '&token=' + state.token,
    method: 'GET'
  });
  if (r && r.ok && matchesPage(tabId, url)) {
    bindPage(tabId, url);
    for (const it of (r.items || []).slice(0, 60)) {
      record(tabId, it.url, {
        type: it.type, filename: it.filename, referer: url, from: 'scan', page: url
      });
    }
    r.added = tabStore(tabId).items.length;
  }
  return r;
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
  if (tabId == null || tabId < 0 || !/^https?:/i.test(url || '')) return false;
  if (opts.page && !matchesPage(tabId, opts.page)) return false;
  const store = tabStore(tabId);
  if (store.items.some((it) => it.url === url)) return false;
  const detected = isMediaUrl(url);
  const info = detected || { type: opts.type || 'media', ext: getExt(url).ext };
  const page = opts.page || opts.referer || store.page || state.pageUrls[tabId] || '';
  const item = {
    url,
    // URL 扩展名是硬证据：<audio src="x.mp3"> 曾被内容脚本标成 video，
    // 若这里仍优先 opts.type，就会把音频误显示成视频。
    type: detected ? detected.type : (opts.type || 'media'),
    ext: info.ext || '',
    filename: opts.filename || filenameOf(url),
    size: opts.size || null,
    page,
    referer: opts.referer || page,
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
    if (!store.page) bindPage(details.tabId, details.documentUrl || details.initiator || '');
    // Chrome 的 initiator 通常只有 origin，不能覆盖已知的完整页面地址。
    const page = store.page || details.documentUrl || details.initiator || '';
    record(details.tabId, url, {
      type: hit ? hit.type : (ctype.startsWith('video') ? 'video'
            : ctype.startsWith('audio') ? 'audio' : 'media'),
      size: sizeOf(details.responseHeaders),
      page,
      referer: page,
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
  delete state.pageUrls[tabId];
  saveState();
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  const page = changeInfo.url || (tab && tab.url) || '';
  if (changeInfo.status === 'loading' || changeInfo.url) {
    // 地址变化（含 SPA）清空旧页面结果；普通 loading 不丢掉当前页媒体。
    bindPage(tabId, page, !!changeInfo.url);
    saveState();
  } else if (changeInfo.status === 'complete') {
    bindPage(tabId, page);
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
      method: payload.method || 'POST',
      headers: {
        ...(payload.method === 'GET' ? {} : { 'Content-Type': 'application/json' }),
        'X-XZD-Token': state.token
      },
      ...(payload.method === 'GET' ? {} : { body: JSON.stringify(payload.body) })
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
        const store = tabStore(tabId);
        if (msg.page) bindPage(tabId, msg.page);
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
        // ★ v3.1.10 修复（真实 Chrome 扩展复测 EXT-03 失败）：
        //   子帧（iframe）上报时 msg.page 是**子帧自己的 URL**，与父页面不同。
        //   旧代码无条件 bindPage(tabId, page)，而 bindPage 在 page 变化时会
        //   store.items = [] 清空整个标签页列表 —— 于是：
        //     父页面 DOM 上报 → 清空 → 写入父页面 3 项
        //     子帧 DOM 上报   → 又清空 → 写入子帧音频
        //     父页面再上报    → 再清空 → 子帧音频丢失
        //   实测证据（run-v310-a）：fixture 页含 <iframe src=".../frame">，
        //   帧内 <audio src="frame-audio.mp3">；popup 最终只列出
        //   network.mp4 / playlist.m3u8 / dom.mp4，frame-audio.mp3 永远不出现
        //   （夹具日志显示 /frame 确实被加载过）。子帧资源本就属于当前标签页，
        //   不应触发页面切换语义。
        const isSubFrame = sender && sender.frameId != null && sender.frameId !== 0;
        // 子帧：沿用既有页面绑定，绝不改写 page、绝不清空列表。
        // 主帧：维持原有页面隔离语义（换页即清空）。
        if (!isSubFrame) {
          const page = msg.page || senderPage(sender);
          if (tabId == null || tabId < 0 || !page) {
            reply({ ok: false, error: '无法识别媒体来源页面' });
            break;
          }
          bindPage(tabId, page);
        } else if (tabId == null || tabId < 0) {
          reply({ ok: false, error: '无法识别媒体来源页面' });
          break;
        }
        const store = tabStore(tabId);
        // 归属页面：子帧用当前标签页已绑定的页面作 referer，
        // 避免把 iframe 地址写成下载 Referer（会导致防盗链站 403）。
        const ownerPage = store.page || state.pageUrls[tabId] || senderPage(sender) || '';
        let n = 0;
        (Array.isArray(msg.items) ? msg.items : []).slice(0, 60).forEach((it) => {
          if (record(tabId, it.url, {
            type: it.type,
            filename: it.filename,
            page: ownerPage,
            referer: ownerPage,
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
      case 'scanPage': {   // 让下载器代为分析网页 → 结果必须进入当前标签页嗅探列表
        const tabId = msg.tabId != null ? msg.tabId
          : (sender.tab ? sender.tab.id : -1);
        // ★ v3.1.10：此前只把响应回给调用方，没有写进本标签页列表，
        //   于是"分析完成，共 N 个媒体"之后列表仍然是空的（用户以为失败）。
        const r = await scanAndRecord(tabId, msg.url || '');
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

/** 接管所需的上下文：页面地址与文件名。
 *
 *  ★ v3.1.10 修复两个真实缺陷（真实 Chrome 156 复现）：
 *    1) downloads.onCreated 早期 item.filename 可能为空 → 请求里 filename:''
 *       下载器只能从 URL 猜名字。这里用 filenameOf() 兜底。
 *    2) item.tabId 可能为 -1（无来源标签页）→ referer 为空，
 *       下载器发出的 Range 请求缺少 Referer，防盗链站点会 403。
 *       这里依次回退：标签页已知页面 → item.referrer。
 */
function takeoverContext(item) {
  const tabId = item.tabId;
  let page = '';
  if (tabId != null && tabId >= 0) {
    page = (state.byTab[tabId] && state.byTab[tabId].page) || state.pageUrls[tabId] || '';
  }
  if (!page && item.referrer && /^https?:/i.test(item.referrer)) page = item.referrer;
  if (!page && item.documentUrl && /^https?:/i.test(item.documentUrl)) page = item.documentUrl;
  const raw = item.filename || '';
  const filename = raw ? raw.split(/[\\/]/).pop() : filenameOf(item.finalUrl || item.url);
  return { tabId, page, filename };
}

chrome.downloads.onCreated.addListener(async (item) => {
  const url = item.finalUrl || item.url;
  if (!shouldTakeover(url, item.mime || '')) return;
  const key = item.id + '|' + url;
  if (takeoverGuard.has(key)) return;
  takeoverGuard.add(key);
  setTimeout(() => takeoverGuard.delete(key), 8000);

  const ctx = takeoverContext(item);

  // ★ v3.1.10 事务修复：先让下载器确认接收，成功后才取消浏览器下载。
  //   旧顺序是"先 cancel + erase，再转交"——一旦转交失败（下载器未启动、
  //   接口报错、唤醒超时），这次下载就凭空消失了，用户两头都拿不到文件。
  const r = await ensureSend({
    path: '/add',
    body: {
      url: url,
      filename: ctx.filename || '',
      referer: ctx.page || '',
      cookie: item.cookie || '',
      kind: 'auto'
    }
  }, url);

  if (r && r.ok) {
    // 下载器已受理 → 此时才取消浏览器自己的下载，避免重复下载与磁盘占用
    //
    // ★ v3.1.10 修复（真实 Chrome 复测 EXT-09 失败定位，真实产品缺陷）：
    //   `cancel` + `erase` 只清**下载记录**，**不删磁盘上已落盘的文件**。
    //   实测证据（run-v310-b，EXT-09）：
    //     chrome.downloads.search({}) 过滤 takeover.zip → 0 条 ✓（记录已抹）
    //     但 runtime.browser_downloads 目录里 takeover.zip 仍在 ✗（文件没删）
    //   后果：一段"已经交给下载器"的下载，磁盘上还留着一份谁也不会再用的
    //   重复文件 —— 与"接管"语义矛盾，用户白占空间。
    //
    //   为什么之前没删成：小文件（夹具 64KB）几乎瞬间下完。而这里的顺序是
    //   `await ensureSend(...)`（一次 HTTP 往返）**之后**才 cancel —— 等回来时
    //   下载已经是 complete，cancel 对已完成的下载无效，文件已经落盘。
    //
    //   修法（顺序很关键，写反就无效）：
    //     ① 先取回 item 状态（此时记录还在，search 查得到，exists 才有值）
    //     ② 若已 complete 且 exists → removeFile 删磁盘文件
    //     ③ 最后才 erase 抹记录（抹完就再也查不到，所以必须放最后）
    //   removeFile 在权限或时序不允许时静默降级：最坏结果是"记录已清、
    //   多留一份文件"，不会中断接管主流程，也不会让用户丢文件。
    try {
      const before = await new Promise((resolve) => {
        chrome.downloads.search({ id: item.id }, (ds) => resolve((ds && ds[0]) || null));
      });
      if (before && before.exists) {
        await new Promise((resolve) => {
          try {
            chrome.downloads.removeFile(item.id, () => {
              void chrome.runtime.lastError;   // 不允许删除时静默降级
              resolve();
            });
          } catch (e) { resolve(); }
        });
      }
    } catch (e) { /* 删文件失败不阻塞接管 */ }
    try {
      await chrome.downloads.cancel(item.id);
      await chrome.downloads.erase({ id: item.id });
    } catch (e) { /* 可能已完成，忽略 */ }
  }
  // 转交失败则保留浏览器原始下载（用户不丢文件），提示里说明原因

  // 记录到嗅探列表，便于用户在面板看到
  if (ctx.tabId >= 0) {
    record(ctx.tabId, url, { filename: ctx.filename, referer: ctx.page, from: 'takeover' });
  }
  notifyUser(r && r.ok ? '已转交下载' : '转交失败（已保留浏览器下载）',
             r && r.ok ? (ctx.filename || url) : ((r && r.error) || '请确认下载器正在运行'));
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
    if (tab && tab.id >= 0) {
      const r = await scanAndRecord(tab.id, tab.url || '');
      notifyUser(r && r.ok ? '页面分析完成' : '分析失败',
                 r && r.ok ? `发现 ${(r.items || []).length} 个媒体`
                           : ((r && r.error) || '请确认下载器正在运行'));
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