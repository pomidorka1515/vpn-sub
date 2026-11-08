import type { ConfigObject } from './json';

export const RESTART_KEYS = ['uri', 'api_uri', 'api_token', '3xui', 'redis', 'bot', 'publicbot', 'discord', 'panel_alert_cooldown', 'salt'];
export const OPTIONAL_KEYS = ['api_uri', 'fallback_domain', 'funny_strings', 'bot', 'publicbot', 'discord', 'support_text'];
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

export const MAIN_SECTIONS: Array<[string, string[]]> = [
  ['config_sec_service', ['uri', 'api_uri', 'domain', 'fallback_domain', 'ping_check_url', 'provider_id', 'sub_name']],
  ['config_sec_auth', ['api_token', 'api_admin_ui_auth', 'salt']],
  ['config_sec_lists', ['panel_alert_cooldown', 'fingerprints', 'bypass_packages', 'funny_strings', 'nodes']],
  ['config_sec_integrations', ['redis', 'bot', 'publicbot', 'discord', 'support_text']],
  ['config_sec_template', ['json_template', '$schema']],
];

export interface EditorState {
  loaded: ConfigObject | null;
  draft: ConfigObject | null;
  etag: string;
  dirty: boolean;
  conflict: boolean;
  saving: boolean;
  loading: boolean;
  fetched: boolean;
  open: { inbounds: Set<string>; panels: Set<string> };
  restartLine: string;
  dialog: { kind: string; draft: ConfigObject; opener: HTMLElement | null } | null;
  nodeRows: Array<[string, string]> | null;
  headerRows: Map<string, Array<[string, string]>>;
  jsonInputs: Map<string, string>;
}

export const editor: EditorState = {
  loaded: null,
  draft: null,
  etag: '',
  dirty: false,
  conflict: false,
  saving: false,
  loading: false,
  fetched: false,
  open: { inbounds: new Set<string>(), panels: new Set<string>() },
  restartLine: '',
  dialog: null,
  nodeRows: null,
  headerRows: new Map<string, Array<[string, string]>>(),
  // Textarea source is separate from parsed values, including JSON strings.
  jsonInputs: new Map<string, string>(),
};
