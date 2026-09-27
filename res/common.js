(function (w) {
  const SVG_ATTRS = 'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"';
  const ICON_MOON = '<svg id="themeIconDark" ' + SVG_ATTRS + '><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
  const ICON_SUN = '<svg id="themeIconLight" ' + SVG_ATTRS + ' style="display:none"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>';
  const ICON_EYE = '<svg ' + SVG_ATTRS + '><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
  const ICON_EYE_OFF = '<svg ' + SVG_ATTRS + '><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';

  w.applyTheme = function () {
    const theme = w.theme || localStorage.getItem('theme') || 'dark';
    w.theme = theme;
    document.documentElement.setAttribute('data-theme', theme);
    const btn = document.getElementById('themeBtn');
    if (btn && !document.getElementById('themeIconDark')) btn.innerHTML = ICON_MOON + ICON_SUN;
    const dark = document.getElementById('themeIconDark');
    const light = document.getElementById('themeIconLight');
    if (dark && light) { dark.style.display = theme === 'dark' ? 'block' : 'none'; light.style.display = theme === 'light' ? 'block' : 'none'; }
  };
  w.toggleTheme = function () { w.theme = (w.theme || 'dark') === 'dark' ? 'light' : 'dark'; localStorage.setItem('theme', w.theme); w.applyTheme(); };
  w.toast = function (msg, type = 'success') {
    document.querySelector('.toast')?.remove();
    const el = document.createElement('div'); el.className = 'toast ' + type; el.textContent = msg;
    el.setAttribute('role', 'alert'); el.setAttribute('aria-live', 'polite'); el.onclick = () => el.remove(); document.body.appendChild(el);
    setTimeout(() => el.remove(), 2500);
  };
  w.api = async function (method, path, body) {
    const cfg = w.apiConfig || {};
    const str = (v) => (typeof v === 'function' ? v() : v);
    if (cfg.guard && cfg.guard()) {
      if (cfg.guardMessage) w.toast(str(cfg.guardMessage), 'error');
      return null;
    }
    const headers = Object.assign({}, str(cfg.headers));
    const opts = { method: method, credentials: 'same-origin', headers: headers };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    let r;
    try {
      const base = String(str(cfg.baseUrl) || '').replace(/\/+$/, '');
      r = await fetch(base + path, opts);
    } catch (e) {
      if (cfg.networkError === 'toast') { w.toast(str(cfg.networkMessage) || 'Network error', 'error'); return null; }
      throw e;
    }
    if (r.status === 204) return { success: true, status: 204 };
    if (r.status === 401 && cfg.on401) { cfg.on401(); return null; }
    let data = {};
    try { data = await r.json(); } catch (_) { data = { success: false, msg: r.statusText || 'Invalid server response' }; }
    return Object.assign({}, data, { status: r.status });
  };
  w.fmtBytes = function (b) {
    if (!Number.isFinite(b)) return 'N/A';
    if (b <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(Math.floor(Math.log(b) / Math.log(1000)), units.length - 1);
    return (b / Math.pow(1000, i)).toFixed(i > 1 ? 2 : 0) + ' ' + units[i];
  };
  w.escapeHtml = function (s) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); };
  w.togglePass = function (id, btn) {
    const input = document.getElementById(id);
    input.type = input.type === 'password' ? 'text' : 'password';
    btn.innerHTML = input.type === 'text' ? ICON_EYE_OFF : ICON_EYE;
  };
  w.initPassToggles = function () {
    document.querySelectorAll('.pass-toggle').forEach(btn => {
      if (!btn.querySelector('svg')) btn.innerHTML = ICON_EYE;
    });
  };
  w.confirmDialog = function (message, title, opts) {
    opts = opts || {};
    const withInput = !!opts.input;
    return new Promise(resolve => {
      const overlay = document.getElementById('modalOverlay');
      const okBtn = document.getElementById('modalOk');
      const cancelBtn = document.getElementById('modalCancel');
      const input = document.getElementById('modalInput');
      const submit = () => close(withInput ? (input && input.value) || null : true);
      const dismiss = () => close(withInput ? null : false);
      document.getElementById('modalTitle').textContent = title || '';
      document.getElementById('modalMsg').textContent = message;
      okBtn.textContent = opts.okText || 'OK';
      cancelBtn.textContent = opts.cancelText || 'Cancel';
      if (input) {
        input.value = '';
        input.style.display = withInput ? 'block' : 'none';
        if (withInput) input.placeholder = opts.inputPlaceholder || '';
      }
      const close = (val) => {
        overlay.classList.remove('show');
        okBtn.onclick = null;
        cancelBtn.onclick = null;
        overlay.onclick = null;
        document.removeEventListener('keydown', onKey);
        resolve(val);
      };
      const onKey = (e) => {
        if (e.key === 'Escape') dismiss();
        else if (e.key === 'Enter') submit();
      };
      okBtn.onclick = submit;
      cancelBtn.onclick = dismiss;
      overlay.onclick = (e) => { if (e.target === overlay) dismiss(); };
      document.addEventListener('keydown', onKey);
      overlay.classList.add('show');
      if (withInput && input) input.focus(); else cancelBtn.focus();
    });
  };
  w.fmtUptime = function (seconds) {
    if (seconds == null || isNaN(seconds)) return 'N/A';
    seconds = Math.floor(seconds);
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (d > 0) return d + 'd ' + h + 'h ' + m + 'm';
    if (h > 0) return h + 'h ' + m + 'm ' + s + 's';
    return m + 'm ' + s + 's';
  };
  w.fmtTime = function (ts, t) {
    t = t || function (k) { return k; };
    if (!ts) return t('unlimited');
    const d = new Date(ts * 1000);
    if (d.getTime() < Date.now()) return t('expired');
    const diff = d.getTime() - Date.now();
    const days = Math.floor(diff / 86400000);
    if (days > 0) return days + t('d_left');
    return Math.floor(diff / 3600000) + t('h_left');
  };
})(window);
