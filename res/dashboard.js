function renderLang() {
  applyStaticLang();
  if (currentStats) renderStats(currentStats);
}

window.apiConfig = {
  baseUrl: window.API,
  on401: () => { window.location.href = window.AUTH_PAGE; },
};



function getBwColor(pct) {
  if (pct <= 25) return 'var(--green)';
  if (pct <= 50) return 'var(--yellow)';
  if (pct <= 75) return 'var(--orange)';
  if (pct <= 90) return 'var(--red)';
  return 'var(--red-dark)';
}

let currentStats = null;

async function loadStats() {
  const res = await api('GET', '/stats');
  if (!res) return;
  if (!res.success) { toast(res.msg || t('network_err'), 'error'); return; }
  currentStats = res.obj;
  renderStats(res.obj);
}

function renderStats(s) {
  document.getElementById('topName').textContent = s.displayname || '—';

  const dot = document.getElementById('statusDot');
  const stxt = document.getElementById('statusText');
  if (s.online) { dot.className = 'status-dot on'; stxt.textContent = t('online'); }
  else { dot.className = 'status-dot off'; stxt.textContent = t('offline'); }

  const banner = document.getElementById('disabledBanner');
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

  document.getElementById('subLink').textContent = s.link ? s.link + '&lang=' + window.lang : '—';

  const expEl = document.getElementById('statExpiry');
  expEl.textContent = fmtTime(s.time, t);
  expEl.style.color = isExpired ? 'var(--red)' : '';

  const enEl = document.getElementById('statEnabled');
  if (!s.enabled) {
    enEl.textContent = t('disabled');
    enEl.style.color = 'var(--red)';
  } else {
    enEl.textContent = t('active');
    enEl.style.color = 'var(--green)';
    if (s.wl_enabled) enEl.textContent += t('wl_suffix');
  }

  const bw = s.bandwidth;
  document.getElementById('statTotal').textContent = fmtBytes(bw.total.upload + bw.total.download);
  document.getElementById('statTotalSub').textContent = '↑ ' + fmtBytes(bw.total.upload) + '  ↓ ' + fmtBytes(bw.total.download);
  document.getElementById('statWl').textContent = fmtBytes(bw.wl_total.upload + bw.wl_total.download);
  document.getElementById('statWlSub').textContent = '↑ ' + fmtBytes(bw.wl_total.upload) + '  ↓ ' + fmtBytes(bw.wl_total.download);

  const limitB = bw.limit * 1073741824;
  const wlLimitB = bw.wl_limit * 1073741824;

  if (limitB > 0) {
    const pct = (bw.monthly / limitB) * 100;
    document.getElementById('bwMonthlyBar').style.width = Math.min(100, pct) + '%';
    document.getElementById('bwMonthlyBar').style.background = getBwColor(pct);
    document.getElementById('bwMonthlyText').textContent = fmtBytes(bw.monthly) + ' / ' + bw.limit + ' ' + t('gb_unit');
  } else {
    document.getElementById('bwMonthlyBar').style.width = '0%';
    document.getElementById('bwMonthlyText').textContent = fmtBytes(bw.monthly) + ' / ∞';
  }

  if (wlLimitB > 0) {
    const pct = (bw.wl_monthly / wlLimitB) * 100;
    document.getElementById('bwWlBar').style.width = Math.min(100, pct) + '%';
    document.getElementById('bwWlBar').style.background = getBwColor(pct);
    document.getElementById('bwWlText').textContent = fmtBytes(bw.wl_monthly) + ' / ' + bw.wl_limit + ' ' + t('gb_unit');
  } else {
    document.getElementById('bwWlBar').style.width = '0%';
    document.getElementById('bwWlText').textContent = fmtBytes(bw.wl_monthly) + ' / ∞';
  }

  document.getElementById('infoUuid').textContent = s.uuid || '—';
  document.getElementById('infoFp').textContent = s.fingerprint || '—';
  document.getElementById('infoToken').textContent = s.token || '—';

  document.getElementById('setName').value = s.displayname || '';
}

