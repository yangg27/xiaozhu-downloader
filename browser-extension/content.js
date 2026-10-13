/**
 * 小助下载器 · 内容脚本
 * =======================
 * 在网页里：
 *   1. 扫描 <video>/<audio>/<source> 标签与内嵌播放器配置，把媒体地址报给后台
 *   2. 右下角显示一个悬浮按钮，点开是当前页嗅探到的媒体列表，一键发送下载
 * 完全本地运行，不采集、不上传任何数据。
 */

(function () {
  if (window.__XZD_INJECTED__) return;
  window.__XZD_INJECTED__ = true;

  const PANEL_ID = 'xzd-float-panel';

  // ------------------------- 页面媒体扫描 -------------------------
  function scanDom() {
    const out = [];
    const push = (u, type) => {
      if (!u) return;
      try {
        u = new URL(u, location.href).href;
      } catch (e) { return; }
      if (!/^https?:/i.test(u)) return;
      out.push({ url: u, type: type });
    };
    document.querySelectorAll('video, audio').forEach((el) => {
      if (el.currentSrc) push(el.currentSrc, 'video');
      if (el.src) push(el.src, 'video');
      el.querySelectorAll('source').forEach((s) => push(s.src, 'video'));
    });
    document.querySelectorAll('source').forEach((s) => push(s.src, 'video'));
    // 播放器常见配置字段
    const html = document.documentElement.innerHTML;
    const patterns = [
      /"(?:url|src|file|playUrl|videoUrl|video_url|hlsUrl|m3u8)"\s*:\s*"([^"]{10,600})"/gi,
      /['"]([^'"\s]{10,600}\.m3u8(?:\?[^'"\s]*)?)['"]/gi,
      /['"]([^'"\s]{10,600}\.(?:mp4|mp3|m4a|flv)(?:\?[^'"\s]*)?)['"]/gi
    ];
    for (const re of patterns) {
      let m;
      let guard = 0;
      while ((m = re.exec(html)) !== null && guard++ < 60) {
        const u = m[1].replace(/\\\//g, '/');
        if (/\.(m3u8|mpd)(\?|$)/i.test(u)) push(u, 'hls');
        else if (/\.(mp4|webm|mkv|flv|mov|ts)(\?|$)/i.test(u)) push(u, 'video');
        else if (/\.(mp3|m4a|aac|flac|wav|ogg)(\?|$)/i.test(u)) push(u, 'audio');
        else push(u, 'media');
      }
    }
    // 去重
    const seen = new Set();
    return out.filter((it) => {
      if (seen.has(it.url)) return false;
      seen.add(it.url);
      return true;
    });
  }

  function reportDom() {
    const items = scanDom();
    if (!items.length) return;
    try {
      chrome.runtime.sendMessage({ cmd: 'addFound', items: items.slice(0, 40) }, () => void 0);
    } catch (e) { /* 扩展已重载 */ }
  }

  // ------------------------- 悬浮按钮与面板 -------------------------
  let panel = null;
  let btn = null;

  function fmtSize(n) {
    if (!n) return '';
    const u = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(1) + ' ' + u[i];
  }

  function createButton() {
    btn = document.createElement('div');
    btn.id = 'xzd-float-btn';
    btn.title = '小助下载器 · 查看本页媒体';
    btn.innerHTML = '<span class="xzd-ico">⬇</span><span class="xzd-cnt" id="xzd-cnt"></span>';
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      e.preventDefault();
      togglePanel();
    });
    document.documentElement.appendChild(btn);
  }

  function refreshCount() {
    try {
      chrome.runtime.sendMessage({ cmd: 'getItems' }, (res) => {
        if (!res || !res.ok) return;
        const n = (res.items || []).length;
        const c = document.getElementById('xzd-cnt');
        if (c) c.textContent = n ? String(n) : '';
        if (btn) btn.classList.toggle('xzd-nonempty', n > 0);
      });
    } catch (e) { /* ignore */ }
  }

  function togglePanel() {
    if (panel) {
      closePanel();
      return;
    }
    openPanel();
  }

  function closePanel() {
    if (panel) {
      panel.remove();
      panel = null;
    }
  }

  function openPanel() {
    panel = document.createElement('div');
    panel.id = PANEL_ID;
    panel.innerHTML = `
      <div class="xzd-head">
        <div class="xzd-title">小助下载器 · 本页媒体</div>
        <div class="xzd-acts">
          <button class="xzd-btn xzd-mini xzd-scan">分析页面</button>
          <button class="xzd-btn xzd-mini xzd-clear">清空</button>
          <button class="xzd-btn xzd-mini xzd-x">✕</button>
        </div>
      </div>
      <div class="xzd-tip">正在读取…</div>
      <div class="xzd-list"></div>
      <div class="xzd-foot">
        <span class="xzd-status"></span>
        <button class="xzd-btn xzd-primary xzd-dlall">全部下载</button>
      </div>`;
    document.documentElement.appendChild(panel);
    panel.addEventListener('click', (e) => e.stopPropagation());
    panel.querySelector('.xzd-x').addEventListener('click', closePanel);
    panel.querySelector('.xzd-clear').addEventListener('click', () => {
      chrome.runtime.sendMessage({ cmd: 'clear' }, () => { render(); refreshCount(); });
    });
    panel.querySelector('.xzd-scan').addEventListener('click', () => {
      const st = panel.querySelector('.xzd-status');
      st.textContent = '正在分析页面…';
      chrome.runtime.sendMessage({ cmd: 'scanPage', url: location.href }, (r) => {
        render();
        refreshCount();
        st.textContent = r && r.ok
          ? `分析完成，共 ${(r.items || []).length} 个`
          : ('分析失败：' + ((r && r.error) || '未知错误'));
      });
    });
    panel.querySelector('.xzd-dlall').addEventListener('click', () => {
      chrome.runtime.sendMessage({ cmd: 'getItems' }, (res) => {
        if (!res || !res.ok || !(res.items || []).length) return;
        const items = res.items.map((it) => ({
          url: it.url, filename: it.filename, referer: it.referer || location.href,
          kind: it.type === 'hls' ? 'hls' : 'auto'
        }));
        chrome.runtime.sendMessage({
          cmd: 'downloadBatch', items: items, page: location.href
        }, (r) => {
          const st = panel.querySelector('.xzd-status');
          st.textContent = r && r.ok ? `已发送 ${items.length} 个任务到下载器` :
            ('发送失败：' + ((r && r.error) || '请先启动小助下载器'));
        });
      });
    });
    render();
  }

  function render() {
    if (!panel) return;
    chrome.runtime.sendMessage({ cmd: 'getItems' }, (res) => {
      if (!panel) return;
      const dev = panel.querySelector('.xzd-tip');
      const list = panel.querySelector('.xzd-list');
      if (!res || !res.ok) {
        dev.textContent = '无法读取';
        return;
      }
      const items = res.items || [];
      dev.textContent = res.connected
        ? `已连接下载器（127.0.0.1:${res.port}） · 共 ${items.length} 个媒体`
        : '未检测到下载器，请先启动「小助下载器」';
      dev.classList.toggle('xzd-warn', !res.connected);
      list.innerHTML = '';
      if (!items.length) {
        list.innerHTML = '<div class="xzd-empty">暂未嗅探到媒体<br>' +
          '<span>播放一下视频，或点右上「分析页面」</span></div>';
        return;
      }
      items.forEach((it) => {
        const row = document.createElement('div');
        row.className = 'xzd-row';
        const tag = it.type === 'hls' ? 'HLS' : it.type === 'video' ? '视频'
          : it.type === 'audio' ? '音频' : it.type === 'file' ? '文件' : '媒体';
        row.innerHTML = `<span class="xzd-tag xzd-tag-${it.type}">${tag}</span>` +
          `<span class="xzd-name" title="${it.url.replace(/"/g, '&quot;')}">${it.filename}</span>` +
          `<span class="xzd-size">${fmtSize(it.size)}</span>` +
          `<button class="xzd-btn xzd-primary xzd-one">下载</button>`;
        row.querySelector('.xzd-one').addEventListener('click', (e) => {
          const b = e.target;
          b.textContent = '…';
          chrome.runtime.sendMessage({
            cmd: 'download', url: it.url, filename: it.filename,
            referer: it.referer || location.href,
            kind: it.type === 'hls' ? 'hls' : 'auto'
          }, (r) => {
            b.textContent = r && r.ok ? '已发送' : '失败';
            if (panel) {
              panel.querySelector('.xzd-status').textContent = r && r.ok
                ? '已加入下载队列' : ('失败：' + ((r && r.error) || ''));
            }
            setTimeout(() => { b.textContent = '下载'; }, 2200);
          });
        });
        list.appendChild(row);
      });
    });
  }

  // ------------------------- 启动 -------------------------
  function boot() {
    if (document.getElementById('xzd-float-btn')) return;
    createButton();
    reportDom();
    refreshCount();
    // 动态加载的视频（SPA / 懒加载）延迟再扫几次
    [2500, 6000].forEach((t) => setTimeout(() => { reportDom(); refreshCount(); }, t));
    setTimeout(refreshCount, 1200);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  // 页面内发生播放/切集时刷新计数
  document.addEventListener('play', (e) => {
    const t = e.target;
    if (t && (t.tagName === 'VIDEO' || t.tagName === 'AUDIO')) {
      if (t.currentSrc) {
        try {
          chrome.runtime.sendMessage({
            cmd: 'addFound', items: [{ url: t.currentSrc, type: 'video' }]
          }, () => { refreshCount(); });
        } catch (err) { /* ignore */ }
      }
    }
  }, true);
})();