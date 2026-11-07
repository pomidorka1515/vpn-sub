import { initHistory } from '../history/page';

const cleanup = initHistory();
window.addEventListener('pagehide', event => { if (!event.persisted) cleanup(); });
