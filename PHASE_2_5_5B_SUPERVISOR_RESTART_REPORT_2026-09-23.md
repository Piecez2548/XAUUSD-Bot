# Phase 2.5.5B — Supervisor Restart / Process Ownership Hardening

## Final status

**AUTOMATED ACCEPTANCE: COMPLETE**

**MANUAL HAPPY-PATH WINDOWS RUNTIME ACCEPTANCE: PASSED**

**PHASE STATUS: ACCEPTED — AUTOMATED, WINDOWS HAPPY PATH, AND ISOLATED CONTROLLED FAILURE-PATH HARNESS COMPLETE**

**MANUAL FAILURE-PATH INJECTION AGAINST THE HEALTHY RUNTIME: NOT PERFORMED**

This report records the latest controlled Windows runtime acceptance supplied
by the operator. During this documentation update, the currently healthy
runtime, Telegram Control process, and live process registry were left
untouched. No lifecycle command was invoked by this update.

## Confirmed manual reproduction addendum

The later manual reproduction confirmed that the flashing-console symptom was
also exposing a lifecycle defect. After `/start`, `/stop` returned an
incomplete-operation response, Telegram reported `PROCESS_CRASHED` for `live`
with `Restart count: 2`, and subsequent MT5/live events appeared despite the
stop request. This is not a console-only problem.

The code audit proved the race: `monitor_once()` classified any exited child as
`CRASHED` without checking whether `stop_component()` had already expressed an
intentional stop. It could then increment `restart_count` and call
`start_component()` while `/stop` was verifying termination. There was no
persistent desired lifecycle state to suppress recovery.

The fix adds `desired_state` to the process registry. `start_component()` sets
it to `RUNNING`; `stop_component()` sets it to `STOPPED` before termination;
the monitor preserves `STOPPING` while an intentional child is still alive and
records a clean `STOPPED` exit without recovery or crash notification. Automatic
recovery is now permitted only when desired state is `RUNNING`. Registry loads
infer the desired state for older records so existing `RUNNING` entries remain
recoverable.

The Windows visibility issue was addressed separately. Supervisor-created
Python children retain the normal `python.exe` command and receive
`CREATE_NO_WINDOW` only when the Windows platform helper is true. `shell=False`
and redirected standard handles remain enforced; child output is now persisted
to private per-component diagnostics. The MT5 terminal launch and ownership
were not modified; only its background `tasklist` inspection helper receives a
visibility-only flag.

## Confirmed second manual acceptance failure

The clean second live reproduction also failed and Phase 2.5.5B is **not
accepted**. After `/start`, the API became unreachable, the supervisor reported
`PROCESS_CRASHED` with `Restart count: 4`, and the observed topology contained
`venv\\Scripts\\pythonw.exe` plus a base `Python311\\pythonw.exe` child. The
control task itself was launched by Task Scheduler with `pythonw.exe`.

The interpreter-selection root cause is proven in the pre-fix code:
`_start_infrastructure_locked()` and `_supervised_commands()` constructed both
API and live commands from `sys.executable`. Because the control process was
`pythonw.exe`, that exact executable was inherited by every supervised child.
The fix resolves a Windows `pythonw.exe` to the verified sibling
`python.exe` in the same active environment, applies `CREATE_NO_WINDOW` only
to those supervisor-created Python children, and fails closed if the sibling is
missing. Non-Windows command construction remains unchanged.

The proven API failure/diagnostics root cause is that the pre-fix supervisor
sent both child stdout and stderr to `DEVNULL`. Therefore the manual evidence
proves that the API child exited and was retried four times, but it does **not**
preserve the application exception needed to name the underlying API startup
fault. No surviving log or registry artifact contains that exception, so it
would be incorrect to invent one or declare the manual failure explained by
`pythonw.exe` alone. The fix now persists each supervised child’s combined
stdout/stderr in `logs/supervisor_api.log` or `logs/supervisor_live.log` and
records only a safe exit-code/path summary in lifecycle state; those contents
are not exposed through Telegram or the public API.

The visible flashing was therefore the combined result of the inherited
windowless-interpreter topology and repeated API crash recovery. The exact
application-level API exception remains a required observation in the next
manual run, using the new local diagnostic file.

