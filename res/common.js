(function (w) {
  w.applyTheme = function () {
    const theme = w.theme || localStorage.getItem('theme') || 'dark';
    w.theme = theme;
    document.documentElement.setAttribute('data-theme', theme);
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
  w.fmtBytes = function (b) {
    if (!Number.isFinite(b)) return 'N/A';
    if (b <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(Math.floor(Math.log(b) / Math.log(1000)), units.length - 1);
    return (b / Math.pow(1000, i)).toFixed(i > 1 ? 2 : 0) + ' ' + units[i];
  };
})(window);
