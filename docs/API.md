# Public API Documentation  

## Introduction  
Authentication: a cookie named 'token'.  
Cookie is httponly, samesite=lax, secure=True  

## Root URI (subject to change)  
`https://pomi.lol/sub/webapi`  

## Response format  
Every response follows this pattern:  
```jsonc  
// HTTP x  
{  
    "success": true, // boolean  
    "msg": null, // str on error/success (varies), null on success // use HTTP codes to detect faliures  
    "obj": null // any object or null  
}  
```  
Missing authorization:  
```jsonc  
// HTTP 401  
{  
    "success": false, 
    "msg": "Invalid token.", 
    "obj": null  
}  
```  
Rate limiting:  
Every `Rate limit` field in routes are in requests / min.  
Response:  
```jsonc  
// HTTP 429  
{  
    "success": false, 
    "msg": "429 Too many requests", 
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

---  

### POST /register  
Description: Register a new account with an invite code.  
Rate limit: 5  
Authentication: none (must NOT have a token cookie set)  
Body:  
```jsonc  
{  
    "username": "", // str, only A-Z a-z 0-9 _ - allowed  
    "password": "", // str  
    "code": "", // str, invite code  
    "name": "" // str, display name  
}  
```  
Response (success):  
```jsonc  
// HTTP 201  
{  
    "success": true,  
    "msg": "Created",  
    "obj": {  
        "username": "", // internal username (web_xxxxx)  
        "token": "", // subscription token  
        "uuid": "", // user UUID  
        "fingerprint": "", // assigned TLS fingerprint  
        "displayname": "" // sanitized display name  
    }  
}  
```  
Response (error):  
```jsonc  
// HTTP 403  
{"success": false, "msg": "Invalid code", "obj": null}  
{"success": false, "msg": "Username already exists", "obj": null}  
// HTTP 400  
{"success": false, "msg": "Missing 'username' key in JSON.", "obj": null}  
```  

---  

### POST /login  
Description: Log in using username and password. Sets a token cookie (30 days).  
Rate limit: 10  
Authentication: none  
Body:  
```jsonc  
{  
    "username": "", // str  
    "password": "" // str  
}  
```  
Response (success):  
```jsonc  
// HTTP 200, sets cookie 'token'  
{  
    "success": true,  
    "msg": "Successful login",  
    "obj": {  
        "username": "" // the username you logged in with  
    }  
}  
```  
Response (error):  
```jsonc  
// HTTP 401  
{"success": false, "msg": "Invalid credentials.", "obj": null}  
// HTTP 400  
{"success": false, "msg": "Missing 'username' key in JSON.", "obj": null}  
```  

---  

### POST /bonus  
Description: Apply a bonus code to your account.  
Rate limit: 15  
Authentication: cookie  
Body:  
```jsonc  
{  
    "code": "" // str, bonus code  
}  
```  
Response (success):  
```jsonc  
// HTTP 200  
{  
    "success": true,  
    "msg": null,  
    "obj": {  
        "perma": false, // bool, whether code is permanent (reusable)  
        "days": 0, // int, days added  
        "gb": 0, // int, GB added to monthly limit  
        "wl_gb": 0 // int, GB added to whitelist limit  
    }  
}  
```  
Response (error):  
```jsonc  
// HTTP 200 (code not found)  
{"success": false, "msg": "Unknown code", "obj": null}  
// HTTP 400  
{"success": false, "msg": "Missing 'code' key in JSON.", "obj": null}  
```  

---  

### GET /stats  
Description: Get full info about yourself.  
Rate limit: 20  
Authentication: cookie  
Body: none  
Response (success):  
```jsonc  
// HTTP 200  
{  
    "success": true,  
    "msg": null,  
    "obj": {  
        "_": "", // str, random funny string  
        "token": "", // str, subscription token  
        "link": "", // str, full subscription link  
        "displayname": "", // str  
        "uuid": "", // str  
        "fingerprint": "", // str, TLS fingerprint  
        "enabled": true, // bool, account active  
        "wl_enabled": true, // bool, active on whitelist locations  
        "time": 0, // int, expiry unix timestamp (0 = unlimited)  
        "online": false, // bool  
        "bandwidth": {  
            "total": {  
                "upload": 0, // int, bytes (all-time, live from panels)  
                "download": 0 // int, bytes  
            },  
            "wl_total": {  
                "upload": 0, // int, bytes (whitelist, live from panels)  
                "download": 0 // int, bytes  
            },  
            "monthly": 0, // int, bytes used this month  
            "wl_monthly": 0, // int, bytes used this month (whitelist)  
            "limit": 0, // int, GB monthly limit (0 = unlimited)  
            "wl_limit": 0 // int, GB whitelist monthly limit (0 = unlimited)  
        }  
    }  
}  
```  

---  

### POST /reset  
Description: Reset your internal UUID and token. You will need to log in again after this!  
Rate limit: 3  
Authentication: cookie  
Body: none  
Response (success):  
```jsonc  
// HTTP 200  
{  
    "success": true,  
    "msg": null,  
    "obj": {  
        "uuid": "", // str, new UUID  
        "token": "" // str, new token  
    }  
}  
```  

---  

### POST /settings  
Description: Update your settings. All fields are optional, only provided fields are updated.  
Rate limit: 10  
Authentication: cookie  
Body:  
```jsonc  
{  
    "name": "", // OPTIONAL str, display name  
    "fingerprint": "", // OPTIONAL str, TLS fingerprint  
    "username": "", // OPTIONAL str, requires password too  
    "password": "" // OPTIONAL str, requires username too  
}  
```  
Response (success):  
```jsonc  
// HTTP 200  
{"success": true, "msg": null, "obj": null}  
```  
Response (error):  
```jsonc  
// HTTP 400  
{"success": false, "msg": "Unknown fingerprint", "obj": null}  
{"success": false, "msg": "Both ext params needed", "obj": null}  
{"success": false, "msg": "Ext username exists", "obj": null}  
```  

---  

### POST /logout  
Description: Log out and delete your cookie.  
Rate limit: 20  
Authentication: cookie  
Body: none  
Response (success):  
```jsonc  
// HTTP 200  
{  
    "success": true, 
    "msg": "Logged out", 
    "obj": null  
}  
```  

---  

### GET /fingerprints  
Description: List of available TLS fingerprints.  
Rate limit: 60  
Authentication: cookie  
Body: none  
Response (success):  
```jsonc  
// HTTP 200  
{  
    "success": true, 
    "msg": null, 
    "obj": [] // ["chrome", "safari", "ios", "edge", "firefox", "qq", "360"]  
}  
```  

---  

### DELETE /delete  
Description: Delete your account. Irreversible.  
Rate limit: 3  
Authentication: cookie  
Body: none  
Response (success):  
```jsonc  
// HTTP 200  
{  
    "success": true, 
    "msg": "Deleted account", 
    "obj": null  
}  
```  

---  

### GET /validate  
Description: Validate a username for illegal characters and availability.  
Rate limit: 80  
Authentication: none  
Args:  
    username: Username to validate.  
Response (success):  
```jsonc  
// HTTP 200  
{  
    "success": true,  
    "msg": null,  
    "obj": {  
        "valid": true, // bool, true if no illegal chars and non-empty  
        "taken": true, // bool, username already registered  
        "sanitized": "" // str, username with illegal chars stripped  
    }  
}  
```  

---  

### GET /qr  
Description: Generate a QR code of your subscription link.  
Rate limit: 80  
Authentication: cookie  
Params:  
    happ: if 1, append happ://add/ to the start of the link  
Response (success):  
**Returns an image (PNG)**. Be careful.  
`HTTP 200`, Mimetype: `image/png`  

---  

### GET /profiles
Description: Get all currently available profiles and their descriptions.
Rate limit: 60
Authentication: cookie
Params:
    lang: language. either 'en' or 'ru'
Response (success):
```jsonc
// HTTP 200
{
    "success": true,
    "msg": null,
    "obj": {
        "[ Profile Name ]": "Some long description...",
        "[ Another Profile ]": "Another description"
    }
}
```

---

### GET /history
Description: Get daily bandwidth history for yourself.
Rate limit: 30
Authentication: cookie
Params:
    days: (optional) int, number of days to return (default: 30, max: 90).
Response (success):
```jsonc
// HTTP 200
{
    "success": true,
    "msg": null,
    "obj": [
        {
            "ts": 1714000000,   // int, unix timestamp (UTC midnight of the day)
            "up": 500000000,    // int, upload bytes used this day
            "down": 1200000000, // int, download bytes used this day
            "wl_up": 0,         // int, whitelist upload bytes
            "wl_down": 0        // int, whitelist download bytes
        },
        {
        	// Same object, a day earlier...
        }
    ]
}
```
Data is newest-first. Entries older than the retention window (90 days) are not returned.
