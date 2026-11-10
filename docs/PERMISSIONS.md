# Administrator permissions  
  
`api_admin_ui_auth` is the non-deletable root account, `api_token` is the independent non-deletable root key. Both always have literal `*`. Config-backed root credentials retain their existing live/restart behavior; root API token changes require a restart. SQLite schema 8 adds scoped identities and sessions automatically without changing existing users, codes, quotas, or root credentials.
  
The first admin UI tab, **Administrators**, manages two independent lists: admin accounts and API keys. Creating or inspecting an entry opens a dialog. Passwords are hashed with Argon2id; generated keys are stored as SHA-256 digests and revealed only on creation/rotation. No list or info response reveals stored secrets. Accounts issue session-bound browser tokens with their own permissions at login; they do not create separately managed API keys. Each account has one active session. Updating an account revokes its session; another login replaces only that account's session. Scoped account sessions are independent of root sessions and of other accounts. Browser bearers also expire or become invalid on process restart. Key updates take effect on the next request, rotation revokes the previous key, and deletion revokes access immediately. Open live-chart streams recheck authority before each frame.  
  
## Matching and delegation  
  
- Literal `*` allows everything, including root-only operations and future permissions.  
- Every branch supports `.*`, for example `user.*`, `user.edit.*`, or `user.edit.bandwidth.monthly.*`. These grants include all descendants, including future actions. Bare branch names are invalid. Wildcards respect dot boundaries.  
- Permission arrays are strictly validated. Unknown permissions, wrong types and missing read prerequisites are rejected with HTTP 400. An empty array is valid and grants no actions.  
- Create/update grants must be covered by the acting identity's own grants. Wildcards cannot be delegated merely because the actor has today's individual leaves. Updating, rotating or deleting an existing identity additionally requires control of **all its existing permissions**, preventing credential takeover of a stronger administrator. Concurrent changes to the actor or target abort the management write with HTTP 409.  
- The UI automatically selects prerequisites, removes dependent actions when read access is removed, and folds complete branches to wildcards when the actor may delegate those wildcards. Selecting everything folds to `['*']` for root-authorized administrators.  
- Tabs never disappear for lack of permission: they remain disabled with an explanation. Field/action controls follow the same rules. The frontend checks permission before sending API requests; the server independently enforces every request.  
  
## Permission catalog  
  
| Permission | Authority / prerequisite |  
| --- | --- |  
| `auth.read.accounts`, `auth.read.tokens` | List account/key metadata and permissions; never secrets. |  
| `auth.create.accounts`, `auth.create.tokens` | Create the corresponding identity type; requires its `auth.read.*` leaf. |  
| `auth.update.accounts`, `auth.update.tokens` | Edit/delete the corresponding type; key editing includes rotation. Requires its read leaf. There is no separate delete permission. |  
| `user.read` | List/info/history and fingerprints, including subscription credentials. |  
| `user.edit.displayname` | Edit display name. Requires `user.read`. |  
| `user.edit.username` | Rename an existing external web login (`new_username`). Requires `user.read`. |  
| `user.edit.fingerprint`, `user.edit.time` | Edit client fingerprint / expiry timestamp. Require `user.read`. |  
| `user.edit.bandwidth.monthly.used`, `user.edit.bandwidth.monthly.limit` | Edit regular monthly used bytes / quota in GB. Require `user.read`. |  
| `user.edit.bandwidth.wl_monthly.used`, `user.edit.bandwidth.wl_monthly.limit` | Edit whitelist monthly used bytes / quota in GB. Require `user.read`. |  
| `user.reset`, `user.password_reset`, `user.delete`, `user.create` | Reset subscription credentials, issue a web-password reset link, delete, or create users. Each requires `user.read`. Creation authorizes the initial credentials, expiry and quotas; it does not require separate edit permissions. |  
| `code.read` | List and inspect codes. |  
| `code.add.bonus`, `code.add.register`, `code.delete` | Create bonus/registration codes or delete codes. Require `code.read`. |  
| `audit.read` | Read audit logs; logs can contain sensitive operational details. |  
| `state.read.panels`, `state.read.host` | Panel status / host status. Combined `/api/state/all` requires both. |  
| `state.charts.read` | Live host/panel polling stream **only**. No separate `charts.*` category. |  
| `snapshots.read` | Saved snapshots and snapshot charts, independent of live-chart permission. |  
| `health.read.recovery` | Pending recovery and rollback status. Resolving a rollback marker is root-only (`*`), because this permission is read-only. |  
| `health.read.status` | Service health, with recovery details omitted unless separately authorized. |  
| `health.read.online_users`, `health.read.teapot` | Online-user listing / authenticated teapot check. |  
| `health.sync` | Synchronize users to panels (`/api/user/refresh`). |  
| `leaderboard.read.total`, `leaderboard.read.monthly`, `leaderboard.read.wl_monthly` | Read only the selected leaderboard category. |  
| `config.read.main`, `config.read.inbounds`, `config.read.panels` | Read main configuration, inbound profiles, or panel connections. Inbound reads also include the node catalog. Panel configuration includes panel credentials: delegate carefully. |  
| `config.write.main`, `config.write.inbounds`, `config.write.panels` | Patch corresponding top-level configuration keys. Each requires its corresponding read leaf. `profiles` belongs to inbounds; `3xui` belongs to panels; all other keys belong to main. |  
  