## Stale registry investigation

The current `data/process_registry.json` was inspected read-only and was not
deleted, reset, or rewritten. Its API and live timestamps are from the earlier
13:00 BKK topology, not the clean approximately 13:32 BKK `/start`. The live
record still names dead historical PIDs `26600` and `18480` and the old
`pythonw.exe` command.

The code path explains why this old state could remain unchanged. Before this
fix, `/start` performed database migration and MT5 readiness before its first
supervisor status/reconciliation call. A migration failure returned a specific
database response, but an unexpected exception in the pre-Popen MT5/control
path escaped to `handle_update()`, which returned only `Operation failed;
inspect /health`. In that path no `ProcessSupervisor.status()` or `Popen()` was
reached, so no registry reconciliation or child diagnostic log could occur.
The surviving evidence cannot identify the exact runtime exception without the
control log, but it proves the failure occurred before a new supervised child
was spawned.

The startup path now calls explicit supervisor reconciliation first. A
conclusively absent persisted identity is persisted as `STOPPED` with no PID;
the desired state remains an operator-intent field and does not authorize
adoption or termination. A live-but-unverifiable PID, PID reuse, ambiguous
matching topology, or identity mismatch remains `DEGRADED` and fail-closed.
Only after reconciliation does `/start` proceed through migration, MT5
readiness, interpreter resolution, and child spawn. Unexpected pre-Popen MT5
failures now return a safe `START INCOMPLETE` response with the failed stage
and exception type while the logger retains the local traceback.

## Confirmed third manual acceptance failure — exact SystemError

The third clean manual attempt at approximately 13:54 BKK reached the new
reconciliation boundary and returned `SUPERVISOR=ERROR`, with no API/live
child and no registry change. The failure is now reproduced in isolation on
Windows and is not inferred from the exception class.

The exact failing line was `os.kill(pid, 0)` in `_pid_alive()` in
`services/supervisor.py`. For the stale PID `26600`, Windows first raised
`PermissionError: [WinError 5] Access is denied`; Python surfaced that failed
native call to the caller as:

`SystemError: <built-in function kill> returned a result with an exception set`

The historical PID `18480` returned absent normally. The PowerShell Python
snapshot contained neither historical PID, but `_pid_alive(26600)` was not
catching `SystemError`, so `status()` exited before its final `_save(records)`.
This exactly explains both the unchanged stale registry and the absence of
child diagnostic files: no `Popen()` was reached.

The fix catches this Windows probe failure without treating it as ownership
evidence. Reconciliation can complete; termination still requires verified
command, creation-time, and topology identity. The dedicated local file
`logs/supervisor_control_diagnostics.jsonl` now records UTC timestamp,
operation, stage, exception type, message, and traceback with configured MT5
and Telegram secrets redacted. Telegram receives only the concise stage/type
response.

## Confirmed fourth manual acceptance failure — post-spawn rollback

The fourth clean manual attempt confirmed that stale reconciliation now works:
the old PIDs were removed, both records ended at `STOPPED`, and the commands
were rewritten to the resolved `.venv\\Scripts\\python.exe`. The API then
successfully reached Uvicorn application startup and wrote no traceback. Its
final `exit_code=0` therefore describes the later controlled stop/rollback,
not an API application crash.

The exact rollback decision in the current control path is the explicit branch
after `_wait_for_startup_verification()`:

`if not verification["complete"]: _stop_failed_start(...)`

That branch can be reached after API and live have spawned. Completion requires
API health, live runtime health, fresh MT5 health, a complete/live snapshot,
database connectivity, forward-worker state, and a live process in `RUNNING`.
The pre-fix code did not persist the individual checks or rollback reason, so
this manual run proves normal post-spawn rollback but cannot identify which
verification check was false from the surviving files alone. It must not be
described as an API crash. The new lifecycle log records the verification
checks, rollback trigger, component PID/topology, verification reason, target,
and final state locally before/while rollback occurs.

The remaining console flashing source was also narrowed by launch audit. API
and live `Popen` already use `CREATE_NO_WINDOW`; Windows PowerShell process
inspection and `tasklist` inspection were still launched without a background
creation flag. Those helpers run during `/start` and can create visible console
windows even when the supervised children do not. The fix applies
`CREATE_NO_WINDOW` to those inspection helpers and to the exact-PID `taskkill`
helper. MT5 terminal ownership and its GUI launch behavior remain unchanged;
only the background `tasklist` probe receives the visibility-only flag.