async function loadFingerprints() {
  const res = await api('GET', '/fingerprints');
  if (!res || !res.success) return;
  const sel = document.getElementById('setFp');
  sel.innerHTML = '';
  (res.obj || []).forEach(fp => {
    const o = document.createElement('option');
    o.value = fp; o.textContent = fp;
    if (currentStats && currentStats.fingerprint === fp) o.selected = true;
    sel.appendChild(o);
  });
}

function copyLink() {
  const link = document.getElementById('subLink').textContent;
  if (!link || link === '—') return;
  navigator.clipboard.writeText(link).then(() => {
    const btn = document.getElementById('btnCopy');
    btn.textContent = t('copied'); btn.classList.add('copied');
    setTimeout(() => { applyStaticLang(); btn.classList.remove('copied'); }, 1500);
  });
}

async function applyBonus() {
  const code = document.getElementById('bonusInput').value.trim();
  if (!code) return;
  const msg = document.getElementById('bonusMsg');
  const res = await api('POST', '/bonus', { code });
  if (!res) return;
  if (res.success) {
    const o = res.obj;
    let parts = [];
    if (o.days) parts.push('+' + o.days + ' ' + t('days'));
    if (o.gb) parts.push('+' + o.gb + ' ' + t('gb_unit'));
    if (o.wl_gb) parts.push('+' + o.wl_gb + ' ' + t('gb_wl'));
    if (o.perma) parts.push(t('bonus_reusable'));
    msg.textContent = parts.join(', ') || t('ok');
    msg.className = 'bonus-msg ok';
    document.getElementById('bonusInput').value = '';
    loadStats();
  } else { msg.textContent = res.msg || t('error'); msg.className = 'bonus-msg err'; }
}

let svt;
function validateSettingsUsername() {
  clearTimeout(svt);
  const v = document.getElementById('setUser').value.trim();
  const h = document.getElementById('setUserHint');
  if (!v) { h.textContent = ''; h.className = 'username-hint'; return; }
  svt = setTimeout(async () => {
    try {
      const res = await api('GET', `/validate?username=${encodeURIComponent(v)}`);
      if (!res) return;
      if (res.success && res.obj) {
        if (!res.obj.valid) { h.textContent = t('hint_invalid') + res.obj.sanitized; h.className = 'username-hint bad'; }
        else if (res.obj.taken) { h.textContent = t('hint_taken'); h.className = 'username-hint bad'; }
        else { h.textContent = t('hint_ok'); h.className = 'username-hint ok'; }
      }
    } catch {}
  }, 400);
}

async function saveSettings() {
  const body = {};
  const name = document.getElementById('setName').value.trim();
  const fp = document.getElementById('setFp').value;
  const user = document.getElementById('setUser').value.trim();
  const pass = document.getElementById('setPass').value;
  const curPass = document.getElementById('setCurPass').value;
  if (name) body.name = name;
  if (fp) body.fingerprint = fp;
  if (user && pass) { body.username = user; body.password = pass; }
  if (!Object.keys(body).length) return;

  const msg = document.getElementById('settingsMsg');
  if (body.username || body.password) {
    if (!curPass) {
      msg.textContent = t('current_pw_required'); msg.className = 'settings-msg err';
      setTimeout(() => { msg.textContent = ''; }, 3000);
      return;
    }
    body.current_password = curPass;
  }
  const res = await api('POST', '/settings', body);
  if (!res) return;
  if (res.success) {
    msg.textContent = t('saved'); msg.className = 'settings-msg ok';
    document.getElementById('setUser').value = '';
    document.getElementById('setPass').value = '';
    document.getElementById('setCurPass').value = '';
    document.getElementById('setUserHint').textContent = '';
    document.getElementById('setUserHint').className = 'username-hint';
    loadStats();
  } else { msg.textContent = res.status === 401 ? t('invalid_current_pw') : (res.msg || t('error')); msg.className = 'settings-msg err'; }
  setTimeout(() => { msg.textContent = ''; }, 3000);
}