### Additional root-only boundaries  
  
`POST /api/user/update` checks every supplied edit field before any mutation:
`displayname` → `user.edit.displayname`, `new_username` → `user.edit.username`,
`fingerprint` → `user.edit.fingerprint`, `time` → `user.edit.time`, `used` / `limit`
→ `user.edit.bandwidth.monthly.used` / `.limit`, and `wl_used` / `wl_limit` →
`user.edit.bandwidth.wl_monthly.used` / `.limit`. Mixed unauthorized updates are
rejected as a whole. `username` identifies the target and is not itself an edit.
  
Root identities cannot be deleted or have their permissions reduced. Only literal `*` may read/write `api_token`, `api_admin_ui_auth`, `salt`, `redis`, `bot`, `publicbot`, or `discord` configuration. Hiding/protecting these values prevents root-password disclosure and escalation through bot credentials or administrator whitelists. Existing root browser config reads continue to omit `api_token`. Other configuration reads return only authorized keys; their ETag remains the revision of the complete configuration so patches retain optimistic concurrency.  
  
**Telegram and Discord administrator operations are explicitly `*`-only.** Their existing UID/Discord-ID whitelists represent trusted root operators. Scoped HTTP accounts and keys do not authorize bot commands and cannot be used to log in as bot administrators. These non-HTTP integrations intentionally do not implement granular permissions. Public bot user-account features are unchanged.  
  
## Stable HTTP contracts  
  
All paths below are relative to the existing admin API root. Authentication accepts the legacy raw `Authorization` token or `Authorization: Bearer <token>`. Cookies alone never authorize API calls. Existing root calls and response envelopes remain supported. New management responses use `obj` rather than parsing messages.  
  
An authenticated but unauthorized call returns HTTP 403:  
```json
{"success":false,"msg":"Missing user.edit.time permission","obj":{"required_permission":"user.edit.time"}}
```
Missing/invalid credentials still return 401; authentication lockout returns 429 and unavailable authentication storage returns 503. Unknown route permissions fail closed to `*`.  
  
| Endpoint | Request | `obj` on success |  
| --- | --- | --- |  
| `GET /api/auth/permissions` | No body; any authenticated identity | `{permissions, catalog: [{permission, requires}], identity}` |  
| `GET /api/auth/list?kind=accounts` (or `tokens`) | Corresponding read permission | `{items: [identity, ...]}`, including the protected root entry |  
| `POST /api/auth/create` | `{kind, name, permissions, password?}`; password required only for accounts | HTTP 201 `{item: identity, token?}`; generated token returned for keys only |  
| `POST /api/auth/update` | `{kind, id, name?, permissions?, password?, rotate?}` | `{item: identity, token?}`; password applies only to accounts, boolean rotate only to keys |  
| `POST /api/auth/delete` | `{kind, id}`; corresponding update permission | `{id, kind}` |  
  
An `identity` is always `{id: string, kind: 'accounts'|'tokens', name: string, permissions: string[], root: boolean}`. Root IDs are `root` within each kind. Names are 1–64 characters without surrounding whitespace/control characters; account names are unique among accounts and cannot shadow root. Passwords for new/scoped accounts are 1–128 characters. Updating permissions alone keeps the existing password/key secret; account updates still revoke the browser session. Unknown management fields are rejected. Root key name and permissions are immutable; rotating it also returns `restart: ['api_token']`. Root account updates return `restart: []` and invalidate its root browser session immediately.  
  
`GET /{uri}/admin/token` additionally returns `permissions` and `catalog` alongside `token`, `api_root`, and `panels_configured`. Management/bootstrap responses are `Cache-Control: no-store`. Secrets never enter the audit log. If the mutation commits but audit append fails, the existing HTTP 202 `committed` / `audit_pending` contract preserves the operation result, including any one-time generated key. Do not retry that mutation. See [API_ADMIN.md](API_ADMIN.md) and [AUDIT.md](AUDIT.md).  