## Confirmed fifth manual acceptance failure — intentional restart stop was misclassified

The fifth manual run establishes that the ordinary lifecycle is healthy:
`/stop`, `/start`, `/status`, API health, stale-registry reconciliation,
resolved `python.exe` children, and the no-console successful-start check all
passed. The failure is isolated to `/restart` from a verified RUNNING state.

The exact defect was not a process crash and not a restart-count increment. The
API record already had a historical `restart_count` of `4`. During the
intentional restart stop, `stop_component()` durably persisted
`desired_state=STOPPED` before termination and returned a truthful `STOPPED`
record. The control loop nevertheless used the old notification predicate:

`record.restart_count > 0 or record.state == "ERROR"`

That predicate treated the cumulative historical count as proof of a new crash,
so it emitted `PROCESS_CRASHED` while reporting `State: STOPPED`. The persisted
count was not incremented by the intentional stop.

The concurrency audit found no missing stop intent in the current ordering.
`/start`, `/stop`, and `/restart` share the control operation lock. The
component stop path writes the STOPPED intent before ownership verification,
termination, and stop polling. The same control task cannot run
`monitor_once()` in the middle of the synchronous stop phase; an independent
observer is also gated by the persisted desired state and cannot auto-recover
that component. The observable failure was the notification decision after the
STOPPED transition, not a monitor race that changed the registry back to
RUNNING or incremented the count.

The fix makes crash notification transition-based: `CRASHED`/`ERROR` states
notify, and a recovered `RUNNING` record notifies only when it carries the
supervised-exit diagnostic from that crash transition. A deliberate `STOPPED`
record with any historical restart count does not notify. `/restart` now also
creates a local operation ID and records restart start, per-component persisted
stop intent, termination start/result, verified STOPPED state, restart abort
reason, monitor observations during the transition, startup transition, and
final state in
`logs/supervisor_control_diagnostics.jsonl`. Telegram remains concise and
secret-free.

## Confirmed final manual happy-path acceptance — PASSED

The latest controlled Windows acceptance passed the complete healthy lifecycle:

- Task Scheduler started fresh Telegram Control successfully.
- `/start` passed: MT5/account/market, API, Live Engine, and Forward Shadow
  were connected/ready; execution remained disabled; no console flashing was
  observed.
- `/status` reported Supervisor, Live, API, MT5, database, and Telegram as
  connected/running.
- `/stop` passed: API and Live stopped, Control and MT5 remained online, no
  broker position changed, no revival occurred, no `PROCESS_CRASHED` was sent,
  historical restart counts did not increase, and no console flashed.
- A subsequent `/start` passed.
- `/restart` from verified RUNNING passed: intentional stop completed, no
  generic failure or `PROCESS_CRASHED` occurred, one new API topology and one
  new Live topology started, MT5/Forward Shadow stayed connected, and
  execution remained disabled.
- Post-restart `/status` again passed with all required components healthy.
- Windows showed one venv `python.exe` launcher plus one Python311 interpreter
  for each API/Live component, with no duplicate independent topology.
- Post-restart registry state was API `RUNNING`, `desired_state=RUNNING`,
  `restart_count=4`; Live `RUNNING`, `desired_state=RUNNING`,
  `restart_count=2`. The intentional restart did not increment either count.
- Forward Session remained
  `forward_833cc72f-4494-488b-8c68-f76555833730` and real-money execution
  remained `DISABLED`.

This is a manual happy-path pass. It does not by itself prove the controlled
failure-injection requirements listed below.

## 1. Original `/restart` root cause

The supervisor persisted and acted on a single process identity while Windows
may represent a virtual-environment launch as a launcher/interpreter
parent-child topology. Stopping only the tracked parent could leave the
interpreter alive. The prior control path also ignored the stop result and
continued directly into startup, allowing duplicate or partially stopped
topologies to be reported inconsistently.

The fifth manual failure was a separate reporting defect layered on top of this
ownership hardening: the old control-loop notification condition confused
historical `restart_count` with a newly observed crash. It is now fixed without
changing process ownership, PID reuse rejection, or desired-state semantics.

