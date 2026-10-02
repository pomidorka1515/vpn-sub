// Chart descriptions only. No DOM, no Chart.
// path is a list of keys. scale 'percent' divides used by total * 100.
// axis 'y1' is the right-hand percent axis on dual-axis cards.

const HOST_GENERAL = [
  {
    id: 'cpu',
    titleKey: 'cpu',
    unit: 'percent',
    threshold: 'cpu',
    series: [{ key: 'cpu', labelKey: 'cpu', path: ['cpu'] }],
  },
  {
    id: 'load',
    titleKey: 'load_avg',
    unit: 'float',
    series: [
      { key: 'load_1m', labelKey: 'load_1m', path: ['loadavg', 'load_1m'] },
      { key: 'load_5m', labelKey: 'load_5m', path: ['loadavg', 'load_5m'] },
      { key: 'load_15m', labelKey: 'load_15m', path: ['loadavg', 'load_15m'] },
    ],
  },
  {
    id: 'processes',
    titleKey: 'processes',
    unit: 'count',
    series: [{ key: 'processes', labelKey: 'processes', path: ['process_count'] }],
  },
  {
    id: 'memory',
    titleKey: 'memory',
    unit: 'bytes',
    threshold: 'memory',
    series: [
      { key: 'used', labelKey: 'used', path: ['memory', 'ram', 'used'] },
      { key: 'percent', labelKey: 'percent', path: ['memory', 'ram', 'used'], scale: 'percent', totalPath: ['memory', 'ram', 'total'], axis: 'y1', unit: 'percent' },
    ],
  },
  {
    id: 'swap',
    titleKey: 'swap',
    unit: 'bytes',
    series: [
      { key: 'used', labelKey: 'used', path: ['memory', 'swap', 'used'] },
      { key: 'percent', labelKey: 'percent', path: ['memory', 'swap', 'used'], scale: 'percent', totalPath: ['memory', 'swap', 'total'], axis: 'y1', unit: 'percent' },
    ],
  },
];

const HOST_NETWORK = [
  {
    id: 'host_connections',
    titleKey: 'connections',
    unit: 'count',
    series: [
      { key: 'tcp', labelKey: 'tcp', path: ['connections', 'tcp'] },
      { key: 'udp', labelKey: 'udp', path: ['connections', 'udp'] },
    ],
  },
  {
    id: 'host_totals',
    titleKey: 'network',
    unit: 'bytes',
    series: [
      { key: 'in', labelKey: 'total_in', path: ['network', 'recv'] },
      { key: 'out', labelKey: 'total_out', path: ['network', 'sent'] },
    ],
  },
];

const HOST_UPTIME = [
  {
    id: 'host_uptime',
    titleKey: 'uptime_app',
    unit: 'seconds',
    beginAtZero: false,
    series: [
      { key: 'sys', labelKey: 'sys_uptime', path: ['uptime'] },
      { key: 'app', labelKey: 'app_uptime', path: ['app_uptime'] },
    ],
  },
  {
    id: 'host_app_memory',
    titleKey: 'app_mem',
    unit: 'bytes',
    series: [{ key: 'ram', labelKey: 'app_mem', path: ['app_memory', 'ram'] }],
  },
  {
    id: 'host_app_swap',
    titleKey: 'app_swap',
    unit: 'bytes',
    series: [{ key: 'swap', labelKey: 'app_swap', path: ['app_memory', 'swap'] }],
  },
  {
    id: 'host_app_threads',
    titleKey: 'app_threads',
    unit: 'count',
    series: [{ key: 'threads', labelKey: 'app_threads', path: ['app_thread_amount'] }],
  },
];

