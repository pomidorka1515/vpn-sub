import { createDialogs } from '../shared/dialogs';
import { escapeHtml, loadingHtml as loading } from '../shared/dom';
import { fmtBytes as bytes, fmtTime } from '../shared/formatting';
import { createI18n } from '../shared/i18n';
import { isRecord } from '../shared/json';
import { createNotifications } from '../shared/notifications';
import { initPassToggles, togglePass } from '../shared/passwords';
import { createLoadGeneration } from '../shared/stale';
import { safeStorage } from '../shared/storage';
import { createTheme } from '../shared/theme';
import { withBusy } from '../shared/busy';
import { readBootstrap } from '../shared/bootstrap';
import { getBwColor } from './data';
import type { Stats } from './data';
import { createDashboardService } from './service';

export function initDashboard(doc: Document = document, win: Window = window): () => void {
  const document = doc;
  const bootstrap = readBootstrap(doc);
  const local = safeStorage(() => win.localStorage);
  const i18n = createI18n(bootstrap, doc, local, safeStorage(() => win.sessionStorage), win.location);
  const { t, applyStaticLang } = i18n;
  const theme = createTheme(doc, local);
  const notifications = createNotifications(doc, t, win.setTimeout.bind(win), win.clearTimeout.bind(win));
  const toast = (message: string, type = 'success') => { if (!disposed) notifications.toast(message, type); };
  const logoutWarning = () => { if (!disposed) notifications.logoutWarning(); };
  const dialogs = createDialogs(doc, t);
  const { confirmDialog } = dialogs;
  const apiBase = bootstrap.prefix + '/webapi';
  const authPage = bootstrap.prefix + '/auth';
  const service = createDashboardService({ baseUrl: apiBase, onUnauthorized: () => { if (!disposed) win.location.href = authPage; }, translate: t, toast, request: win.fetch.bind(win) });
  const { api } = service;
  const loadGen = createLoadGeneration();
  const fmtBytes = (value: number) => bytes(value, t);
  const loadingHtml = () => loading(t);
  const timers = new Set<number>();
  const qrUrls = new Set<string>();
  let disposed = false;
  function schedule(fn: () => void, delay: number): number { const id = win.setTimeout(() => { timers.delete(id); if (!disposed) fn(); }, delay); timers.add(id); return id; }
  function cancelTimer(id: number | undefined): void { if (id !== undefined) { win.clearTimeout(id); timers.delete(id); } }
  function el(id: string): HTMLElement { const node = doc.getElementById(id); if (!node) throw new Error('Missing element #' + id); return node; }
  function input(id: string): HTMLInputElement | HTMLSelectElement { const node = el(id); if (!(node instanceof HTMLInputElement) && !(node instanceof HTMLSelectElement)) throw new Error('Missing input #' + id); return node; }
  function button(id: string): HTMLButtonElement { const node = el(id); if (!(node instanceof HTMLButtonElement)) throw new Error('Missing button #' + id); return node; }
  function getCanvas(id: string): HTMLCanvasElement { const node = el(id); if (!(node instanceof HTMLCanvasElement)) throw new Error('Missing canvas #' + id); return node; }
  let currentStats: Stats | null = null;
  const dirtySettings = { name: false, fp: false };
  let statsLoads = 0;

  async function loadStats() {
    const btn = button('btnRefresh');
    const dash = document.querySelector<HTMLElement>('.dash-content');
    if (!dash) throw new Error('Missing dashboard content');
    ++statsLoads;
    dash.setAttribute('aria-busy', 'true');
    try {
      const current = loadGen('stats');
      await withBusy(btn, async () => {
        const { response: res, stats } = await service.stats();
        if (disposed || !current()) return;
        if (!res) return;
        if (!res.success) { toast(res.msg || t('network_err'), 'error'); return; }
        if (!stats) { toast(t('invalid_response'), 'error'); return; }
        if (currentStats && currentStats.link !== stats.link) {
          ++qrGen;
          el('qrSection').style.display = 'none';
          const canvas = getCanvas('qrCanvas');
          canvas.getContext('2d')?.clearRect(0, 0, canvas.width, canvas.height);
        }
        currentStats = stats;
        renderStats(stats);
      });
    } finally {
      if (--statsLoads === 0) dash.setAttribute('aria-busy', 'false');
    }
  }

  function renderStats(s: Stats) {
    el('topName').textContent = s.displayname || '—';

    const dot = el('statusDot');
    const stxt = el('statusText');
    if (s.online) { dot.className = 'status-dot on'; stxt.textContent = t('online'); }
    else { dot.className = 'status-dot off'; stxt.textContent = t('offline'); }

    const banner = el('disabledBanner');
    const isExpired = s.time && s.time * 1000 < Date.now();
    if (!s.enabled) {
      banner.textContent = t('banner_disabled');
      banner.classList.add('show');
    } else if (isExpired) {
      banner.textContent = t('banner_expired');
      banner.classList.add('show');
    } else {
      banner.classList.remove('show');
    }

    el('subLink').textContent = s.link ? s.link + '&lang=' + i18n.language : '—';

    const expEl = el('statExpiry');
    expEl.textContent = fmtTime(s.time, t);
    expEl.style.color = isExpired ? 'var(--red)' : '';

    const enEl = el('statEnabled');
    if (!s.enabled) {
      enEl.textContent = t('disabled');
      enEl.style.color = 'var(--red)';
    } else {
      enEl.textContent = t('active');
      enEl.style.color = 'var(--green)';
      if (s.wl_enabled) enEl.textContent += t('wl_suffix');
    }

    const bw = s.bandwidth;
    el('statTotal').textContent = fmtBytes(bw.total.upload + bw.total.download);
    el('statTotalSub').textContent = '↑ ' + fmtBytes(bw.total.upload) + '  ↓ ' + fmtBytes(bw.total.download);
    el('statWl').textContent = fmtBytes(bw.wl_total.upload + bw.wl_total.download);
    el('statWlSub').textContent = '↑ ' + fmtBytes(bw.wl_total.upload) + '  ↓ ' + fmtBytes(bw.wl_total.download);

    const limitB = bw.limit * 1000000000;
    const wlLimitB = bw.wl_limit * 1000000000;

    if (limitB > 0) {
      const pct = (bw.monthly / limitB) * 100;
      el('bwMonthlyBar').style.width = Math.min(100, pct) + '%';
      el('bwMonthlyBar').style.background = getBwColor(pct);
      el('bwMonthlyText').textContent = fmtBytes(bw.monthly) + ' / ' + bw.limit + ' ' + t('gb_unit');
    } else {
      el('bwMonthlyBar').style.width = '0%';
      el('bwMonthlyText').textContent = fmtBytes(bw.monthly) + ' / ∞';
    }

    if (wlLimitB > 0) {
      const pct = (bw.wl_monthly / wlLimitB) * 100;
      el('bwWlBar').style.width = Math.min(100, pct) + '%';
      el('bwWlBar').style.background = getBwColor(pct);
      el('bwWlText').textContent = fmtBytes(bw.wl_monthly) + ' / ' + bw.wl_limit + ' ' + t('gb_unit');
    } else {
      el('bwWlBar').style.width = '0%';
      el('bwWlText').textContent = fmtBytes(bw.wl_monthly) + ' / ∞';
    }

    el('infoUuid').textContent = s.uuid || '—';
    el('infoFp').textContent = s.fingerprint || '—';
    el('infoToken').textContent = s.token || '—';

    if (!dirtySettings.name) input('setName').value = s.displayname || '';
    if (!dirtySettings.fp) input('setFp').value = s.fingerprint || '';
  }

  async function loadFingerprints() {
    const res = await api('GET', '/fingerprints');
    if (disposed || !res || !res.success) return;
    const sel = input('setFp');
    const previous = sel.value;
    if (!Array.isArray(res.obj) || !res.obj.every((fp: unknown) => typeof fp === 'string')) return;
    sel.innerHTML = '';
    res.obj.forEach((fp: string) => {
      const o = document.createElement('option');
      o.value = fp; o.textContent = fp;
      sel.appendChild(o);
    });
    sel.value = dirtySettings.fp ? previous : (currentStats?.fingerprint || '');
  }

  function copyLink() {
    const link = el('subLink').textContent;
    if (!link || link === '—') return;
    win.navigator.clipboard.writeText(link).then(() => {
      if (disposed) return;
      const btn = button('btnCopy');
      btn.textContent = t('copied'); btn.classList.add('copied');
      schedule(() => { applyStaticLang(); btn.classList.remove('copied'); }, 1500);
    }).catch(() => {});
  }

  async function applyBonus() {
    if (button('btnApply').disabled) return;
    const code = input('bonusInput').value.trim();
    if (!code) return;
    const msg = el('bonusMsg');
    await withBusy(button('btnApply'), async () => {
      const res = await api('POST', '/bonus', { code });
      if (disposed || !res) return;
      if (res.success) {
        const o = isRecord(res.obj) ? res.obj : {};
        const parts: string[] = [];
        if (o.days) parts.push('+' + o.days + ' ' + t('days'));
        if (o.gb) parts.push('+' + o.gb + ' ' + t('gb_unit'));
        if (o.wl_gb) parts.push('+' + o.wl_gb + ' ' + t('gb_wl'));
        if (o.perma) parts.push(t('bonus_reusable'));
        msg.textContent = parts.join(', ') || t('ok');
        msg.className = 'bonus-msg ok';
        input('bonusInput').value = '';
        loadStats();
      } else { msg.textContent = res.msg || t('error'); msg.className = 'bonus-msg err'; }
    });
  }

  let svt: number | undefined, settingsValidationGen = 0;
  function validateSettingsUsername() {
    cancelTimer(svt);
    const gen = ++settingsValidationGen;
    const v = input('setUser').value.trim();
    const h = el('setUserHint');
    if (!v) { h.textContent = ''; h.className = 'username-hint'; return; }
    svt = schedule(async () => {
      try {
        const res = await api('GET', `/validate?username=${encodeURIComponent(v)}`);
        if (disposed || gen !== settingsValidationGen || input('setUser').value.trim() !== v) return;
        if (!res) return;
        if (res.success && isRecord(res.obj) && typeof res.obj.valid === 'boolean' && typeof res.obj.taken === 'boolean' && typeof res.obj.sanitized === 'string') {
          if (!res.obj.valid) { h.textContent = t('hint_invalid') + res.obj.sanitized; h.className = 'username-hint bad'; }
          else if (res.obj.taken) { h.textContent = t('hint_taken'); h.className = 'username-hint bad'; }
          else { h.textContent = t('hint_ok'); h.className = 'username-hint ok'; }
        }
      } catch {}
    }, 400);
  }

  async function saveSettings() {
    const body: Record<string, string> = {};
    const name = input('setName').value.trim();
    const fp = input('setFp').value;
    const user = input('setUser').value.trim();
    const pass = input('setPass').value;
    const curPass = input('setCurPass').value;
    if (name) body.name = name;
    if (fp) body.fingerprint = fp;
    if (user) body.username = user;
    if (pass) body.password = pass;
    if (!Object.keys(body).length) return;

    const msg = el('settingsMsg');
    if (body.username || body.password) {
      if (!curPass) {
        msg.textContent = t('current_pw_required'); msg.className = 'settings-msg err';
        schedule(() => { msg.textContent = ''; }, 3000);
        return;
      }
      body.current_password = curPass;
    }
    await withBusy(button('btnSave'), async () => {
      const res = await api('POST', '/settings', body, { allow401: res => res.msg === 'Invalid current password' });
      if (disposed || !res) return;
      if (res.success) {
        if (input('setName').value.trim() === name) dirtySettings.name = false;
        if (input('setFp').value === fp) dirtySettings.fp = false;
        msg.textContent = t('saved'); msg.className = 'settings-msg ok';
        input('setUser').value = '';
        ++settingsValidationGen;
        cancelTimer(svt);
        input('setPass').value = '';
        input('setCurPass').value = '';
        el('setUserHint').textContent = '';
        el('setUserHint').className = 'username-hint';
        loadStats();
      } else { msg.textContent = res.status === 401 ? t('invalid_current_pw') : (res.msg || t('error')); msg.className = 'settings-msg err'; }
      schedule(() => { msg.textContent = ''; }, 3000);
    });
  }

  const confirmOpts = () => ({ okText: t('confirm_yes'), cancelText: t('confirm_cancel') });

  async function doReset() {
    if (!(await confirmDialog(t('confirm_reset'), t('confirm_title_reset'), confirmOpts()))) return;
    await withBusy(button('btnReset'), async () => {
      const res = await api('POST', '/reset');
      if (disposed || !res) return;
      if (res.success) { win.location.href = authPage; }
      else toast(res.msg || t('error'), 'error');
    });
  }

  async function doDelete() {
    if (!(await confirmDialog(t('confirm_delete'), t('confirm_title_delete'), confirmOpts()))) return;
    if (!(await confirmDialog(t('confirm_delete2'), t('confirm_title_final'), confirmOpts()))) return;
    const curPass = await confirmDialog(t('current_pw_required'), t('confirm_title_delete'), { input: true, inputPlaceholder: t('current_password'), ...confirmOpts() });
    if (!curPass) return;
    await withBusy(button('btnDelete'), async () => {
      const res = await api('POST', '/delete', { current_password: curPass }, { allow401: res => res.msg === 'Invalid current password' });
      if (disposed || !res) return;
      if (res.success) { win.location.href = authPage; }
      else toast(res.status === 401 ? t('invalid_current_pw') : (res.msg || t('error')), 'error');
    });
  }

  async function doLogout() {
    if (button('btnLogout').disabled) return;
    await withBusy(button('btnLogout'), async () => {
      try {
        const res = await win.fetch(apiBase + '/logout', { method: 'POST', credentials: 'same-origin', cache: 'no-store' });
        const data: unknown = await res.json().catch(() => null);
        if (disposed) return;
        if (res.ok && isRecord(data) && data.success === true) { win.location.href = authPage; return; }
      } catch {}
      try {
        const probe = await win.fetch(apiBase + '/stats', { credentials: 'same-origin', cache: 'no-store' });
        if (disposed) return;
        if (probe.status === 401) { win.location.href = authPage; return; }
      } catch {}
      logoutWarning();
    });
  }

  async function showQrCode() {
    const link = el('subLink').textContent;
    if (!link || link === '—') return;

    let url = apiBase + '/qr?lang=' + i18n.language;
    if (qrMode === 'happ') url += '&happ=1';

    const gen = ++qrGen;
    await withBusy(button('btnQr'), async () => {
      let objectUrl: string | undefined;
      try {
        const res = await win.fetch(url, { method: 'GET', credentials: 'same-origin', cache: 'no-store' });
        if (disposed || gen !== qrGen) return;
        if (!res.ok) throw new Error(t('qr_failed'));
        const blob = await res.blob();
        if (disposed || gen !== qrGen) return;
        objectUrl = URL.createObjectURL(blob);
        qrUrls.add(objectUrl);
        const canvas = getCanvas('qrCanvas');
        const ctx = canvas.getContext('2d');
        const img = new Image();
        await new Promise<void>((resolve, reject) => {
          img.onload = () => {
            if (disposed || gen !== qrGen) { resolve(); return; }
            canvas.width = img.width;
            canvas.height = img.height;
            ctx?.drawImage(img, 0, 0);
            el('qrSection').style.display = 'block';
            resolve();
          };
          img.onerror = () => reject(new Error(t('qr_failed')));
          img.src = objectUrl ?? '';
        });
      } catch (err) {
        if (!disposed && gen === qrGen) toast(t('qr_failed'), 'error');
      } finally {
        if (objectUrl) { URL.revokeObjectURL(objectUrl); qrUrls.delete(objectUrl); }
      }
    });
  }

  function hideQrCode() {
    ++qrGen;
    el('qrSection').style.display = 'none';
    const canvas = getCanvas('qrCanvas');
    canvas.getContext('2d')?.clearRect(0, 0, canvas.width, canvas.height);
  }

  let qrMode: 'link' | 'happ' = 'link';
  let qrGen = 0;
  function setQrMode(mode: 'link' | 'happ') {
    qrMode = mode;
    el('qrBtnLink').classList.toggle('active', mode === 'link');
    el('qrBtnHapp').classList.toggle('active', mode === 'happ');
    showQrCode();
  }

  let closeProfiles: (() => void) | undefined;
  async function showProfiles() {
    closeProfiles?.();
    const overlay = el('profilesOverlay');
    const list = el('profilesList');
    const current = loadGen('profiles');
    const listeners = new AbortController();
    overlay.classList.add('show');
    list.innerHTML = loadingHtml();

    const close = () => {
      overlay.classList.remove('show');
      loadGen('profiles');
      closeProfiles = undefined;
      listeners.abort();
    };
    closeProfiles = close;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close(); };
    el('profilesClose').addEventListener('click', close, { signal: listeners.signal });
    overlay.addEventListener('click', e => { if (e.target === overlay) close(); }, { signal: listeners.signal });
    document.addEventListener('keydown', onKey, { signal: listeners.signal });

    const res = await api('GET', '/profiles?lang=' + i18n.language);
    if (disposed || !current()) return;
    if (!res || !res.success) {
      list.innerHTML = '<div class="profiles-loading">' + escapeHtml(res?.msg || t('network_err')) + '</div>';
      return;
    }
    const obj = isRecord(res.obj) ? res.obj : {};
    if (Object.values(obj).some(value => typeof value !== 'string')) { list.textContent = t('invalid_response'); return; }
    const keys = Object.keys(obj);
    if (!keys.length) {
      list.innerHTML = '<div class="profiles-loading">' + escapeHtml(t('no_profiles')) + '</div>';
      return;
    }
    list.innerHTML = keys.map(name =>
      `<div class="profile-item">
        <div class="profile-name">${escapeHtml(name)}</div>
        <div class="profile-desc">${escapeHtml(obj[name])}</div>
      </div>`
    ).join('');
  }

  const abort = new AbortController();
  const listen = (id: string, fn: () => void) => el(id).addEventListener('click', fn, { signal: abort.signal });
  listen('themeBtn', theme.toggleTheme);
  doc.querySelectorAll<HTMLElement>('[data-lang]').forEach(node => node.addEventListener('click', () => i18n.setLang(node.dataset.lang ?? ''), { signal: abort.signal }));
  listen('btnProfiles', () => { void showProfiles(); });
  listen('btnRefresh', () => { void loadStats(); });
  listen('btnLogout', () => { void doLogout(); });
  listen('btnCopy', copyLink);
  listen('btnQr', () => { void showQrCode(); });
  listen('qrBtnLink', () => setQrMode('link'));
  listen('qrBtnHapp', () => setQrMode('happ'));
  listen('qrClose', hideQrCode);
  listen('btnHistory', () => { win.location.href = bootstrap.prefix + '/history'; });
  listen('btnApply', () => { void applyBonus(); });
  listen('btnSave', () => { void saveSettings(); });
  listen('btnReset', () => { void doReset(); });
  listen('btnDelete', () => { void doDelete(); });
  input('setUser').addEventListener('input', validateSettingsUsername, { signal: abort.signal });
  input('setName').addEventListener('input', () => { dirtySettings.name = true; }, { signal: abort.signal });
  input('setFp').addEventListener('change', () => { dirtySettings.fp = true; }, { signal: abort.signal });
  input('bonusInput').addEventListener('keydown', event => { if (event instanceof KeyboardEvent && event.key === 'Enter') void applyBonus(); }, { signal: abort.signal });
  doc.querySelectorAll<HTMLElement>('[data-password]').forEach(node => node.addEventListener('click', () => togglePass(doc, node.dataset.password ?? '', node), { signal: abort.signal }));
  theme.applyTheme(); initPassToggles(doc); i18n.initLang();
  void loadStats(); void loadFingerprints();
  return () => { disposed = true; abort.abort(); ++qrGen; ++settingsValidationGen; loadGen('stats'); loadGen('profiles'); timers.forEach(id => win.clearTimeout(id)); qrUrls.forEach(url => URL.revokeObjectURL(url)); qrUrls.clear(); dialogs.dispose(); notifications.dispose(); closeProfiles?.(); };
}