## 2. Previous pytest hang investigation

The Phase 2.5.5B regression file itself contains only mocks/fakes and completed
in 1.26s after the final fix. The surviving test inventory identified four
broader-suite tests that launch real Python subprocesses:

- `tests/test_phase17_control.py::test_process_supervisor_prevents_duplicate_and_stops`
- `tests/test_phase17_control.py::test_stale_supervisor_lock_is_recoverable`
- `tests/test_phase17_control.py::test_crash_recovery_is_bounded`
- `tests/test_phase201_history_health.py::test_second_supervisor_adopts_existing_live_command`

The first three use `python -c` subprocesses, including 30-second sleeps; the
first two do not wrap cleanup in `finally`, and the fourth uses a real process
with cleanup in `finally`. On Windows, launcher/interpreter descendants and
supervisor stop polling are the only surviving test paths capable of leaving a
real child or cleanup wait behind. The final validation excluded all four
tests. One intermediate combined pytest command was stopped immediately after
its selector was found not to apply to a second file; it did not start the
XAUUSD runtime or MT5, and a read-only process check afterward found no Python
test child remaining.

No pytest stack trace or process dump from the interrupted three-hour run was
preserved, so the exact historical blocking frame cannot be proven. The safe
mock-only broad suite completed in 32.03s with those four tests excluded,
which bounds the known hang surface without repeating the unbounded run.

## 3. Interrupted changes preserved

The existing uncommitted Phase 2.5.3–2.5.5A work was preserved. The surviving
Phase 2.5.5B changes included:

- `ProcessIdentity`, creation-time identity, parent PID, process-tree, and
  per-process identity persistence in `services/supervisor.py`;
- Windows process snapshots and exact supervised command-tail matching;
- launcher/interpreter topology collapse and adoption;
- PID-reuse rejection, ambiguous-tree degradation, stale-record handling, and
  fail-closed termination;
- stop-timeout degradation and restart abort behavior;
- supervisor/API/Telegram health derived from verified process state; and
- operation-scoped startup rollback tracking in `services/control.py`.

No unrelated strategy, Pair Zone, Forward Shadow, MT5, persistence, or
frontend behavior was rewritten.

## 4. Additional fixes made

- A Windows child whose command, creation identity, or process snapshot cannot
  be verified is now reported as `DEGRADED`, not `RUNNING`.
- In-memory `status()` and `monitor_once()` now preserve that same degraded
  truth instead of masking an unverified child as healthy.
- A process spawned by the current startup attempt remains marked for rollback
  even when its first state is `DEGRADED`; pre-existing processes are not added
  to the rollback set.
- Added a mock-only regression proving unverified spawn degradation,
  operation-scoped rollback eligibility, and truthful subsequent status.
- Added persisted desired-state gating so intentional stop exits cannot be
  classified as crashes or automatically restarted.
- Added Windows-only `CREATE_NO_WINDOW` creation flags while preserving normal
  Python interpreters, `shell=False`, redirected diagnostics, and identity
  tracking; MT5 terminal launch ownership remains unchanged.
- Replaced discarded supervised-child output with per-component local
  diagnostic logs and safe exit-code summaries.
- Added fail-closed Windows console-interpreter resolution, including tests for
  `pythonw.exe` sibling selection, missing-interpreter refusal, and unchanged
  non-Windows behavior.
- Added startup tests proving an API launch failure prevents live startup and
  rollback does not stop a pre-existing API component.
- Added restart-transition diagnostics with operation IDs and tests proving
  stop intent is durable before termination, historical restart counts do not
  produce `PROCESS_CRASHED`, crash transitions still do, and incomplete stop
  aborts restart without entering startup.

## 5. Process ownership model

Ownership is verified using the strongest available evidence:

1. exact supervised script path and argument tail;
2. Windows process identity and creation time;
3. recorded parent-child relationships;
4. the complete matching descendant topology; and
5. persisted PID/creation-time pairs for an orphaned interpreter child.

The supervisor collapses one launcher/interpreter chain into one component.
Multiple independent matching roots degrade without spawning or adopting one
arbitrarily. A PID alone is never sufficient for persisted-process ownership.

