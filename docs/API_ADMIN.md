# Admin API Documentation  
  
## Introduction  
Authentication: `Authorization` header with the admin API token.  
On failure, returns HTTP 401.  
  
## Root URI  
`{domain}/{uri}/{api_uri}` where `domain` is your domain.  
Example: `http://127.0.0.1/sub/adminapi/api/something`  
the `/api/` part is present in case something else is added to the admin API.  
## Response format  
Every response follows this pattern:  
```jsonc  
// HTTP x  
{  
    "success": true, // boolean  
    "msg": null, // str on error/success (varies), null or str on success  
    "obj": null // any object or null  
}  
```  
Missing/invalid authorization:  
```jsonc  
// HTTP 401  
{  
    "success": false,  
    "msg": "Unauthorized",  
    "obj": null  
}  
```  
Error:  
```jsonc  
// HTTP 500  
{  
	"success": false,  
	"msg": "Internal server error",  
	"obj": null  
}  
```  
  
Errors preserve the response format above and use semantic HTTP status codes. Validation failures return HTTP 400, missing resources return HTTP 404, conflicts return HTTP 409, and panel failures return HTTP 502.  
  
## Endpoints  
  
---  
  
### GET /api/user/list  
Description: List all usernames.  
Authentication: header  
Body: none  
Response (success):  
```jsonc  
// HTTP 200  
{  
    "success": true,  
    "msg": null,  
    "obj": ["username1", "username2"] // list of internal usernames  
}  
```  
  
---  
  
### GET /api/user/info  
Description: Get full info about a user.  
Authentication: header  
Args:  
    user: Internal username.  
    beautify: (optional) bool-like (`1`, `true`, `yes`, `on`, `y`) to return formatted values in MB.  
Response (success):  
```jsonc  
// HTTP 200  
{  
    "success": true,  
    "msg": null,  
    "obj": { } // user info object (same shape as /webapi/stats obj)  
}  
```  
  
---  
  
### POST /api/user/add  
Description: Add a new user.  
Authentication: header  
Body:  
```jsonc  
{  
    "user": "",           // str, internal username (required, non-empty)  
    "displayname": "",    // str (required, non-empty)  
    "ext_username": "",   // OPTIONAL str or null, web UI login username  
    "ext_password": "",   // OPTIONAL str or null, web UI login password  
    "token": "",          // OPTIONAL str or null, subscription token (auto-generated if omitted)  
    "userid": "",         // OPTIONAL str or null, UUID (auto-generated if omitted)  
    "fingerprint": "",    // OPTIONAL str or null, TLS fingerprint  
    "limit": 0,           // OPTIONAL int >= 0, monthly GB limit (0 = unlimited)  
    "wl_limit": 5,        // OPTIONAL int >= 0, whitelist monthly GB limit (default: 5)  
    "time": 0             // OPTIONAL int >= 0, expiry unix timestamp (0 = unlimited)  
}  
```  
All fields are strictly type-checked. Wrong types return HTTP 400 with a descriptive message.  
Response (success):  
```jsonc  
// HTTP 201  
{  
	"success": true,  
	"msg": "Created",  
	"obj": null  
}  
```  
Response (error):  
```jsonc  
// HTTP 400  
{  
	"success": false,  
	"msg": "Ext Username too long", // or another validation error  
	"obj": null  
}  
// HTTP 409  
{  
	"success": false,  
	"msg": "Username or external username exists",  
	"obj": null  
}  
// HTTP 502  
{  
	"success": false,  
	"msg": "Panel rejected user update",  
	"obj": null  
}  
```  
  
---  
  
### POST /api/user/delete  
Description: Delete a user.  
Authentication: header  
Body:  
```jsonc  
{  
    "user": "",      // str, internal username  
    "perma": true    // OPTIONAL bool-like (true, 'yes', '1', 'on', 'y'), permanent delete (default: true)  
}  
```  
Response (success):  
```jsonc  
// HTTP 200  
{  
	"success": true,  
	"msg": "Deleted",  
	"obj": null  
}  
```  
Response (error):  
```jsonc  
// HTTP 404  
{  
	"success": false,  
	"msg": "Unknown username",  
	"obj": null  
}  
// HTTP 502  
{  
	"success": false,  
	"msg": "Panel rejected user deletion",  
	"obj": null  
}  
```  
  
