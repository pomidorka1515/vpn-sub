# Admin API Documentation  
  
## Introduction  
Authentication: `Authorization` header with the admin API token.  
On failure, returns HTTP 401.  
  
## Root URI (subject to change)  
`{domain}/sub/{api_uri}` where `domain` is your domain.  
  
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
    "msg": "Internal server error", // Or exception as a str  
    "obj": null  
}  
```  
  
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
	"msg": "Username exists", // or type/validation error description  
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
	"msg": "Refreshed all users.",  
	"obj": null  
}  
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
    "obj": [] // list of online usernames, or dict if keyed=1  
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
```  
  
---  
  
  
  
---  
  
### GET /api/health  
Description: Lightweight health check. Returns HTTP 204 with no body on success.  
Authentication: none  
Body: none  
Response (success):  
```jsonc  
// HTTP 204  
(no body)  
```  
Use this to verify the API is reachable and the token is valid.  
  
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
        "PanelName": { } // status object per panel  
    }  
    // or a single status object if ?name= was provided  
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
	"msg": "Code not found",  
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
    "days": 0,       // OPTIONAL int, days to add  
    "gb": 0,         // OPTIONAL int, GB to add to monthly limit  
    "wl_gb": 0,      // OPTIONAL int, GB to add to whitelist limit  
    "uses": 0        // OPTIONAL int, amount of uses. ignored if perma == true, defaults to 1 use  
}  
```  
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
	"msg": "...",  
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
	"msg": "Code not found",  
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
      "ram": 524288000.0, // float, application RAM usage in bytes (500 MB)  
      "swap": 0.0         // float, application swap usage in bytes  
    },  
    "app_uptime": 432000.0,  // float, application uptime in seconds (~5 days)  
    "app_thread_amount": 12, // int, total number of application threads  
    "app_threads": [  
      {  
        "name": "MainThread",  // str, thread name  
        "ident": 140234567890, // int, thread identifier  
        "daemon": false        // bool, whether thread is a daemon thread  
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