Termination is limited to verified owned PIDs, deepest child first. There is no
basename-only adoption and no broad `taskkill python.exe` or all-Python
termination path.

The production subprocess audit found two actual child-launch paths: the
supervisor’s API/live `Popen` and the separately owned MT5 terminal `Popen`.
The supervisor path is `shell=False`, uses the resolved console Python, and
receives `CREATE_NO_WINDOW` on Windows. The MT5 terminal launch remains
ownership- and behavior-unchanged; its background `tasklist` probe now receives
the visibility-only flag. Other production `subprocess.run` calls are
read-only Git metadata or Windows process-identity inspection; they do not
launch the API/live/MT5 runtime.

## 6. Restart fail-closed behavior

`/restart` stops the supervised `live` and `api` components first, checks the
resulting registry/status, and starts nothing unless both components are
conclusively `STOPPED`. An unverified identity, ambiguous topology, timeout,
or stale live record therefore aborts restart and leaves the operator a
truthful degraded result. Intentional stop/restart phases set desired state to
`STOPPED` before termination, suppressing watchdog recovery and
`PROCESS_CRASHED` notifications.

## 7. Rollback behavior

Startup records which components were actually spawned by the current
operation. If startup or bounded verification fails, rollback targets only
those components. It does not stop a pre-existing supervised component merely
because the current `/start` observed it.

## 8. Tests executed

- Phase 2.5.5B regression file: **43 passed**.
- Phase 2.5.5B plus remote-security regressions: **47 passed, 2 warnings in
  2.46s**.
- Phase 2.5.1 MT5 bootstrap regressions: **9 passed**.
- Phase 1.7 control regressions using fakes/mocks: **14 passed, 3 real-process
  tests deselected in 2.89s**.
- Phase 2.0.1 health regressions using fakes/mocks: **3 passed, 1 real-process
  test deselected, 2 warnings in 3.89s**.
- Related security, performance, Pair Zone, and Forward Shadow regressions:
  **17 passed, 2 warnings in 6.27s**.
- Bounded broad suite: **195 passed, 4 real-process tests deselected, 2
  warnings in 27.37s**.
- The new console/lifecycle tests cover Windows and non-Windows creation flags,
  `pythonw.exe` interpreter resolution, diagnostic capture, API-start fail
  closed behavior, rollback ownership, unexpected crash recovery, intentional
  stop suppression, watchdog polling, restart ordering, stale-registry
  reconciliation, exact Windows `SystemError` reproduction, pre-Popen
  traceback diagnostics, post-spawn verification rollback evidence, transient
  launcher/interpreter stabilization, inspection-helper visibility flags,
  secret redaction, no crash notification on intentional termination, durable
  restart stop intent, restart operation IDs, and restart abort diagnostics.

No test in this validation started the XAUUSD runtime or MT5 integration.

## 9. Validation results and durations

All selected tests passed. The only warnings were existing Starlette/httpx
dependency deprecations. The previously observed multi-hour pytest behavior
was not repeated; every command was bounded and progress-visible.

## 10. Static validation

- Ruff on changed production and Phase 2.5.5B test files: **passed**.
- Python `compileall` on relevant production/test files: **passed**.
- `git diff --check`: **passed**; only normal CRLF conversion warnings were
  reported by Git.
- Production scan for `mt5.order_send()` outside tests: **no matches**.
- Accepted Pair Zone V1 config SHA-256 remains
  `FD2D73B9AA0D21004653E455263107CAF727AC552FD204486F363CB7C25B7DED` and
  is unchanged in the working diff.

## 11. Safety and non-regression status

- Real-money execution remains disabled; no broker write was performed.
- Pair Zone V1 thresholds/configuration remain unchanged.
- Forward Shadow session/history was not reset, rewritten, or backfilled.
- Historical and Forward evidence remain separate.
- Telegram remains the control plane; this documentation update sent no
  Telegram lifecycle command. The separately supplied manual evidence records
  the operator-controlled lifecycle acceptance.