---  
  
### GET /api/user/refresh  
Description: Re-sync all users to all panels. **This can take a lot of time.**  
Authentication: header  
Body: none  
Response (success):  
```jsonc  
// HTTP 200  
{  
	"success": true,  
	"msg": "Refreshed all users.", // or "Refresh completed with panel failures"  
	"obj": null, // or {"failed": ["user"], "succeeded": 1, "total": 2}  
}  
```  
  
Response (error):  
```jsonc  
// HTTP 502, when every user failed because a panel was unavailable  
{  
	"success": false,  
	"msg": "Panel refresh failed for all users",  
	"obj": {"failed": ["user"], "total": 1}  
}  
```  
  
```jsonc  
// HTTP 500, when a non-panel error aborts the remaining users  
{  
	"success": false,  
	"msg": "Refresh aborted",  
	"obj": {"failed": ["user"], "aborted": "user2", "succeeded": 1, "total": 3}  
}  
```  
  
---  
  
### GET /api/operations/status  
  
Description: Return operational recovery metadata, including rollback markers and partial snapshot failures.  
Authentication: header  
Body: none  
Response (success):  
```jsonc  
// HTTP 200  
{  
	"success": true,  
	"msg": null,  
	"obj": {  
		"daily_snapshot_failure": null, // or {"ts": 0, "failed": 0, "eligible": 0}  
		"rollback_failures": {"uuid": {}, "registration": {}}  
	}  
}  
```  
  
### POST /api/operations/rollback/resolve  
  
Description: Clear a rollback marker after manual repair.  
Authentication: header  
Body:  
```json  
{"kind": "uuid|registration", "user": "<username>"}  
```  
Response (success):  
```jsonc  
// HTTP 200  
{"success": true, "msg": "Resolved", "obj": null}  
```  
  
---  
### GET /api/user/onlines  
Description: Get currently online users.  
Authentication: header  
Args:  
    keyed: (optional) bool-like (`1`, `true`, `yes`, `on`, `y`) to return a dict keyed by username: ExternalUsernameOrNull instead of a list.  
Response (success):  
```jsonc  
// HTTP 200  
{  
    "success": true,  
    "msg": null,  
    "obj": {  
        "users": [], // list, or dict if keyed=1  
        "panel_health": {"node-1": "ok"} // ok, invalid, or unavailable  
    }  
}  
```  
  
---  
  
### POST /api/user/reset  
Description: Reset a user's token and UUID.  
Authentication: header  
Body:  
```jsonc  
{  
    "user": "" // str, internal username (required)  
}  
```  
Response (success):  
```jsonc  
// HTTP 200  
{  
    "success": true,  
    "msg": null,  
    "obj": {  
        "uuid": "",  // str, new UUID  
        "token": ""  // str, new token  
    }  
}  
```  
Response (error):  
```jsonc  
// HTTP 404  
{  
	"success": false,  
	"msg": "Unknown username",  
	"obj": null  
}  
// HTTP 502  
{  
	"success": false,  
	"msg": "Panel rejected UUID update",  
	"obj": null  
}  
```  
  
---  
  
### POST /api/user/update  
Description: Update selected fields for a user. Omitted fields are left unchanged.  
Authentication: header  
Body:  
```jsonc  
{  
    "user": "",            // str, internal username (required)  
    "displayname": "",     // OPTIONAL str  
    "fingerprint": "",     // OPTIONAL str, must be in cfg fingerprints  
    "limit": 0,            // OPTIONAL int >= 0, monthly GB limit (0 = unlimited)  
    "wl_limit": 0,         // OPTIONAL int >= 0, whitelist monthly GB limit  
    "time": 0              // OPTIONAL int >= 0, expiry unix timestamp (0 = unlimited)  
}  
```  
Response (success):  
```jsonc  
// HTTP 200  
{"success": true, "msg": "Updated", "obj": null}  
```  
  
