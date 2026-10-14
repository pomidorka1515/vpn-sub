# Async Telegram (phase 9)  
  
`TelegramAdminBot` and `TelegramPublicBot` replace the synchronous bots in place, retaining the admin/public feature modules, localized keyboards, commands, callback payloads, and business validation. Both dependency manifests require pyTelegramBotAPI 4.37–4.x; compatibility was inspected and tested against 4.37.0. Constructors register handlers without starting polling or opening sessions.  
  
## Library and lifecycle  
  
AsyncTeleBot provides awaited sends, `get_updates()` and `process_new_updates()`, but no synchronous-style next-step API. Its stock polling detaches update tasks and closes the shared `asyncio_helper` session on exit. `stop_poll()` stops a Telegram poll message, not update polling. The service therefore owns polling directly: 20-second long polls, 30-second request timeout, bounded eight-update batches, interruptible three-second retry, and Telegram `retry_after` hints. Handler exceptions use an async exception handler with sanitized token/URL text.  
  
Each entry handler creates a named, bounded, shielded owned task. Polling cancellation cannot orphan an active handler. Close stops admission and polling, drains handlers for 30 seconds, then cancels and settles remaining handlers. Core mutations retain their separate runtime ownership. `AsyncTelegramSession` closes telebot's shared client only after both bots, watcher delivery and queued log alerts settle. Standalone bot compositions must supply the same session owner. Repeated close is safe; stopped instances cannot be restarted.  
  
`create_application()` constructs configured bots only when `start_bots` is enabled and their token is nonempty. Public notices propagate send failures so accepted markers retain their existing semantics. Admin watcher alerts and queued `AsyncTelegramLogger` delivery await `msg()`. Synchronous Telegram logging and bot implementations have been removed at the user's explicit request; synchronous watcher tests now use delivery protocols pending phase 12 retirement.  
  
## Conversation transitions  
  
Each bot owns a `ConversationRouter`, keyed by `(chat_id, user_id)` within that bot instance. A record holds the named bound step, temporary arguments and a ten-minute monotonic expiry. Dispatch removes the record before invoking its step. A per-conversation lock serializes text and callbacks; locks disappear after their final borrower. State admission is capped at 1,024 records. Admin callback wizard values use the same user scope, expiry and capacity through `PendingValues`.  
  
Every former next-step registration is now `_expect(prompt, step, *arguments)`:  
  
| Workflow | Expected steps / transitions |  
| --- | --- |  
| Public login | `step_login_email` → `step_login_pass(email)` → credential validation and Telegram mapping |  
| Public settings | `step_settings_name`, `step_settings_login`, `step_settings_pass` → coordinated parameter update; fingerprint stays callback-based |  
| Public account/subscription | `step_bonus`, `step_reset`, `step_delete` → existing validation/confirmation and mutation |  
| Admin user creation | `_step_add_user_name` → `_step_add_user_display` → `_step_add_user_limit` → `_step_add_user_time` → creation |  
| Admin user edits | `_step_edit_limit`, `_step_edit_wl_limit`, `_step_edit_time`, `_step_edit_name`; fingerprint stays callback-based |  
| Admin reset | `_step_reset_user` → UUID reset |  
| Admin codes | `_step_info_code`, `_step_del_code`; creation `_step_add_code_name` → type callback → `_step_add_code_days` → `_step_add_code_time` → `_step_add_code_wl_time` → `_step_add_code_perma` → `_step_add_code_uses` if finite |  
| Admin chart | `_step_chart_username` → `_step_chart_days` → history/render/send |  
| Admin leaderboard | type callback → order callback → `_step_leaderboard_window` → result |  
  
`/start`, `/menu`, other slash cancellation and localized menu buttons clear the active workflow. Callbacks discard a pending text step before choosing another action. Invalid input retains the original cancel-on-invalid behavior. Expired records fall through to normal routing. Nontext messages do not consume steps. Admin membership is checked again for every entry transition. Passwords are never stored in router state; consumed email arguments are released with the step. State remains in-memory and is discarded on restart/shutdown.  
  
History/business reads and Telegram sends are awaited; bandwidth charts, leaderboard charts and QR generation use the core's shared bounded rendering offload. Callback acknowledgements precede rendering and panel work.  
  
## Verification  
  
Existing bot suites were converted to async with service-aware mocks, preserving menu/routing/validation/rendering coverage. Additional tests exercise concurrent messages, independent users, expiry, menu cancellation, per-transition admin revocation, callback-state isolation, cancelled polling waiters, draining, error redaction and optional production bot wiring. Required checks: `venv/bin/ruff check .`, `venv/bin/mypy`, `venv/bin/pyright`, `venv/bin/pytest`.  