- No credentials or secrets were exposed.
- The separate Nexus repository was not modified by this work.
- No XAUUSD runtime, MT5, Telegram lifecycle, commit, push, tag, or deployment
  operation was performed. One previously documented broad-suite real-process
  test was interrupted after its selector was found unsafe; this was not an
  XAUUSD runtime process. The user-started control topology was only observed
  and was not started, stopped, or terminated by this work.
- A read-only audit found an existing supervised `control → server/live`
  Python topology and registry entries; it was left untouched by instruction.

## 12. Original manual acceptance procedure — historical reference

The latest happy-path acceptance and Phase 2.5.6 harness close the requirements
in this procedure. Do not repeat those operations while the current runtime is
healthy. The numbered list is retained only to show traceability to the
original acceptance specification.

For reference, the original controlled procedure was:

1. Record the pre-check state: `REAL-MONEY EXECUTION: DISABLED`, broker writes
   `0`, active Forward session ID, accepted strategy hash, and the exact API,
   live, launcher, and interpreter PIDs with creation times. Preserve the
   existing registry; do not delete or reset it.
2. Start only the control process and issue one `/start`. Before any child is
   expected, confirm the old dead live record is reconciled to `STOPPED` with
   cleared PID/tree/identity fields and that no generic `Operation failed`
   response is returned.
3. If startup fails before `Popen`, inspect
   `logs/supervisor_control_diagnostics.jsonl` and verify timestamp, stage,
   exception, traceback, and redaction without sending the file to Telegram.
   If children did spawn and were rolled back, verify the same file contains a
   `startup_verification` event with every check plus one `startup_rollback`
   event per spawned component, including the rollback reason and final state.
4. Use the normal Telegram control plane to perform one supervised restart and
   observe that stop reaches `STOPPED` for both components before startup
   begins, with no `PROCESS_CRASHED` and no increase to the pre-stop historical
   restart counts for the intentional stop phase. Inspect the local diagnostics
   file and confirm one restart operation ID links the stop-intent,
   termination, verification, startup-transition, and final-state events.
5. Confirm the post-restart registry contains one verified topology per
   component, including creation-time identities and any launcher/interpreter
   child, with no unrelated Python process changed.
6. Exercise an operator-visible incomplete-stop scenario in a safe maintenance
   setup and confirm `/restart` reports `RESTART ABORTED` and does not start a
   replacement while any prior topology remains unverified or alive.
7. Confirm `/status`, `/health`, API health, and the registry agree on
   `CONNECTED`, `DEGRADED`, or `STOPPED` state throughout the sequence.
8. Confirm MT5 remains open/unmodified, broker writes remain `0`, the Forward
   session/history and Pair Zone V1 hash remain unchanged, and no secrets enter
   Telegram or API responses.
9. Confirm Task Scheduler/control, `/start`, `/stop`, and `/restart` produce no
   visible console windows. Verify by exact command line that server/live use
   the environment’s normal `python.exe`, not `pythonw.exe`, and that only the
   supervisor-created Python children receive `CREATE_NO_WINDOW`; MT5 is
   excluded from this check.
10. If API or live exits, inspect the corresponding local
   `logs/supervisor_<component>.log` before repeating startup. Record the
   exception and exit code, confirm the API failure is surfaced as
   `START INCOMPLETE` with no live child started when API startup fails, and
   confirm the supervisor does not exceed the configured bounded recovery
   window. If no API/live exception exists, treat the case as verification
   rollback and use the persisted `startup_verification` checks to identify the
   failed condition.
11. Repeat the stop/restart race check: intentional stop must reach `STOPPED`
   without `PROCESS_CRASHED` or restart-count increment, and a failed startup
   rollback must leave pre-existing verified components untouched.

This report records the supplied manual happy-path runtime acceptance as
passed. No new lifecycle operation was performed while documenting it.

## Phase 2.5.6 — controlled failure-path acceptance harness — PASSED

The disposable Windows harness was executed without touching the healthy
runtime. Evidence was written only under:

`C:\Users\Windows 11\AppData\Local\Temp\phase256-final-evidence-20260923`

The harness used a unique temporary registry and lock per scenario, temporary
worker scripts with unique command markers, temporary SQLite databases, no
production port 8000, no Telegram client, no MT5, no Forward Shadow state, and
no broker execution. All worker launches used the existing Windows
background-creation flags. Cleanup verified exact marker/command ownership and
reported no leftover disposable process for any scenario.