---  
  
### GET /api/user/history  
Description: Daily bandwidth history for a user, same snapshots as `/webapi/history`.  
Authentication: header  
Args:  
    user: Internal username.  
    days: optional int, clamped to 1-90 (default 30).  
Response (success):  
```jsonc  
// HTTP 200  
{"success": true, "msg": null, "obj": [{"ts": 0, "up": 0, "down": 0, "wl_up": 0, "wl_down": 0}]}  
```  
  
---  
  
### GET /api/fingerprints  
Description: Allowed TLS fingerprints from config.  
Authentication: header  
Body: none  
Response (success):  
```jsonc  
// HTTP 200  
{"success": true, "msg": null, "obj": ["chrome", "firefox"]}  
```  
  
---  
  
### GET /api/health  
Description: Lightweight liveness check. Pings the database and reports process stats. Does not contact panels.  
Authentication: header  
Body: none  
Response (success):  
```jsonc  
// HTTP 200  
{  
	"success": true,  
	"msg": null,  
	"obj": {  
		"db": true,  
		"uptime": 432000.0, // float, process uptime in seconds  
		"memory": {"ram": 0.0, "swap": 0.0}, // float, MB  
		"threads": 12, // int  
		"degraded": false, // bool, true if a snapshot or rollback marker is set  
		"daily_snapshot_failure": null, // or {"ts": 0, "failed": 0, "eligible": 0}  
		"rollback_failures": {"uuid": {}, "registration": {}}  
	}  
}  
```  
Response (error):  
```jsonc  
// HTTP 503, database ping failed. obj has the same shape with "db": false  
{  
	"success": false,  
	"msg": "Database unavailable",  
	"obj": {"db": false}  
}  
```  
  
---  
  
## Admin UI  

Pages are mounted on the service URI, not under the admin API prefix. The UI uses a login form (no Register tab) against `api_admin_ui_auth`. There is one admin account. A successful login replaces the stored session and sets an HttpOnly `admin_ui` cookie on `/{uri}/admin`; the API token is never stored in `localStorage`. Logout clears that session, which also signs out any other browser still using it.  

### GET /{uri}/admin  
Description: Admin HTML UI.  
Authentication: `admin_ui` cookie  
Body: none  
Response (success): HTML document (`admin.html`).  
Response (error): HTTP 302 to `/{uri}/admin/login`.  

### GET /{uri}/admin/login  
Description: Admin login form. Same fields as the user login form, without a Register tab.  
Authentication: none  
Body: none  
Response (success): HTML document (`admin-auth.html`).  

### POST /{uri}/admin/session  
Description: Sign in with the configured admin UI username and password. Replaces the single stored session, so a new login signs out the previous one. Rate-limited.  
Authentication: none  
Body: `{"username": "...", "password": "..."}`  
Response (success): HTTP 200 and `Set-Cookie: admin_ui=...; HttpOnly; Secure; SameSite=Lax; Path=/{uri}/admin`.  
Response (error): HTTP 401 `{"success": false, "msg": "Invalid credentials.", "obj": null}`. A missing, empty, or non-string username or password is also 401, not 500. A body that is not a JSON object is HTTP 400.  

### POST /{uri}/admin/logout  
Description: Clear the stored admin UI session and expire the cookie.  
Authentication: none (the stored session is cleared either way)  
Body: none  
Response (success): HTTP 200 and `Set-Cookie: admin_ui=; Max-Age=0; Path=/{uri}/admin`.  

### GET /{uri}/admin/token  
Description: Bootstrap the admin UI. Returns the admin API token and the already-computed API root so the page does not guess paths. Token and `api_root` stay in memory only.  
Authentication: `admin_ui` cookie  
Body: none  
Response (success):  
```jsonc  
// HTTP 200  
{  
	"success": true,  
	"msg": null,  
	"obj": {  
		"token": "admin-api-token",  
		"api_root": "/sub/privapi" // or "/sub" when api_uri is empty  
	}  
}  
```  
Response (error): HTTP 401. The UI redirects to `/{uri}/admin/login`.  
  
