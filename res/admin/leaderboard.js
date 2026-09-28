export async function fetchLeaderboard() {
  const type = document.getElementById('lbType').value;
  const cutoff = parseInt(document.getElementById('lbCutoff').value) || 0;
  const displaynames = document.getElementById('lbDisplaynames').checked;
  const flip = document.getElementById('lbFlip').checked;

  const body = {
    type,
    cutoff: cutoff > 0 ? cutoff : 0,
    displaynames: !!displaynames,
    flip: !!flip,
  };

  const wrap = document.getElementById('lbTableWrap');
  wrap.innerHTML = `<div class="table-empty" style="padding:20px">${t('loading')}</div>`;

  const res = await api('POST', '/api/leaderboard', body);
  if (!res || !res.success) {
    wrap.innerHTML = `<div class="table-empty">${escapeHtml(res?.msg || t('error'))}</div>`;
    return;
  }

  const data = res.obj;
  if (!Array.isArray(data) || data.length === 0) {
    wrap.innerHTML = `<div class="table-empty">${t('no_entries')}</div>`;
    return;
  }

  wrap.innerHTML = `
    <table class="data-table">
      <thead><tr>
        <th style="width:50px">${t('lb_rank')}</th>
        <th>${t('lb_user')}</th>
        <th style="text-align:right">${t('lb_bandwidth')}</th>
      </tr></thead>
      <tbody>
        ${data.map(entry => `
          <tr>
            <td class="mono" style="color:var(--text2)">${entry.place}</td>
            <td class="mono">${escapeHtml(entry.username)}</td>
            <td class="mono" style="text-align:right">${fmtBytes(entry.amount)}</td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}