Results:

- API incomplete stop: **PASSED**. Operation ID
  `26ddf935-7e79-4655-9d2e-50c8b0d5a953`; API PID `17888` remained truthfully
  `STOPPING` with `desired_state=STOPPED`, Live stopped, restart aborted, no
  replacement, no crash notification, and restart count unchanged.
- Live incomplete stop: **PASSED**. Operation ID
  `0120a545-f574-4bd7-8413-3f21f9814197`; Live PID `27496` remained truthfully
  `STOPPING`, API stopped, restart aborted, no replacement, no crash
  notification, and restart count unchanged.
- Ambiguous topology: **PASSED**. Two independent matching roots remained two
  roots before and after; the topology contained four launcher/interpreter
  nodes, no third replacement was created, and neither root was adopted or
  terminated.
- Identity disagreement/PID reuse: **PASSED**. Missing-creation-time PID
  `27868` and reused-creation-time PID `5808` were both rejected as
  `DEGRADED` and remained alive. PID alone was not accepted.
- Pre-Popen startup failure: **PASSED**. No API/Live child or registry child
  was created; response was `START INCOMPLETE`; diagnostics recorded
  `mt5_readiness`; the injected secret was absent from evidence diagnostics.
- Post-spawn verification failure: **PASSED**. Injected
  `live_runtime=FAILED_INJECTED` produced `startup_verification` and targeted
  rollback. Pre-existing API PID `8548` remained `RUNNING` and unchanged;
  only the newly spawned Live component was rolled back to `STOPPED`.

Every scenario produced initial/final registry snapshots, topology identities
and creation times, desired-state transitions, operation/rollback diagnostics,
unrelated-process before/after identity, and cleanup evidence. The aggregate
result in `summary.json` was `all_passed=true` with six scenarios passed and
six cleanups passed.

The original Phase 2.5.5B failure-path requirements are therefore demonstrated
in a controlled disposable environment. A real failure injection against the
healthy production-like runtime remains intentionally unperformed and is not
required for this harness acceptance.

## 13. Concise final assessment

1. **Phase 2.5.5B final status:** automated acceptance complete; manual
   happy-path Windows runtime acceptance passed; controlled failure-path
   harness acceptance passed.
2. **Proven:** clean start/stop/restart behavior, intentional-stop semantics,
   no false `PROCESS_CRASHED`, no restart-count increase, stale registry
   reconciliation, verified launcher/interpreter topology, no duplicate API or
   Live topology, no console flashing, status/health consistency, MT5 and
   Forward Shadow continuity, and execution disabled.
3. **Unproven:** real failure injection against the healthy production-like
   runtime. This is intentionally not a test target; all specified failure
   paths are proven by the isolated Windows harness above.
4. **Can Phase 2.5.5B be closed?** **Yes.** The automated, happy-path runtime,
   and controlled disposable failure-path acceptance requirements are complete.
5. **Recommended next development phase:** Phase 2.5.7 — release-readiness
   closeout and evidence retention review, with no production behavior change
   unless a new issue is reproduced.
6. **Current git working-tree status:** intentionally dirty. Existing changes
   and untracked phase artifacts remain preserved; no commit, push, reset,
   clean, stash, or discard was performed. The current healthy runtime and
   live registry were not modified.
7. **Files changed by Phase 2.5.5B:**
   `services/control.py`, `services/supervisor.py`, `mt5/bootstrap.py`,
   `tests/test_phase255b_supervisor_restart.py`, and
   `PHASE_2_5_5B_SUPERVISOR_RESTART_REPORT_2026-09-23.md`.
   Phase 2.5.6 added the disposable harness
   `tools/phase256_failure_harness.py`; it does not change production
   lifecycle behavior.

### Failure-path requirements status

- API incomplete stop: **PASSED in isolated harness**.
- Live incomplete stop: **PASSED in isolated harness**.
- Ambiguous topology, identity disagreement, PID reuse, and unrelated-process
  safety: **PASSED in isolated harness**.
- Pre-Popen and post-spawn startup failure/rollback diagnostics: **PASSED in
  isolated harness**.

No failure-path requirement remains open. The healthy runtime remains out of
scope and must not be injected or disturbed.