const confirmOpts = () => ({ okText: t('confirm_yes'), cancelText: t('confirm_cancel') });

async function doReset() {
  if (!(await confirmDialog(t('confirm_reset'), t('confirm_title_reset'), confirmOpts()))) return;
  const res = await api('POST', '/reset');
  if (!res) return;
  if (res.success) { toast(t('ok')); loadStats(); }
  else toast(res.msg || t('error'), 'error');
}

async function doDelete() {
  if (!(await confirmDialog(t('confirm_delete'), t('confirm_title_delete'), confirmOpts()))) return;
  if (!(await confirmDialog(t('confirm_delete2'), t('confirm_title_final'), confirmOpts()))) return;
  const curPass = await confirmDialog(t('current_pw_required'), t('confirm_title_delete'), { input: true, inputPlaceholder: t('current_password'), ...confirmOpts() });
  if (!curPass) return;
  const res = await api('POST', '/delete', { current_password: curPass });
  if (!res) return;
  if (res.success) { window.location.href = window.AUTH_PAGE; }
  else toast(res.status === 401 ? t('invalid_current_pw') : (res.msg || t('error')), 'error');
}

async function doLogout() {
  try { await api('POST', '/logout'); } catch {}
  window.location.href = window.AUTH_PAGE;
}

async function showQrCode() {
  const link = document.getElementById('subLink').textContent;
  if (!link || link === '—') return;

  let url = window.API + '/qr?lang=' + window.lang;
  if (qrMode === 'happ') url += '&happ=1';

  try {
    const res = await fetch(url, { method: 'GET', credentials: 'same-origin' });

    if (!res.ok) throw new Error(t('qr_failed'));

    const blob = await res.blob();
    const canvas = document.getElementById('qrCanvas');
    const ctx = canvas.getContext('2d');
    const img = new Image();

    img.onload = () => {
      canvas.width = img.width;
      canvas.height = img.height;
      ctx.drawImage(img, 0, 0);
      document.getElementById('qrSection').style.display = 'block';
    };

    img.src = URL.createObjectURL(blob);
  } catch (err) {
    toast(t('qr_failed'), 'error');
  }
}

function hideQrCode() {
  document.getElementById('qrSection').style.display = 'none';
}

let qrMode = 'link';
function setQrMode(mode) {
  qrMode = mode;
  document.getElementById('qrBtnLink').classList.toggle('active', mode === 'link');
  document.getElementById('qrBtnHapp').classList.toggle('active', mode === 'happ');
  showQrCode();
}

async function showProfiles() {
  const overlay = document.getElementById('profilesOverlay');
  const list = document.getElementById('profilesList');
  list.innerHTML = '<div class="profiles-loading">…</div>';
  overlay.classList.add('show');

  const close = () => {
    overlay.classList.remove('show');
    overlay.onclick = null;
    document.removeEventListener('keydown', onKey);
    document.getElementById('profilesClose').onclick = null;
  };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  document.getElementById('profilesClose').onclick = close;
  overlay.onclick = (e) => { if (e.target === overlay) close(); };
  document.addEventListener('keydown', onKey);

  const res = await api('GET', '/profiles?lang=' + window.lang);
  if (!res || !res.success) {
    list.innerHTML = '<div class="profiles-loading">' + escapeHtml(res?.msg || t('network_err')) + '</div>';
    return;
  }
  const obj = res.obj || {};
  const keys = Object.keys(obj);
  if (!keys.length) {
    list.innerHTML = '<div class="profiles-loading">' + t('no_profiles') + '</div>';
    return;
  }
  list.innerHTML = keys.map(name =>
    `<div class="profile-item">
      <div class="profile-name">${escapeHtml(name)}</div>
      <div class="profile-desc">${escapeHtml(obj[name])}</div>
    </div>`
  ).join('');
}

applyTheme();
initPassToggles();
initLang();
renderLang();
loadStats();
loadFingerprints();

document.getElementById('bonusInput').addEventListener('keydown', e => { if (e.key === 'Enter') applyBonus(); });
