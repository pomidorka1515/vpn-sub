# Async config and durable file boundaries (Step 5)  
  
`config.store.AsyncConfig[Doc]` and `AsyncLinesConfig` are injectable runtime resources. Constructors only assemble dependencies; `start()` invokes the supplied synchronous store factory through the runtime's bounded file offload service. Factories must pass `start_backup=False`. The synchronous config APIs remain available to existing callers and the separate Discord process. Production composition uses these resources. The synchronous config stores and JSONL logger remain supported for the separate Discord process.  
  
## Config snapshots and operations  
  
- `view()` deep-copies the published in-memory document without file checks or thread locks. Nested caller edits cannot change the published state.  
- `mutate(callback)` offloads the complete existing transaction: reload under the inter-process lock, callback, schema validation, atomic replacement, configured fsync, and detached committed snapshot. Callbacks are synchronous and execute in the worker; they must not call async services. Publication happens on the event loop only after successful completion. Failed commits keep the previous published snapshot.  
- `reload()` offloads complete reload/validation and publishes only a valid result. Invalid external edits retain the previous snapshot and surface errors; scheduled reload failures are logged and retried on the next cycle. Commits use the existing transaction freshness checks, so callbacks work against the latest valid disk document rather than overwriting external changes with a stale snapshot.  
- Config operations have bounded admission (32), serialize with an async lock, and remain owned after waiter cancellation. Admitted commits finish and publish before shutdown releases the store. Cancellation is not proof that a commit did not occur; callers must observe state before retrying.  
- `FileSchedules` owns named reload/backup tasks. Jobs wait after completion, do not overlap or accumulate catch-up work, propagate cancellation, and log failures without killing later cycles. Manual and scheduled backups share operation serialization. Existing backup format and retention remain intact.  
- Start/close tasks are retained and shielded. Close stops admission, stops schedules, drains admitted operations, then offloads synchronous closure. The injected file offload service remains available until these resources have closed.  
  
## Audit acknowledgement  
  
`AuditService.audit()` preserves the existing action/date/timestamp/info envelope and optional disabled store behavior. Its `AsyncLinesConfig.append()` boundary copies inputs, runs the complete locked JSONL append and configured fsync off-loop, and acknowledges only after completion. Errors propagate rather than being dropped or converted to best-effort logs. Cancellation after admission leaves an owned append that shutdown drains; it does not retract an already accepted audit record. Audit is called at the same business-operation points when core callers migrate in Step 6; it does not make mutation and audit persistence one atomic transaction.  
  
## Log delivery  
  
`loggers.delivery` supplies JSONL and Telegram handlers plus `LogDelivery[T]`. Runtime composition owns each delivery resource and supplies an awaitable destination (for Telegram, the later async bot). Handlers format/enqueue synchronously; one named consumer performs serial awaited delivery. JSONL retains existing fields and adds task names. Telegram preserves ANSI removal and HTML escaping.  
  
- Capacity defaults to 256 queued records, plus one active delivery. A bounded `queue.Queue` and thread-safe empty-to-nonempty loop wakeup support records emitted by offload threads without an unbounded callback per record.  
- Overflow drops the newest record and increments `dropped`. ERROR+ records fall back directly to stderr. No blocking file or Telegram call runs in `emit()`.  
- Delivery failures increment `failed` and emit a generic stderr message without transport exception text or recursive alert logging. A context guard suppresses records originating inside delivery, including inherited offload contexts.  
- Close stops admission and drains accepted records. The default five-second deadline raises on timeout while retaining the consumer; dependencies must remain open until a repeated close completes. It never silently cancels an active durable append.  
  
Verification: six async file/logging tests cover detached reads, failed reload/commit, external edits, cancellation and drain, audit fsync acknowledgement/errors, thread logging/overflow, recursion prevention, shutdown timeout, backups and non-overlapping owned schedules. Full pytest: 603 passed. Ruff, mypy and pyright pass.  
