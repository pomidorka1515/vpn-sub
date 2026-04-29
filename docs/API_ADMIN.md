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
s
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
// HTTP 400  
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
// HTTP 400  
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
Authentication: header
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
    name: (optional) Panel name. If omitted, returns status for all panels (long).  
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
// HTTP 400  
{  
	"success": false,  
	"msg": "panel not found",  
	"obj": null  
}  
```  

---  

### GET /api/code/list  
Description: List all bonus/invite codes.  
Authentication: header  
Body: none  
Response (success):  
```jsonc  
// HTTP 200  
{  
    "success": true,  
    "msg": null,  
    "obj": { } // dict of codes  
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
    "action": "",    // str, code action (required)  
    "perma": false,  // OPTIONAL bool-like (true, 'yes', '1', 'on', 'y'), reusable (default: false)  
    "days": 0,       // OPTIONAL int, days to add  
    "gb": 0,         // OPTIONAL int, GB to add to monthly limit  
    "wl_gb": 0       // OPTIONAL int, GB to add to whitelist limit  
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
    "code": "" // str, code string (required)  
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

### GET /api/health
Description: Verify connectivity.  
Auth: None  
Response (success):  
```jsonc  
// HTTP 204
// <no content>
```  