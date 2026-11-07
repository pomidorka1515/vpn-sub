import { initDashboard } from '../dashboard/page';

const cleanup = initDashboard();
window.addEventListener('pagehide', event => { if (!event.persisted) cleanup(); });
