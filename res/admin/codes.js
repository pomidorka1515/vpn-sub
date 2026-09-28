export async function loadCodes() {
  const list = document.getElementById('codeList');
  list.innerHTML = `<div class="table-empty">${t('loading')}</div>`;
  const res = await api('GET', '/api/code/list');
  if (!res || !res.success) {
    list.innerHTML = `<div class="table-empty">${escapeHtml(res?.msg || t('network_err'))}</div>`;
    return;
  }
  const names = Array.isArray(res.obj) ? res.obj : [];
  if (!names.length) {
    list.innerHTML = `<div class="table-empty">${t('no_codes')}</div>`;
    return;
  }
  const entries = await Promise.all(names.map(async name => {
    const info = await api('GET', `/api/code/info?code=${encodeURIComponent(name)}`);
    return [name, info && info.success ? info.obj : null];
  }));
  renderCodesList(entries);
}

function renderCodesList(entries) {
  const list = document.getElementById('codeList');
  if (!entries.length) {
    list.innerHTML = `<div class="table-empty">${t('no_codes')}</div>`;
    return;
  }
  list.innerHTML = entries.map(([name, c]) => {
    let badges = '';
    const meta = [];
    if (c) {
      const isRegister = c.action === 'register';
      badges += `<span class="badge ${isRegister ? 'purple' : 'cyan'}">${escapeHtml(c.action || '—')}</span>`;
      badges += c.perma
        ? `<span class="badge green">${t('reusable')}</span>`
        : `<span class="badge gray">${c.uses ?? 1} ${t('uses').toLowerCase()}</span>`;
      if (c.days) meta.push(`<span>${c.days} ${t('days').toLowerCase()}</span>`);
      if (c.gb) meta.push(`<span>${c.gb} ${t('gb')}</span>`);
      if (c.wl_gb) meta.push(`<span>${c.wl_gb} ${t('gb')} ${t('wl')}</span>`);
    }
    return `
      <div class="code-card">
        <div class="code-info">
          <div class="code-name">${escapeHtml(name)}</div>
          <div class="code-meta">${badges}${meta.join('')}</div>
        </div>
        <div class="code-actions">
          <button class="btn-tiny" data-code-action="copy" data-code="${escapeHtml(name)}">${t('copy')}</button>
          <button class="btn-tiny danger" data-code-action="delete" data-code="${escapeHtml(name)}">${t('delete')}</button>
        </div>
      </div>
    `;
  }).join('');
}

function onCodeListClick(e) {
  const btn = e.target.closest('button[data-code-action]');
  if (!btn) return;
  const code = btn.getAttribute('data-code');
  const action = btn.getAttribute('data-code-action');
  if (action === 'copy') copyCode(code);
  else if (action === 'delete') deleteCode(code);
}

function copyCode(code) {
  navigator.clipboard.writeText(code)
    .then(() => toast(t('copied'), 'success'))
    .catch(() => toast(t('copy_failed'), 'error'));
}

async function deleteCode(code) {
  if (!await confirmDialog(t('confirm_delete_code'), t('confirm'), confirmOpts())) return;
  const res = await api('POST', '/api/code/delete', { code });
  if (res && res.success) {
    toast(t('deleted'), 'success');
    loadCodes();
  } else {
    toast(res?.msg || t('error'), 'error');
  }
}

export function toggleUsesRow() {
  const perma = document.getElementById('addPerma').checked;
  document.getElementById('addUsesRow').style.display = perma ? 'none' : 'flex';
}

export function showAddCode() {
  document.getElementById('addCodeCard').style.display = 'block';
  document.getElementById('addCode').value = '';
  document.getElementById('addAction').value = 'bonus';
  document.getElementById('addDays').value = '0';
  document.getElementById('addGb').value = '0';
  document.getElementById('addWlGb').value = '0';
  document.getElementById('addPerma').checked = false;
  document.getElementById('addUses').value = '1';
  toggleUsesRow();
  const result = document.getElementById('addCodeResult');
  result.style.display = 'none';
  result.innerHTML = '';
  document.getElementById('addCode').focus();
}

export function hideAddCode() {
  document.getElementById('addCodeCard').style.display = 'none';
}

export async function doAddCode() {
  const code = document.getElementById('addCode').value.trim();
  if (!code) { toast(t('code_required'), 'error'); return; }

  const perma = document.getElementById('addPerma').checked;
  const body = {
    code,
    action: document.getElementById('addAction').value,
    perma,
    days: parseInt(document.getElementById('addDays').value, 10) || 0,
    gb: parseInt(document.getElementById('addGb').value, 10) || 0,
    wl_gb: parseInt(document.getElementById('addWlGb').value, 10) || 0,
  };
  if (!perma) {
    const uses = parseInt(document.getElementById('addUses').value, 10);
    if (isNaN(uses) || uses <= 0) { toast(t('uses_invalid'), 'error'); return; }
    body.uses = uses;
  }

  const res = await api('POST', '/api/code/add', body);
  if (res && res.success) {
    toast(t('created'), 'success');
    hideAddCode();
    loadCodes();
  } else {
    const result = document.getElementById('addCodeResult');
    result.style.display = 'block';
    result.className = 'result-box error';
    result.innerHTML = `<div class="result-title err">${t('error')}</div><div class="result-msg">${escapeHtml(res?.msg || t('error'))}</div>`;
  }
}

export function bindCodeList() {
  document.getElementById('codeList').addEventListener('click', onCodeListClick);
}

const confirmOpts = () => ({ okText: t('confirm'), cancelText: t('cancel') });
