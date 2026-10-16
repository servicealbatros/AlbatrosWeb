/* Keep authenticated dashboards fresh without navigating or replacing forms. */
(function (root) {
  'use strict';
  function start({refresh, indicatorId = 'liveStatus', interval = 3000, timeout = 12000}) {
    let timer, running = false, queued = false, stopped = false, controller;
    const doc = root.document;
    function show(healthy) {
      const el = doc.getElementById(indicatorId);
      if (!el) return;
      el.textContent = healthy ? '● Live · ' + new Date().toLocaleTimeString('ro-RO') : '● Reconectare automată…';
      el.style.color = healthy ? '#15803d' : '#b45309';
      el.style.background = healthy ? '#f0fdf4' : '#fffbeb';
      el.style.borderRadius = '5px';
      el.style.padding = '4px 8px';
      el.style.display = 'inline-block';
      el.dataset.state = healthy ? 'connected' : 'reconnecting';
    }
    async function tick() {
      if (stopped) return;
      if (running) { queued = true; return; }
      root.clearTimeout(timer);
      if (doc.hidden || root.navigator?.onLine === false) {
        if (root.navigator?.onLine === false) show(false);
        timer = root.setTimeout(tick, interval);
        return;
      }
      running = true;
      controller = new AbortController();
      const deadline = root.setTimeout(() => controller.abort(), timeout);
      try {
        await refresh(controller.signal);
        if (!stopped) show(true);
      } catch (_) {
        if (!stopped) show(false);
      } finally {
        root.clearTimeout(deadline);
        running = false;
        if (!stopped) timer = root.setTimeout(tick, queued ? 0 : interval);
        queued = false;
      }
    }
    const wake = () => { if (!doc.hidden) void tick(); };
    const offline = () => { show(false); controller?.abort(); };
    doc.addEventListener('visibilitychange', wake);
    root.addEventListener('focus', wake);
    root.addEventListener('online', wake);
    root.addEventListener('offline', offline);
    void tick();
    return {refresh: tick, stop() {
      stopped = true;
      root.clearTimeout(timer);
      controller?.abort();
      doc.removeEventListener('visibilitychange', wake);
      root.removeEventListener('focus', wake);
      root.removeEventListener('online', wake);
      root.removeEventListener('offline', offline);
    }};
  }
  root.AlbatrosLive = {start};
})(typeof window !== 'undefined' ? window : globalThis);
