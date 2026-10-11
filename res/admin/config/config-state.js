export const RESTART_KEYS = ['uri', 'api_uri', 'api_token', '3xui', 'redis', 'bot', 'publicbot', 'panel_alert_cooldown', 'salt'];
export const OPTIONAL_KEYS = ['api_uri', 'fallback_domain', 'funny_strings', 'bot', 'publicbot'];
export const REQUIRED_KEYS = [
  'uri', 'api_token', 'api_admin_ui_auth', 'provider_id', 'panel_alert_cooldown', 'salt',
  'fingerprints', 'nodes', 'json_template', 'bypass_packages', 'domain', 'ping_check_url',
  '3xui', 'sub_name', 'profiles', 'redis',
];
export const KNOWN_KEYS = ['$schema', ...REQUIRED_KEYS, ...OPTIONAL_KEYS.filter(k => !REQUIRED_KEYS.includes(k))];
export const TEMPLATE_KEYS = ['dns', 'routing', 'inbounds', 'outbounds', 'remarks'];
export const PROFILE_FIELDS = ['flag', 'name', 'json', 'description', 'whitelist', 'xhttpExtra', 'masterLink', 'node', 'shortProfileDescription'];
export const PANEL_FIELDS = ['name', 'address', 'port', 'uri', 'token', 'https', 'whitelist', 'inbounds_list', 'mode'];
export const PANEL_OPTIONAL = ['nginx_auth', 'inject_headers'];
export const INTEGER_RE = /^(0|-?[1-9]\d*)$/;

export const MAIN_SECTIONS = [
  ['config_sec_service', ['uri', 'api_uri', 'domain', 'fallback_domain', 'ping_check_url', 'provider_id', 'sub_name']],
  ['config_sec_auth', ['api_token', 'api_admin_ui_auth', 'salt']],
  ['config_sec_lists', ['panel_alert_cooldown', 'fingerprints', 'bypass_packages', 'funny_strings', 'nodes']],
  ['config_sec_integrations', ['redis', 'bot', 'publicbot']],
  ['config_sec_template', ['json_template', '$schema']],
];

export const editor = {
  loaded: null,
  draft: null,
  etag: '',
  dirty: false,
  conflict: false,
  saving: false,
  loading: false,
  fetched: false,
  open: { inbounds: new Set(), panels: new Set() },
  restartLine: '',
  dialog: null,
  nodeRows: null,
  headerRows: new Map(),
  // Textarea source is separate from parsed values, including JSON strings.
  jsonInputs: new Map(),
};
