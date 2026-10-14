# Discord
  
Discord is bundled into `vpn-sub` and runs on the application's event loop.  
Public slash commands and `/admin` use one Discord application/token.  
  
Add to `data/config.json`:  
```json
"discord": {
    "token": "your Discord bot token",
    "whitelist": [123456789012345678]
}
```
  
Omit the section or leave `token` empty to disable Discord. `whitelist` contains  
Discord user IDs allowed to use `/admin`; an empty list allows no administrators.  
Restart after changing the token or whitelist.  
  
Commands call the application's shared `Subscription` services directly, using  
typed service results and config snapshots. Account workflows are shared with  
WebAPI; bulk refresh shares its bounded concurrency and partial-failure handling.  
Domain errors are handled at the Discord interaction boundary. Redis rate limits  
use the runtime-owned limiter with explicit Discord user IDs. QR images and charts  
use the shared rendering pool; subscription stats are fetched fresh each time.  
  
Sessions and language preferences are stored in SQLite. Existing  
`data/discord-sessions.json` is imported once when Discord first starts; the  
original is retained. Login, logout and account reset keep the WebAPI's shared  
auth-token semantics.  
Persisted tokens resolve directly through `user_svc.auth_token_to_user`. No session  
schema migration is needed. Discord startup is independent of API route registration.  
  
## Upgrade from the separate bot
  
1. Stop and disable the old Discord systemd unit.  
2. Copy `discord.json`'s `public.token` to `config.json`'s `discord.token`, and  
   `private.whitelist` to `discord.whitelist`. `private.api_token` is unnecessary.  
3. Keep `discord-sessions.json` in `DIR_DATA` for the first start.  
4. Start the main service. Remove the old config/binary after checking commands.  
  
`PATH_DISCORD_LANG` optionally overrides Discord's language asset. The checkout  
uses `src/discord/lang.jsonc`; releases pack it as `discord.lang.jsonc`.  
Implementation: `src/discord_bot/`. See [public API](../../docs/API.md) and  
[admin API](../../docs/API_ADMIN.md) for command behavior.  