---    
  
### GET /api/panel/status  
Description: Get status of one or all panels.  
Authentication: header  
Args:  
    name: (optional) Panel name. If omitted, returns status for all panels (long, blocking).  
Response (success):  
```jsonc  
// HTTP 200  
{  
    "success": true,  
    "msg": null,  
    "obj": {  
        "PanelName": {  
            "success": true,  
            "msg": "",  
            "obj": {  
                "cpu": 0,  
                "mem": {"current": 0, "total": 0}  
                // remaining 3x-ui server metrics  
            }  
        }  
        // or {"status": "unknown"} when that panel did not answer  
    }  
    // or one 3x-ui envelope if ?name= was provided  
}  
```  
Response (error):  
```jsonc  
// HTTP 404  
{  
	"success": false,  
	"msg": "panel not found",  
	"obj": null  
}  
```  
  
---  
  
### GET /api/code/list  
Description: List all bonus/invite code names. Returns names only, with no metadata.  
Authentication: header  
Body: none  
Response (success):  
```jsonc  
// HTTP 200  
{  
    "success": true,  
    "msg": null,  
    "obj": ["code1", "code2"] // list of code-name strings  
}  
```  
  
---  
  
### GET /api/code/info  
Description: Get info about a specific code.  
Authentication: header  
Args:  
    code: The code string.  
Response (success):  
```jsonc  
// HTTP 200  
{  
    "success": true,  
    "msg": null,  
    "obj": {  
        "action": "",   // str, code action type  
        "perma": false, // bool, reusable  
        "uses": 0,      // int, safe to ignore if perma == true  
                        // NOTE: this is usually omitted or -1 when perma == true  
        "days": 0,      // int  
        "gb": 0,        // int  
        "wl_gb": 0      // int  
    }  
}  
```  
Response (error):  
```jsonc  
// HTTP 404  
{  
	"success": false,  
	"msg": "Unknown code",  
	"obj": null  
}  
```  
  
---  
  
### POST /api/code/add  
Description: Create a new bonus/invite code.  
Authentication: header  
Body:  
```jsonc  
{  
    "code": "",      // str, code string (required)  
    "action": "",    // str, only 'register' or 'bonus', code action (required)  
    "perma": false,  // OPTIONAL bool-like (true, 'yes', '1', 'on', 'y'), reusable (default: false)  
    "days": 0,       // OPTIONAL int, days to add (0 leaves an existing expiry unchanged)  
    "gb": 0,         // OPTIONAL int, GB added onto the monthly limit, including a 0 limit  
    "wl_gb": 0,      // OPTIONAL int, GB added onto the whitelist limit, including a 0 limit  
    "uses": 0        // OPTIONAL int, amount of uses. ignored if perma == true, defaults to 1 use  
}  
```  
For `action: "bonus"`, a stored user limit of 0 is no allowance. Added GB is summed onto it. Expiry 0 stays unlimited. A lapsed finite expiry restarts from now before the granted days are added. A code with 0 days does not revive a lapsed expiry. Re-enable is deferred to the bandwidth watcher.  
Response (success):  
```jsonc  
// HTTP 201  
{  
	"success": true,  
	"msg": "Created",  
	"obj": null  
}  
```  
Response (error):  
```jsonc  
// HTTP 400  
{  
	"success": false,  
	"msg": "code must be a non-empty string", // or another validation error  
	"obj": null  
}  
// HTTP 409  
{  
	"success": false,  
	"msg": "code '<code>' already exists",  
	"obj": null  
}  
```  
  
---  
  
### POST /api/code/delete  
Description: Delete a code.  
Authentication: header  
Body:  
```jsonc  
{  
    "code": "" // str, code name  
}  
```  
Response (success):  
```jsonc  
// HTTP 200  
{  
	"success": true,  
	"msg": "Deleted",  
	"obj": null  
}  
```  
Response (error):  
```jsonc  
// HTTP 404  
{  
	"success": false,  
	"msg": "Unknown code",  
	"obj": null  
}  
```  
  
