# Async core services (Step 6)  
  
`core.AsyncSubscription` composes framework-free async services over the native async repository, panel clients, snapshot readers, acknowledged audit service, injected `OffloadServices`, and a shared `MutationCoordinator`. Constructors open no resources and start no tasks. `start()` performs recovery; callers must complete it before admitting HTTP/bot/watcher work. `close()` stops mutation admission, drains critical operations and panel/cache tasks. Injected clients, stores, DB and offloads remain runtime-owned and close afterwards.  
  
## Coordination and cancellation  
  
- Public mutations run through the runtime's bounded `MutationRegistry` (32 by default). Per-user locks include queued borrowers and are removed when their final borrower settles. Multi-user keys acquire in sorted order. Nested service calls reuse ownership only in the owning task and cannot expand its lock set. Child tasks do not inherit lock bypass privileges.  
- Before admission cancellation starts no work. After admission the caller receives cancellation while its owned operation finishes. A retained settlement child also survives forced registry cancellation; shutdown cannot release dependencies until it finishes. There is deliberately no internal timeout that reports unfinished compensation as complete.  
- User/code registration, reset, delete, resync/reconcile, quota flags, limit/expiry/credential edits, bonus consumption and auth-token edits share coordination. SQL code consumption remains atomic independently of those locks. `apply_quota()` re-reads limits, usage and expiry under ownership; Step 8 must call this boundary rather than submit flags computed from an earlier snapshot.  
- Normal service reads remain cancellable. Password hash/verify runs through bounded password offload; legacy upgrades re-read credentials while coordinated with edits.  
  
## Fanout and caches  
  
Foreground/background have independent bounded fanout registries and four worker permits each. A fixed worker set consumes inputs incrementally, with results in panel order. Every started callback settles before the first ordered failure is raised. `PanelOutcome` exposes accepted, failed (explicit rejection), and unknown (availability/transport/unexpected failure) states. This intentionally fixes the previous early-return behavior during sibling writes. Work-class permits precede each client's existing total request admission; callbacks must not recursively invoke fanout. Saturated admission rejects work rather than accumulating unlimited tasks.  
  
Inbound and client caches use separate cross-process stamps, offloaded through a shared short disk-admission lock. Refresh captures both local epoch and disk generation before HTTP and publishes only if both remain current. A new generation never joins an old flight. Cached containers are detached; cancelled waiters leave the owned refresh alive. Failed refreshes remain retryable. Online classification uses an owned, epoch-aware single flight, four-second TTL, ordered emails, unavailable/invalid distinctions and live external-name mapping. Total outages are not cached.  
  
## Mutation persistence and recovery  
  
No schema migration or operation-journal table is introduced; existing `app_metadata` and pending-registration rows carry intent. No SQLite transaction spans external I/O.  
  
| Mutation | Persisted intent / ordering | Failure and startup policy |  
| --- | --- | --- |  
| Register with code | SQL atomically consumes code, creates user and pending registration before panel writes. Confirmation follows complete fanout. | Live-read/delete only matching UUIDs on every panel **before** deleting user/refunding code. Cleanup failure retains identity, pending row and `registration_rollback_failed:`. Startup includes pending rows even without markers. DB rollback atomically removes recovery/sync markers and refunds at most once. |  
| Admin creation | Creation and `user_create_pending:` UUID commit atomically before writes. | Same panel-first compensation. Startup retries cleanup, then atomically removes user/markers. Successful sync clears creation intent. |  
| Delete | `user_delete_pending:` records UUID and permanence before writes. | Retry live reads/deletes; retain marker on rejected/unknown outcomes. Startup validates identity and finishes deletion. DB removal follows panel settlement. |  
| Enable/disable | Desired flags and `user_sync_pending:` commit in one repository operation before writes. | Failure retains desired state plus retry marker. Startup/reconcile re-reads that state, ensures attachments and reapplies flags. Regular and whitelist writes remain separate. This is an intentional improvement over the old panel-first flags. |  
| Refresh/reconcile | Sync marker precedes panel writes. | Bounded sequential user processing; re-read each user after locking; idempotent create/attach with duplicate live-read handling. Retained registration/create/delete/UUID repair states are excluded from automatic reconcile. |  
| UUID | Existing `uuid_rollback_failed:` marker precedes writes; full rows preserve other panel fields. DB UUID follows all accepted outcomes. | Preserve existing manual-repair semantics: partial writes retain new panel UUIDs and old DB UUID. Marker names first ordered failure; never silently overwrite foreign identity. |  
| Reset | `user_reset_pending:` records target UUID/token before UUID operation. Final token publication, auth-token invalidation and intent removal are atomic. | Startup finalizes only if target DB UUID committed. Otherwise retain reset/UUID repair state for administration. |  
| Parameters / bonus | Re-read user under coordination; short repository commit, then acknowledged audit. | No external writes; existing SQL uniqueness and one-shot-code constraints remain authoritative. Audit failures propagate after commit, as before. |  
  
Panel mutations invalidate client caches on every attempt, including unknown outcomes. Recovery refuses a foreign UUID. Pending identities are reserved against competing workflows until recovery completes. UUID repair remains manual. A process kill can still land between a committed mutation and audit acknowledgement; metadata intent is recovery state, not an exactly-once audit/outbox journal. Subscription requests and later API/bot/watch callers must not expose pending registration identities as completed accounts.  
  
## Staging and synchronous bug fix  
  
Steps 7–10 switch production consumers to these services; the existing synchronous service stack remains until those callers migrate. The subscription route has already moved to `api/subscription.py`, and neither core resource bundle contains Flask/request state. The new async services import no HTTP framework and use no executors.  
  
The synchronous registration/admin-creation paths also receive the partial-write bug fix: fanout settles siblings before reporting failure; compensation deletes matching panel clients before deleting DB identity/refunding codes, retains identity on cleanup failure and recovers persisted failure markers.  
  
Verification includes user/credential/code/Telegram workflows, whitelist/accounting/leaderboard compatibility, one-shot registration races, reset/delete ordering, stale quota decisions, caller and forced-registry cancellation, bounded foreground/background admission, lost-response compensation, failed cleanup/refund recovery, UUID markers, creation/reset intent, cache generation races and cancelled single-flight waiters.  
  
Final checks: `venv/bin/ruff check .`, `venv/bin/mypy`, `venv/bin/pyright`, and `venv/bin/pytest -q`: **629 passed**, with four existing aiohttp BasicAuth deprecation warnings.  
