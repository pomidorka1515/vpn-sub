# Audit logs documentation

## Introduction
Audit config location is usually `../audit.jsonl` unless edited.
There are currently **1** possible actions in the audit log.
The base format is:
```json
{
    "ts": 1778089999.296969, // float, precise timestamp
    "date": "06.05.2026 15:44:30", // str, string in "%d.%m.%Y %H:%M:%S" format, UTC as always
    "action": "sub_hit", // str, action name, see below
    "info": {} // dict, information about an action. empty if none (not null)
}
```

## Action list
- `sub_hit`
- `user_refresh`
- `user_delete`
- `user_reset`
- `user_update`
- `user_update_params`
- `user_add`
- `user_update_uuid`
- `user_consume_code`
- `code_add`
- `code_delete`

## Action info

---

### `sub_hit`
Description: A subscription hit.
```json
{
    "username": "", // str, internal username
    "lang": "ru", // str, the language, 'ru' or 'en'
    "ua": "Happ/3.18.8/...", // str, User-Agent
    "ip": "x.x.x.x", // str, IP address
    "force_json": "0" // str, &force_json= param if it was specified (defaults to 0/false)
}
```
---

### `user_refresh`
Description: A user was refreshed on all panels. This isnt logged when a user is created, see `user_add`.
```json
{
    "username": "" // str, internal username
} 
```

---

### `user_add`
Description: A new user was created.
```json
{
    "username": "", // str, new internal username
    "ext_username": "" // OPTIONAL str, new external username
}
```

---

### `user_delete`
Description: A user was deleted.
```json
{
    "username": "", // str, internal username
    "perma": true // bool, true = deleted from DB too
}
```

---

### `user_update`
Description: A user's status was updated.
```json
{
    "username": "", // str, internal username
    "enable": false, // OPTIONAL bool, new status on regular panels
    "wl_enable": true // OPTIONAL bool, new status on whitelist panels
}
```

---

### `user_update_params`
Description: User's params were updated.
```json
{
    "username": "", // str, internal username
    "displayname": "", // OPTIONAL str, new display name
    "fingerprint": "", // OPTIONAL str, new fingerprint
    "limit": 0, // OPTIONAL int, new limit in GB for regular panels
    "wl_limit": 0, // OPTIONAL int, new limit in GB for whitelist panels
    "time": 1779009009, // OPTIONAL int, new expiry time, timestamp
    "ext_username": "" // OPTIONAL str, new external username
}
```

---

### `user_update_uuid`
Description: User's UUID was updated.
```json
{
    "username": "", // str, internal username
    "uuid": "", // str, new UUID
}
```

---

### `user_consume_code`
Description: A user consumed a bonus code.
```json
{
    "days": 0, // int, days added
    "gb": 0, // int, GB, gigabytes added
    "wl_gb": 0, // int, GB, gigabytes added to whitelist bandwidth limit
    "perma": , // bool, whether the code used was permanent
    "time": 0, // int, timestamp, new time limit
    "limit": 0, // int, GB, new limit
    "wl_limit": 0 // int, GB, new whitelist limit
}
```

---

### `code_add`
Description: A code was added.
```json
{
    "code": "", // str, code name
    "action": "", // str, code type, 'register' or 'bonus'
    "perma": true, // bool, permanent code or no
    "days": 0, // int, amount of days
    "gb": 0, // int, GB
    "wl_gb": 0 // int, GB
}
```

---

### `code_delete`
Description: A code was deleted.
```json
{
    "code": "" // str, code name
}
```

---