---  
  
### GET /api/logs/audit  
  
Description: Return the most recent audit records from the server activity log.  
Authentication: header  
Params:  
	n: number of most recent records to retrieve (optional, integer, default 50).  
       Use 0 to return all records (not recommended for very large logs).  
  
Response (success):  
```jsonc  
// HTTP 200  
{  
	"success": true,  
	"msg": null,  
	"obj": [  
		{  
			"ts": 1778089999.296969,       // float, precise timestamp  
			"date": "06.05.2026 15:44:30", // str, string in "%d.%m.%Y %H:%M:%S" format, UTC as always  
			"action": "sub_hit",           // str, action name  
			"info": {}                     // dict, information about an action. empty if none (not null)  
		}  
		// ...  
	]  
}  
```  
Response (error):  
```jsonc  
// HTTP 400  
{  
    "success": false,  
    "msg": "'n' arg must be a positive integer, or 0 for the whole file",  
    "obj": null  
}  
```  
  
---  
  
### POST /api/state/snapshots  
Description: Get state snapshots (system + panels).  
Authorization: header  
Body:  
```jsonc  
{  
    "cutoff": 0, // int, clamp the data to a certain amount  
                 // must be > 0  
}  
```  
Response (success):  
```jsonc  
{  
  "success": true,  
  "msg": null,  
  "obj": [  
  // object list  
  {  
  
  // sorry for broken indentation  
  
  "ts": 1717200000, // int, unix timestamp at start of day (midnight UTC)  
  "host": {  
    "cpu": 23.5,           // float, current CPU usage percentage  
    "process_count": 187,  // int, total number of running processes  
    "uptime": 864000.0,    // float, system uptime in seconds (~10 days)  
    "cpu_info": {  
      "cores": 4,                       // int, number of CPU cores  
      "name": "Intel Xeon E5-2680 v4",  // str, CPU model name  
      "mhz_max": 3400.0                 // float, maximum CPU frequency in MHz  
    },  
    "loadavg": {  
      "load_1m": 0.42,  // float, load average over 1 minute  
      "load_5m": 0.38,  // float, load average over 5 minutes  
      "load_15m": 0.31  // float, load average over 15 minutes  
    },  
    "network": {  
      "sent": 1073741824,  // int, total bytes sent since boot  
      "recv": 2147483648   // int, total bytes received since boot  
    },  
    "memory": {  
      "ram": {  
        "total": 8589934592,     // int, total RAM in bytes  
        "available": 4294967296, // int, available RAM in bytes  
        "used": 4294967296       // int, used RAM in bytes  
      },  
      "swap": {  
        "total": 4294967296, // int, total swap space in bytes  
        "free": 3221225472,  // int, free swap space in bytes  
        "used": 1073741824   // int, used swap space in bytes  
      }  
    },  
    "ip": {  
      "ipv4": ["192.168.1.100", "10.0.0.5"], // array[str], IPv4 addresses  
      "ipv6": ["fe80::1", "2001:db8::1"]     // array[str], IPv6 addresses  
    },  
    "connections": {  
      "tcp": 142, // int, number of active TCP connections  
      "udp": 18   // int, number of active UDP connections  
    },  
    "app_memory": {  
      "ram": 524288000.0, // float, RSS of this process and its children, in bytes (500 MB)  
      "swap": 0.0         // float, swapped-out memory of this process and its children, in bytes  
    },  
    "app_uptime": 432000.0,  // float, application uptime in seconds (~5 days)  
    "app_thread_amount": 12, // int, total number of application threads  
    "app_threads": [  
      {  
        "tid": 1024,            // int, OS thread id  
        "name": "MainThread",   // str, kernel thread name  
        "state": "sleeping",    // str | null, psutil status  
        "cpu": 12.4,            // float | null, user + system CPU seconds  
        "ctx_switches": 4021,   // int | null, voluntary + involuntary  
        "stack": 65536          // int | null, stack reservation in bytes  
      }  
      // ...  
    ],  
    "app_gc_stats": {  
      "gc_counts": [42, 8, 1],                        // array[int], GC collection counts per generation (gen0, gen1, gen2)  
      "gc_thresholds": [700000, 10000000, 100000000], // array[int], GC thresholds per generation in bytes  
      "gc_stats": [  
        {  
          "collections": 42,  // int, number of collections for gen0  
          "collected": 1337,  // int, objects collected in gen0  
          "uncollectable": 0  // int, uncollectable objects in gen0  
        }  
        // ...  
      ]  
    }  
  },  
  "panels": {  
    "server-node-1": {  
      "cpu": 15.7,           // float, panel server CPU usage percentage  
      "cpuCores": 4,         // int, number of CPU cores on panel server  
      "logicalPro": 8,       // int, number of logical processors  
      "cpuSpeedMhz": 3100.5, // float, current CPU speed in MHz  
      "mem": {  
        "current": 536870912, // int, current memory usage in bytes (512 MB)  
        "total": 2147483648   // int, total memory in bytes (2 GB)  
      },  
      "swap": {  
        "current": 0,       // int, current swap usage in bytes  
        "total": 1073741824 // int, total swap space in bytes (1 GB)  
      },  
      "disk": {  
        "current": 53687091200, // int, Current disk usage in bytes (50 GB)  
        "total": 107374182400   // int, Total disk space in bytes (100 GB)  
      },  
      "xray": {  
        "state": "running", // str, xray service state (running/stopped/error)  
        "errorMsg": "",     // str, error message if state is error (empty if ok)  
        "version": "v1.8.4" // str, xray core version  
      },  
      "uptime": 172800,            // int, panel server uptime in seconds (~2 days)  
      "loads": [0.25, 0.30, 0.28], // array[float], Load averages (1m, 5m, 15m)  
      "tcpCount": 87,              // int, number of TCP connections  
      "udpCount": 12,              // int, number of UDP connections  
      "netIO": {  
        "up": 1048576,  // int, upload bytes in current period  
        "down": 2097152 // int, download bytes in current period  
      },  
      "netTraffic": {  
        "sent": 107374182400,  // int, total bytes sent (100 GB)  
        "recv": 536870912000   // int, total bytes received (500 GB)  
      },  
      "publicIP": {  
        "ipv4": "203.0.113.50",  // str, public IPv4 address  
        "ipv6": "2001:db8::100"  // str, public IPv6 address  
      },  
      "appStats": {  
        "threads": 24,    // int, number of application threads  
        "mem": 268435456, // int, application memory usage in bytes (256 MB)  
        "uptime": 432000  // int, application uptime in seconds (~5 days)  
      }  
    }  
    // ...  
  }  
}]  
  
}  
```  
Response (error):  
```jsonc  
{  
    "success": false,  
    "msg": "cutoff param must be higher than 0",  
    "obj": null  
}  
```  
  
