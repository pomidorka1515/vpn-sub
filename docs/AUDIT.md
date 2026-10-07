# Audit logs documentation  
  
## Introduction  
Audit config location is usually `../audit.jsonl` unless edited.  
There are currently **12** possible actions in the audit log.  
The base format is:  
```jsonc  
{  
    "ts": 1778089999.296969,       // float, precise timestamp  
    "date": "06.05.2026 15:44:30", // str, string in "%d.%m.%Y %H:%M:%S" format, UTC as always  
    "action": "sub_hit",           // str, action name, see below  
    "info": {}                     // dict, information about an action. empty if none (not null)  
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
- `config_update`  
  
## Action info  
  
---  
  
### `sub_hit`  
Description: A subscription hit.  
```jsonc  
{  
    "username": "",          // str, internal username  
    "lang": "ru",            // str, the language, 'ru' or 'en'  
    "ua": "Happ/3.18.8/...", // str, User-Agent  
    "ip": "x.x.x.x",         // str, IP address  
    "force_json": "0"        // str, &force_json= param if it was specified (defaults to 0/false)  
}  
```  
---  
  
### `user_refresh`  
Description: A user was refreshed on all panels. This isnt logged when a user is created, see `user_add`.  
```jsonc  
{  
    "username": "" // str, internal username  
}  
```  
  
---  
  
### `user_add`  
Description: A new user was created.  
```jsonc  
{  
    "username": "",    // str, new internal username  
    "ext_username": "" // OPTIONAL str, new external username  
}  
```  
  
---  
  
### `user_delete`  
Description: A user was deleted.  
```jsonc  
{  
    "username": "", // str, internal username  
    "perma": true   // bool, true = deleted from DB too  
}  
```  
  
---  
  
### `user_update`  
Description: A user's status was updated.  
```jsonc  
{  
    "username": "",   // str, internal username  
    "enable": false,  // OPTIONAL bool, new status on regular panels  
    "wl_enable": true // OPTIONAL bool, new status on whitelist panels  
}  
```  
  
---  
  
### `user_update_params`  
Description: User's params were updated.  
```jsonc  
{  
    "username": "",     // str, internal username  
    "displayname": "",  // OPTIONAL str, new display name  
    "fingerprint": "",  // OPTIONAL str, new fingerprint  
    "limit": 0,         // OPTIONAL int, new limit in GB for regular panels  
    "wl_limit": 0,      // OPTIONAL int, new limit in GB for whitelist panels  
    "time": 1779009009, // OPTIONAL int, new expiry time, timestamp  
    "ext_username": ""  // OPTIONAL str, new external username  
}  
```  
  
---  
  
### `user_update_uuid`  
Description: User's UUID was updated.  
```jsonc  
{  
    "username": "", // str, internal username  
    "uuid": "",     // str, new UUID  
}  
```  
  
---  
  
### `user_consume_code`  
Description: A user consumed a bonus code.  
```jsonc  
{  
    "days": 0,      // int, days on the code (0 leaves expiry unchanged)  
    "gb": 0,        // int, GB added onto the monthly limit, including a 0 limit  
    "wl_gb": 0,     // int, GB added onto the whitelist limit, including a 0 limit  
    "perma": false, // bool, whether the code used was permanent  
    "uses": 1,      // int, uses left after consume  
                    // NOTE: -1 when perma == true  
    "time": 0,      // int, expiry unix timestamp after apply (0 stays unlimited;  
                    // a lapsed finite expiry restarts from now, then days are added)  
    "limit": 0,     // int, GB, monthly limit after apply (0 was no allowance, not unlimited)  
    "wl_limit": 0   // int, GB, whitelist limit after apply  
}  
```  
  
---  
  
### `code_add`  
Description: A code was added.  
```jsonc  
{  
    "code": "",    // str, code name  
    "action": "",  // str, code type, 'register' or 'bonus'  
    "perma": true, // bool, permanent code or no  
    "uses": 1,     // int, amount of uses  
                   // NOTE: usually -1 (or omitted) when perma == true  
    "days": 0,     // int, amount of days  
    "gb": 0,       // int, GB  
    "wl_gb": 0     // int, GB  
}  
```  
  
---  
  
### `code_delete`  
Description: A code was deleted.  
```jsonc  
{  
    "code": "" // str, code name  
}  
```  
  
---  
  
### `config_update`  
Description: The admin config editor wrote a top-level patch. Values are not logged.  
```jsonc  
{  
    "keys": ["sub_name"] // array[str], top-level keys whose value changed  
}  
```  
  
---  
