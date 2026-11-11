import { createApi } from '../shared/api';
import { requiredElement } from '../shared/dom';
import { isRecord } from '../shared/json';
import { button, initAuthChrome, input } from './chrome';

export function initAuth(doc: Document = document, request: typeof fetch = fetch, navigate: (url: string) => void = url => { window.location.href = url; }): () => void {
  const controller = new AbortController();
  const { signal } = controller;
  const isAborted = (): boolean => signal.aborted;
  const { bootstrap, i18n } = initAuthChrome(doc, window.location, signal);
  const api = createApi(() => ({ baseUrl: bootstrap.prefix + '/webapi' }), i18n.t, () => {}, request);
  const dashboard = bootstrap.prefix + '/panel';
  const title = requiredElement(doc, 'authTitle');
  const error = requiredElement(doc, 'authError');
  const loginButton = button(doc, 'loginBtn');
  const registerButton = button(doc, 'regBtn');
  const loginUser = input(doc, 'loginUser');
  const loginPass = input(doc, 'loginPass');
  const regUser = input(doc, 'regUser');
  const regCode = input(doc, 'regCode');
  const regPass = input(doc, 'regPass');
  const regPass2 = input(doc, 'regPass2');
  const hint = requiredElement(doc, 'usernameHint');
  const codeHint = requiredElement(doc, 'codeHint');
  let tab: 'login' | 'register' = 'login';
  let timer: ReturnType<typeof setTimeout> | undefined;
  let validationGen = 0;
  let codeTimer: ReturnType<typeof setTimeout> | undefined;
  let codeGen = 0;
  let validCode = '';
  let codeState = 'code_required';
  let registering = false;

  function renderCode(): void {
    codeHint.textContent = i18n.t(codeState);
    codeHint.className = 'username-hint' + (codeState === 'code_valid' ? ' ok' : codeState === 'code_invalid' || codeState === 'code_check_failed' ? ' bad' : '');
    registerButton.disabled = registering || !validCode || validCode !== regCode.value;
  }

  function render(): void {
    i18n.applyStaticLang();
    title.textContent = i18n.t(tab === 'login' ? 'sign_in' : 'create_account');
    doc.title = title.textContent;
    renderCode();
  }
  function switchTab(next: 'login' | 'register'): void {
    tab = next;
    doc.querySelectorAll('.auth-tab').forEach((element, index) => element.classList.toggle('active', next === 'login' ? index === 0 : index === 1));
    requiredElement(doc, 'loginForm').style.display = next === 'login' ? 'block' : 'none';
    requiredElement(doc, 'registerForm').style.display = next === 'register' ? 'block' : 'none';
    error.classList.remove('show');
    render();
  }
  function showError(message: string): void {
    if (signal.aborted) return;
    error.textContent = message;
    error.classList.add('show');
  }
  async function login(): Promise<void> {
    if (loginButton.disabled) return;
    loginButton.disabled = true;
    try {
      const result = await api('POST', '/login', { username: loginUser.value, password: loginPass.value });
      if (signal.aborted) return;
      if (result?.success) navigate(dashboard);
      else showError(result?.msg || i18n.t('sign_in'));
    } catch { showError(i18n.t('network_err')); }
    finally { loginButton.disabled = false; }
  }
  function validateUsername(): void {
    clearTimeout(timer);
    const generation = ++validationGen;
    const value = regUser.value;
    const code = regCode.value;
    hint.textContent = ''; hint.className = 'username-hint';
    if (!value || !validCode || validCode !== code) return;
    timer = setTimeout(() => { void (async () => {
      try {
        const result = await api('GET', `/validate?username=${encodeURIComponent(value)}&code=${encodeURIComponent(code)}`);
        if (signal.aborted || generation !== validationGen || regUser.value !== value || regCode.value !== code) return;
        if (result?.status === 403) {
          validCode = ''; codeState = 'code_invalid'; renderCode(); return;
        }
        if (result?.success && isRecord(result.obj)) {
          const data = result.obj;
          if (data['valid'] === false && typeof data['sanitized'] === 'string') { hint.textContent = i18n.t('hint_invalid') + data['sanitized']; hint.className = 'username-hint bad'; }
          else if (data['valid'] === true && data['taken'] === true) { hint.textContent = i18n.t('hint_taken'); hint.className = 'username-hint bad'; }
          else if (data['valid'] === true && data['taken'] === false) { hint.textContent = i18n.t('hint_ok'); hint.className = 'username-hint ok'; }
        }
      } catch { /* Validation is advisory. */ }
    })(); }, 400);
  }
  function validateCode(): void {
    clearTimeout(codeTimer);
    const generation = ++codeGen;
    const code = regCode.value;
    validCode = '';
    codeState = !code ? 'code_required' : code.length > 64 ? 'code_invalid' : 'code_checking';
    renderCode();
    validateUsername();
    if (!code || code.length > 64) return;
    codeTimer = setTimeout(() => { void (async () => {
      try {
        const result = await api('POST', '/validate-code', { code });
        if (signal.aborted || generation !== codeGen || regCode.value !== code) return;
        if (result?.status === 200 && result.success && isRecord(result.obj) && typeof result.obj['valid'] === 'boolean') {
          validCode = result.obj['valid'] ? code : '';
          codeState = validCode ? 'code_valid' : 'code_invalid';
        } else codeState = 'code_check_failed';
      } catch {
        if (signal.aborted || generation !== codeGen || regCode.value !== code) return;
        codeState = 'code_check_failed';
      }
      renderCode();
      validateUsername();
    })(); }, 400);
  }
  async function register(): Promise<void> {
    if (registerButton.disabled || !validCode || validCode !== regCode.value) return;
    const username = regUser.value;
    const password = regPass.value;
    if (password !== regPass2.value) { showError(i18n.t('passwords_not_match')); return; }
    registering = true;
    renderCode();
    try {
      const result = await api('POST', '/register', { username, password, code: validCode, name: input(doc, 'regName').value });
      if (signal.aborted) return;
      if (result?.success) {
        validCode = ''; codeState = 'code_required';
        let failure: string;
        try {
          const loginResult = await api('POST', '/login', { username, password });
          if (isAborted()) return;
          if (loginResult?.success) { navigate(dashboard); return; }
          failure = loginResult?.msg || i18n.t('network_err');
        } catch { failure = i18n.t('network_err'); }
        if (isAborted()) return;
        switchTab('login');
        loginUser.value = username;
        showError(i18n.t('account_created_login_failed') + failure);
      } else {
        if (result?.status === 404) { validCode = ''; codeState = 'code_invalid'; validateUsername(); }
        showError(result?.msg || i18n.t('network_err'));
      }
    } catch { showError(i18n.t('network_err')); }
    finally { registering = false; renderCode(); }
  }
  button(doc, 'tabLogin').addEventListener('click', () => { switchTab('login'); }, { signal });
  button(doc, 'tabRegister').addEventListener('click', () => { switchTab('register'); }, { signal });
  loginButton.addEventListener('click', () => { void login(); }, { signal });
  registerButton.addEventListener('click', () => { void register(); }, { signal });
  regUser.addEventListener('input', validateUsername, { signal });
  regCode.addEventListener('input', validateCode, { signal });
  const stopLanguage = i18n.onChange(render);
  loginPass.addEventListener('keydown', event => { if (event.key === 'Enter') void login(); }, { signal });
  for (const field of [regPass, regPass2]) field.addEventListener('keydown', event => { if (event.key === 'Enter') void register(); }, { signal });
  render();
  if (regCode.value) validateCode();
  void request(bootstrap.prefix + '/webapi/stats', { credentials: 'same-origin' }).then(response => {
    if (!signal.aborted && response.ok) navigate(dashboard);
  }).catch(() => {});
  return () => { stopLanguage(); controller.abort(); clearTimeout(timer); clearTimeout(codeTimer); validationGen++; codeGen++; };
}