---  
  
### GET /api/state/all  
Description: Latest system + per-panel info, combines 2 endpoints.  
Authorization: header  
Response (success):  
```jsonc  
{  
    "success": true,  
    "msg": null,  
    "obj": {  
        "host": {  
            // SysUtil.full_info() object, as a dict  
        },  
        "panels": {  
            "node-1": {  
                // state object, as a dict  
            }  
            // ...  
        }  
    }  
}  
```  
  
---  
  
### GET /api/state/system  
Description: Get system info from SysUtil.  
Authorization: header  
Response (success):  
```jsonc  
{  
    "success": true,  
    "msg": null,  
    "obj": {  
        // SysUtil.full_info() as a dict  
    }  
}  
```  
  
---  
  
### GET /api/state/polling  
Description: Limited dynamic host and panel state for frequent GET polling. Not a stream. Use `/api/state/system` or `/api/state/all` for static identity (`cpu_info`, `ip`, GC, `cpuCores`, `xray`, `publicIP`).  
Authentication: header  
Body: none  
CPU does not sleep. Host `cpu` is the percentage since the previous sample on this process; the first call has no baseline and returns `0.0`. Panel `cpu` is that panel host's percentage, not this process.  
Host `uptime` / `app_uptime` and panel `uptime` / `app_stats.uptime` are included because they change every poll. Host network is bytes since boot (`sent` out, `recv` in). Panel `netIO` is the instantaneous rate (bytes/s); panel `netTraffic` is totals (`sent` out, `recv` in).  
A panel that does not answer is `null`. One down panel does not fail the poll.  
Response (success):  
```jsonc  
// HTTP 200  
{  
    "success": true,  
    "msg": null,  
    "obj": {  
        "host": {  
            "cpu": 1.5,              // float, % since the previous sample (0.0 on the first call)  
            "process_count": 4,      // int  
            "uptime": 1000.5,        // float, seconds since boot  
            "loadavg": {  
                "load_1m": 0.1,  
                "load_5m": 0.2,  
                "load_15m": 0.3  
            },  
            "network": {"sent": 1, "recv": 2}, // int, bytes since boot (sent = out, recv = in)  
            "memory": {  
                "ram": {"total": 3, "available": 2, "used": 1}, // int, bytes  
                "swap": {"total": 0, "free": 0, "used": 0}  
            },  
            "connections": {"tcp": 5, "udp": 6},  
            "app_memory": {"ram": 7.0, "swap": 0.0}, // float, bytes  
            "app_uptime": 200.25,    // float, seconds since this process started  
            "app_thread_amount": 1,  
            "app_threads": [  
                {  
                    "tid": 1024,  
                    "name": "MainThread",  
                    "state": "sleeping",    // str | null  
                    "cpu": 12.4,            // float | null, user + system CPU seconds  
                    "ctx_switches": 4021,   // int | null  
                    "stack": 65536          // int | null, stack reservation in bytes  
                }  
            ]  
        },  
        "panels": {  
            "edge": {  
                "app_stats": {"threads": 2, "mem": 3, "uptime": 4}, // uptime: panel app seconds  
                "cpu": 12.5,                 // int | float, panel host %  
                "disk": {"current": 3, "total": 4},  
                "loads": [0.1, 0.2, 0.3],    // array[float], 1m, 5m, 15m  
                "mem": {"current": 10, "total": 20},  
                "netIO": {"up": 1, "down": 2},       // int, bytes/s  
                "netTraffic": {"sent": 9, "recv": 10}, // int, total bytes (sent = out, recv = in)  
                "swap": {"current": 1, "total": 2},  
                "tcpCount": 7,  
                "udpCount": 8,  
                "uptime": 99                 // int, panel host seconds  
            },  
            "down": null // panel did not answer  
        }  
    }  
}  
```  
  
---  
  
### POST /api/leaderboard  
Description: Get leaderboard data for a specified bandwidth type.  
Authorization: header  
Body:  
```jsonc  
{  
    "type": "total",       // str, type of bandwidth to use  
                           // allowed values: "total" (all-time), "monthly", "wl_monthly"  
    "cutoff": 0,           // OPTIONAL int, clamp the leaderboard to a certain amount,  
                           // if <= 0 or omitted, returns a leaderboard with all users  
    "displaynames": false, // OPTIONAL bool, if True, uses displaynames as dict keys  
                           // instead of internal usernames  
    "flip": false,         // OPTIONAL bool, if True, sorted in ascending order  
                           // instead of descending  
}  
```  
Response (success):  
```jsonc  
{  
    "success": true,  
    "msg": null,  
    "obj": [  
        // starts from 1  
        {"place": 1, "username": "user", "amount": 745343740637},  
        {"place": 2, "username": "name123", "amount": 338228869611}  
    ]  
}  
```  
  
---  
  
### GET /api/teapot  
Description: Verify the server cannot brew coffee because it is a teapot.  
Authorization: None  
Response (success):  
```jsonc  
// HTTP 418  
{  
	"teapot": true // Or false if its feeling fancy  
}  
```  
  
---  