const PANEL_GENERAL = [
  {
    id: 'panel_cpu',
    titleKey: 'cpu',
    unit: 'percent',
    threshold: 'cpu',
    series: [{ key: 'cpu', labelKey: 'cpu', path: ['cpu'] }],
  },
  {
    id: 'panel_load',
    titleKey: 'load_avg',
    unit: 'float',
    series: [
      { key: 'load_1m', labelKey: 'load_1m', path: ['loads', 0] },
      { key: 'load_5m', labelKey: 'load_5m', path: ['loads', 1] },
      { key: 'load_15m', labelKey: 'load_15m', path: ['loads', 2] },
    ],
  },
  {
    id: 'panel_memory',
    titleKey: 'memory',
    unit: 'bytes',
    threshold: 'memory',
    series: [
      { key: 'used', labelKey: 'used', path: ['mem', 'current'] },
      { key: 'percent', labelKey: 'percent', path: ['mem', 'current'], scale: 'percent', totalPath: ['mem', 'total'], axis: 'y1', unit: 'percent' },
    ],
  },
  {
    id: 'panel_swap',
    titleKey: 'swap',
    unit: 'bytes',
    series: [
      { key: 'used', labelKey: 'used', path: ['swap', 'current'] },
      { key: 'percent', labelKey: 'percent', path: ['swap', 'current'], scale: 'percent', totalPath: ['swap', 'total'], axis: 'y1', unit: 'percent' },
    ],
  },
  {
    id: 'panel_disk',
    titleKey: 'disk',
    unit: 'bytes',
    threshold: 'disk',
    series: [
      { key: 'used', labelKey: 'used', path: ['disk', 'current'] },
      { key: 'percent', labelKey: 'percent', path: ['disk', 'current'], scale: 'percent', totalPath: ['disk', 'total'], axis: 'y1', unit: 'percent' },
    ],
  },
];

const PANEL_NETWORK = [
  {
    id: 'panel_rate',
    titleKey: 'network',
    unit: 'bytes_per_s',
    series: [
      { key: 'up', labelKey: 'up', path: ['netIO', 'up'] },
      { key: 'down', labelKey: 'down', path: ['netIO', 'down'] },
    ],
  },
  {
    id: 'panel_totals',
    titleKey: 'network',
    unit: 'bytes',
    series: [
      { key: 'in', labelKey: 'total_in', path: ['netTraffic', 'recv'] },
      { key: 'out', labelKey: 'total_out', path: ['netTraffic', 'sent'] },
    ],
  },
  {
    id: 'panel_connections',
    titleKey: 'connections',
    unit: 'count',
    series: [
      { key: 'tcp', labelKey: 'tcp', path: ['tcpCount'] },
      { key: 'udp', labelKey: 'udp', path: ['udpCount'] },
    ],
  },
];

const PANEL_UPTIME = [
  {
    id: 'panel_uptime',
    titleKey: 'uptime_app',
    unit: 'seconds',
    beginAtZero: false,
    series: [
      { key: 'sys', labelKey: 'sys_uptime', path: ['uptime'] },
      { key: 'app', labelKey: 'app_uptime', path: ['appStats', 'uptime'], altPath: ['app_stats', 'uptime'] },
    ],
  },
  {
    id: 'panel_app_memory',
    titleKey: 'app_mem',
    unit: 'bytes',
    series: [{ key: 'mem', labelKey: 'app_mem', path: ['appStats', 'mem'], altPath: ['app_stats', 'mem'] }],
  },
  {
    id: 'panel_threads',
    titleKey: 'app_threads',
    unit: 'count',
    series: [{ key: 'threads', labelKey: 'app_threads', path: ['appStats', 'threads'], altPath: ['app_stats', 'threads'] }],
  },
];

export const GROUPS = {
  host: { general: HOST_GENERAL, network: HOST_NETWORK, uptime: HOST_UPTIME },
  panel: { general: PANEL_GENERAL, network: PANEL_NETWORK, uptime: PANEL_UPTIME },
};

export function chartsFor(scope, group) {
  const kind = scope === 'host' ? 'host' : 'panel';
  return GROUPS[kind][group] || [];
}
