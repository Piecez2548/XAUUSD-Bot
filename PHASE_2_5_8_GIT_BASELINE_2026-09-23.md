# Phase 2.5.8 — Git Baseline & Commit Preparation

Date: 2026-09-23  
Repository: `D:\Project_001\Nexus-Project\XAUUSD Bot`  
Branch: `main`  
HEAD: `4ff90121c83558a59e7262e5b8569c7c3029a54b`  
HEAD subject: `feat: add MT5 auto-launch for Telegram control`  
Remote relation: `main...origin/main` at the inspected HEAD

## Executive result

This phase produced a read-only classification and proposed staging plan. No
index changes were made. No commit, push, tag, reset, clean, checkout, restore,
stash, rebase, runtime lifecycle operation, registry mutation, MT5 operation,
or broker write was performed.

**PHASE 2.5.8 BLOCKER: RESOLVED**  
**SELECTIVE STAGING: READY**

The initial fresh Phase 2.5.6 rerun failed in the post-spawn rollback path with
Windows `SystemError`/`WinError 87`. The failure was reproduced and traced to
the Windows `os.kill(pid, 0)` liveness probe executed immediately before the
verified `taskkill.exe` operation. CPython could surface the Windows error with
a pending native exception; the subsequent `_suppress_os_error()` context
helper then surfaced `SystemError` before `taskkill.exe` was invoked.

The smallest fix was applied in `services/supervisor.py`: Windows cleanup now
uses the already verified PID/command/creation-time topology directly and does
not perform the unreliable pre-`taskkill` `os.kill(pid, 0)` probe. A missing
target is handled as an expected taskkill non-zero exit and verified by the
post-termination process snapshot. Ownership verification and fail-closed
termination rules remain unchanged.

An initial test wrapper accidentally allowed pytest to start the repository-wide
collection instead of the requested file list. It was interrupted after the
13 safe tests in `test_phase17_control.py`, before the first quarantined test;
the four quarantined tests were not executed. The corrected bounded commands
were then run individually and passed.

The original failed rerun is retained in this report as historical evidence:

```text
initial fresh rerun: FAILED
root cause: Windows os.kill(pid, 0) probe surfaced WinError 87 before taskkill
resolution: remove the Windows pre-taskkill os.kill probe; retain verified ownership checks
post-fix repeatability: targeted 10/10 PASS; complete harness 2/2 PASS
```

Files changed for the blocker fix:

- `services/supervisor.py` — smallest Windows cleanup change.
- `tests/test_phase255b_supervisor_restart.py` — deterministic regression for
  the invalid Windows liveness-probe path.
- `PHASE_2_5_8_GIT_BASELINE_2026-09-23.md` — this updated report.

The Phase 2.5.6 harness itself was not changed.

## 1. Repository snapshot

Read-only Git snapshot:

- Working tree: dirty.
- Staged index: empty; `git diff --cached --stat` and
  `git diff --cached --name-status` returned no entries.
- Tracked working-tree diff: 17 files, 1,673 insertions and 379 deletions.
- `git diff --check`: exit 0; Git emitted only normal LF/CRLF conversion
  warnings for existing modified files.
- No deleted or renamed paths were present in the inspected status.
- No destructive Git command was used.

The complete current status is classified in the table below. Ignored runtime
files are listed separately because they are not emitted by ordinary
`git status --short`.

## 2. Complete file classification

`Commit?` is a proposal only. Nothing in this table was staged.

| File | Git state | Category | Phase | Commit? | Reason |
|---|---|---|---|---|---|
| `mt5/bootstrap.py` | modified | A | 2.5.5B | conditional | Background inspection visibility flag; no strategy or broker behavior. |
| `services/control.py` | modified | A | 2.5.5B | conditional | Lifecycle intent, reconciliation, startup verification, rollback, and diagnostics. |
| `services/supervisor.py` | modified | A | 2.5.5B | conditional | Ownership-safe process identity, topology, Windows launch, termination, and registry logic. |
| `tests/test_phase255b_supervisor_restart.py` | untracked | B | 2.5.5B | conditional | Deterministic supervisor/control regression coverage. |
| `tools/phase256_failure_harness.py` | untracked | C | 2.5.6 controlled acceptance | proposed | Disposable failure-path harness; post-fix repeatability is green. |
| `PHASE_2_5_5B_SUPERVISOR_RESTART_REPORT_2026-09-23.md` | untracked | D | 2.5.5B | conditional | Acceptance and closure report; top-level status wording was corrected for consistency. |
| `PHASE_2_5_7_RELEASE_READINESS_REPORT_2026-09-23.md` | untracked | D | 2.5.7 | conditional | Release-readiness and evidence-retention report. |
| `docs/evidence/PHASE_2_5_6_ACCEPTANCE_EVIDENCE_2026-09-23.md` | untracked | D | 2.5.6 | conditional | Sanitized retained evidence manifest; raw evidence is excluded. |
| `PHASE_2_5_8_GIT_BASELINE_2026-09-23.md` | untracked | D | 2.5.8 | proposed | This baseline manifest and staging plan. |
| `PHASE_2_5_3_MANUAL_ACCEPTANCE_BUGFIX_REPORT_2026-09-22.md` | untracked | E | 2.5.3 | no | Earlier manual bugfix report; outside the infrastructure baseline. |
| `PHASE_2_5_3_REMOTE_DASHBOARD_REPORT_2026-09-22.md` | untracked | E | 2.5.3 | no | Remote dashboard workstream. |
| `PHASE_2_5_4_SECURE_REMOTE_DASHBOARD_REPORT_2026-09-22.md` | untracked | E | 2.5.4 | no | Secure remote dashboard workstream. |
| `PHASE_2_5_5A_ROUTE_SWITCHING_PERFORMANCE_REPORT_2026-09-23.md` | untracked | E | 2.5.5A | no | Route/performance workstream. |
| `PHASE_2_5_5_FRONTEND_PERFORMANCE_HARDENING_REPORT_2026-09-23.md` | untracked | E | 2.5.5A | no | Frontend performance workstream. |
| `.env.example` | modified | E | earlier configuration work | no | Template/configuration change; not supervisor infrastructure. |
| `api/app.py` | modified | E | remote/dashboard | no | API/dashboard work outside lifecycle hardening. |
| `config/settings.py` | modified | E | remote/dashboard/config | no | Configuration work outside the Phase 2.5.5B production boundary. |
| `frontend/src/App.tsx` | modified | E | frontend | no | Frontend application work. |
| `frontend/src/components/AppShell.tsx` | modified | E | frontend | no | Frontend shell work. |
| `frontend/src/components/DataState.tsx` | modified | E | frontend | no | Frontend state presentation work. |
| `frontend/src/hooks/useApi.ts` | modified | E | frontend | no | Frontend API hook work. |
| `frontend/src/lib/api.ts` | modified | E | frontend | no | Frontend API client work. |
| `frontend/src/pages/ForwardValidationPage.tsx` | modified | E | frontend/Forward Shadow UI | no | UI-only work; not a Forward Shadow history change. |
| `frontend/src/pages/ModulePages.tsx` | modified | E | frontend | no | Frontend module-page work. |
| `frontend/src/pages/OverviewPage.tsx` | modified | E | frontend | no | Frontend overview work. |
| `frontend/src/pages/ResearchPage.tsx` | modified | E | frontend/research UI | no | Research UI work; no supervisor scope. |
| `frontend/src/styles.css` | modified | E | frontend | no | Frontend styling work. |
| `frontend/src/types.ts` | modified | E | frontend | no | Frontend type work. |
| `frontend/.env.example` | untracked | E | frontend config | no | Frontend template; no credential value intended. |
| `frontend/README.md` | untracked | E | frontend | no | Frontend documentation. |
| `frontend/vercel.json` | untracked | E | frontend deployment | no | Frontend deployment configuration. |
| `frontend/src/components/AppErrorBoundary.tsx` | untracked | E | frontend | no | Frontend component. |
| `frontend/src/components/AppErrorBoundary.test.tsx` | untracked | E | frontend | no | Frontend test. |
| `frontend/src/components/AppShell.test.tsx` | untracked | E | frontend | no | Frontend test. |
| `frontend/src/components/AppShellPerformance.test.tsx` | untracked | E | frontend | no | Frontend performance test. |
| `frontend/src/components/BangkokClock.tsx` | untracked | E | frontend | no | Frontend component. |
| `frontend/src/components/BangkokClock.test.tsx` | untracked | E | frontend | no | Frontend test. |
| `frontend/src/components/SystemHealthContext.test.tsx` | untracked | E | frontend | no | Frontend test. |
| `frontend/src/components/SystemHealthProvider.tsx` | untracked | E | frontend | no | Frontend component. |
| `frontend/src/components/systemHealthContext.ts` | untracked | E | frontend | no | Frontend context. |
| `frontend/src/components/useSystemHealth.ts` | untracked | E | frontend | no | Frontend hook. |
| `frontend/src/lib/runtime.ts` | untracked | E | frontend | no | Frontend runtime helper. |
| `frontend/src/lib/runtime.test.ts` | untracked | E | frontend | no | Frontend test. |
| `frontend/src/pages/ResearchPage.test.tsx` | untracked | E | frontend | no | Frontend test. |
| `tests/test_phase254_remote_security.py` | untracked | E | 2.5.4 | no | Remote security regression outside 2.5.5B baseline. |
| `tests/test_phase255_performance.py` | untracked | E | 2.5.5A | no | Performance regression outside 2.5.5B baseline. |
| `phase255b_diff.txt` | untracked | F | review artifact | no | Pre-existing diff/review artifact; not source. |
| `start_hidden.vbs` | untracked | F | local launcher artifact | no | Pre-existing launcher artifact; requires separate review. |
| `tools/__pycache__/phase256_failure_harness.cpython-311.pyc` | ignored | F | generated | no | Python bytecode; covered by `__pycache__/` and `*.py[cod]`. |

The `tools/` status directory contains the C source above plus ignored
bytecode. The `docs/evidence/` status directory contains the single D manifest
above.

## 3. Mixed-file findings

No Category A, B, C, or D file was found to contain an unrelated Pair Zone,
Forward Shadow strategy, or frontend workstream hunk. No partial staging is
required for the proposed Phase 2.5.5B production files.

The large `services/supervisor.py` diff was inspected for strategy, Forward
Shadow, and broker-ordering terms; its changed scope is lifecycle ownership,
diagnostics, Windows process handling, and registry state. `mt5/bootstrap.py`
changes only the background process-inspection creation flags. The frontend
and API changes are in separate E files and must remain excluded.

## 4. Secret audit

The required secret-pattern search was performed without printing matching
values. Matches in candidate code are configuration names, redaction logic, or
synthetic test fixtures, not discovered production credentials.

| File/category | Tracked state | Commit-safe? | Required remediation |
|---|---|---|---|
| `services/control.py` environment/Telegram/MT5 names and redaction paths | modified | YES | None; values remain external configuration. |
| `services/supervisor.py` diagnostic redaction names | modified | YES | None; no literal credential. |
| `mt5/bootstrap.py` credential fields passed through settings | modified | YES | None; no literal credential. |
| `tests/test_phase255b_supervisor_restart.py` synthetic token/secret fixtures | untracked | YES | Keep explicitly synthetic; do not replace with real values. |
| `tools/phase256_failure_harness.py` synthetic injected secret fixture | untracked | YES | Keep as test-only redaction fixture; raw temp output is not commit content. |
| Phase 2.5.5B/2.5.7/2.5.6/2.5.8 Markdown | untracked | YES | Retain labels and evidence summaries only; no values. |
| `.env` | ignored/untracked local file | NO | Never stage or commit; keep covered by `.env`/`.env.*` rules. |
| `.env.example`, `frontend/.env.example` | modified/untracked templates | YES as templates only | Verify placeholders remain non-secret before any separate frontend commit. |
| Runtime logs, registry, databases, and temporary harness evidence | ignored/outside repository | NO | Do not copy or stage. |

No real secret was identified in candidate commit content. The failed harness
run emitted a synthetic fixture token to the local console traceback; that
output is not in the repository and must not be copied into the release
manifest.

## 5. Runtime artifact audit

The following local artifacts are excluded from candidate commits and were not
deleted:

- `data/process_registry.json`
- `data/trading_observatory.db`, `data/trading_observatory.db-shm`,
  `data/trading_observatory.db-wal`, and `data/phase171-migration.db`
- `data/telegram_update_offset.json`
- `logs/phase1.jsonl`, `logs/supervisor_api.log`,
  `logs/supervisor_control_diagnostics.jsonl`, and `logs/supervisor_live.log`
- `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`, `__pycache__/`, and `*.pyc`
- temporary Phase 2.5.6/2.5.8 evidence directories outside the repository
- local PID/state files and backup artifacts

The current `.gitignore` covers `.env`, `.env.*` with `.env.example` allowed,
`data/process_registry.json`, database files, logs, Python caches, PID files,
Telegram offset state, and frontend generated output. No additional ignore
rule is required for the observed runtime artifacts. The untracked review
file `phase255b_diff.txt` and launcher `start_hidden.vbs` are intentionally
not treated as safe source merely because they are not ignored.

## 6. Acceptance documentation audit

The Phase 2.5.5B report had a stale top-level phrase saying the failure path
remained open. That documentation-only inconsistency was corrected. The
acceptance distinction is now:

- Automated acceptance: **COMPLETE**.
- Windows manual happy-path: **PASSED**.
- Isolated controlled failure-path harness: **PASSED in retained evidence**.
- Manual failure injection against the healthy runtime: **NOT PERFORMED**.

No document is intended to imply that the last item passed. The fresh harness
rerun described in Section 10 is a validation repeatability finding, not a new
manual runtime acceptance claim.

## 7. Safety baseline verification

Read-only checks produced the following:

- Pair Zone V1 SHA-256: `FD2D73B9AA0D21004653E455263107CAF727AC552FD204486F363CB7C25B7DED`.
- Pair Zone configuration file has no working-tree diff.
- `services/forward_shadow.py` has no working-tree diff.
- No production `mt5.order_send(` match was found outside tests.
- Execution-disabled markers remain in the production surface; no broker-write
  path was enabled.
- `mt5/bootstrap.py` changed only the visibility flag for the background
  process probe; MT5 terminal launch/ownership behavior was not changed by
  this baseline preparation.
- No Forward Shadow history/session rewrite was detected in the Phase 2.5
  candidate diff.

## 8. Historical unsafe tests

The following were not executed as bounded validation targets:

- `tests/test_phase17_control.py::test_process_supervisor_prevents_duplicate_and_stops`
- `tests/test_phase17_control.py::test_stale_supervisor_lock_is_recoverable`
- `tests/test_phase17_control.py::test_crash_recovery_is_bounded`
- `tests/test_phase201_history_health.py::test_second_supervisor_adopts_existing_live_command`

Classification:

| Test | Decision | Reason |
|---|---|---|
| `test_process_supervisor_prevents_duplicate_and_stops` | HARDEN LATER | Replace implicit real-process assumptions with unique ownership markers and guaranteed cleanup. |
| `test_stale_supervisor_lock_is_recoverable` | HARDEN LATER | Use a test-local lock/registry and strict cleanup/timeout. |
| `test_crash_recovery_is_bounded` | HARDEN LATER | Move crash simulation into a disposable subprocess fixture. |
| `test_second_supervisor_adopts_existing_live_command` | REPLACE | The isolated harness now covers ambiguous/adoption safety without touching live runtime. |

The Phase 2.5.6 harness supersedes the unsafe tests' acceptance purpose, but
not all future unit-maintenance value. None should be removed in this phase.

## 9. Final bounded validation

Corrected direct invocations were used after the over-broad wrapper was
interrupted. The results below are bounded and do not start the real XAUUSD
runtime.

| Command | Result | Duration / warnings |
|---|---|---|
| `.venv\Scripts\python.exe -m pytest tests/test_phase255b_supervisor_restart.py tests/test_phase254_remote_security.py -q --tb=short` | PASS: 48 passed | 6.65s pytest time; 2 deprecation warnings |
| `.venv\Scripts\python.exe -m pytest tests/test_phase251_mt5_autolaunch.py -q --tb=short` | PASS: 9 passed | 2.15s pytest time |
| `.venv\Scripts\python.exe -m pytest tests/test_phase17_control.py -k "not process_supervisor_prevents_duplicate_and_stops and not stale_supervisor_lock_is_recoverable and not crash_recovery_is_bounded" -q --tb=short` | PASS: 14 passed, 3 deselected | 3.10s pytest time |
| `.venv\Scripts\python.exe -m pytest tests/test_phase201_history_health.py -k "not second_supervisor_adopts_existing_live_command" -q --tb=short` | PASS: 3 passed, 1 deselected | 4.16s pytest time; 2 deprecation warnings |
| `.venv\Scripts\python.exe -m pytest tests/test_pair_zone_strategy.py -q --tb=short` | PASS: 3 passed | 0.12s pytest time |
| `.venv\Scripts\python.exe -m pytest tests/test_phase25_forward_shadow.py -q --tb=short` | PASS: 7 passed | 2.83s pytest time; 2 deprecation warnings |
| `.venv\Scripts\python.exe -m pytest tests/test_connection_and_safety.py -q --tb=short` | PASS: 6 passed | 0.08s pytest time |
| `.venv\Scripts\python.exe -m ruff check services/control.py services/supervisor.py mt5/bootstrap.py tests/test_phase255b_supervisor_restart.py tools/phase256_failure_harness.py` | PASS | No findings |
| `.venv\Scripts\python.exe -m compileall -q services/control.py services/supervisor.py mt5/bootstrap.py tests/test_phase255b_supervisor_restart.py tools/phase256_failure_harness.py` | PASS | No findings |
| `git diff --check` | PASS | Normal CRLF conversion warnings only |

The first wrapper invocation is recorded as an execution anomaly, not as a
validation result: it accidentally collected the repository suite and was
interrupted after the first 13 safe `test_phase17_control.py` cases, before the
first quarantined test. No quarantined test was intentionally or successfully
run, and no repo-scoped test process remained afterward.

The original fresh Phase 2.5.6 harness invocation was bounded to a unique temp
directory outside the repository and **FAILED** in the final
`post-spawn-verification-failure` scenario. That historical failure was:

```text
SystemError: <class 'services.supervisor._suppress_os_error'> returned a result with an exception set
OSError: [WinError 87] The parameter is incorrect
```

It occurred before `taskkill.exe` was invoked, when the Windows
`os.kill(pid, 0)` liveness probe left a native `WinError 87` exception state
that surfaced as the `_suppress_os_error` `SystemError`. The process snapshot
after the run showed only the expected current runtime/frontend processes and
no harness worker leftover.

Post-fix acceptance:

- Targeted `post-spawn-verification-failure`: **10/10 PASS**.
- Targeted cleanup: **10/10 clean**, zero leftovers and zero cleanup errors.
- Complete Phase 2.5.6 harness run #1: **PASS**, `all_passed=true`, 6/6
  scenarios, 6/6 cleanups.
- Complete Phase 2.5.6 harness run #2 from a fresh temp directory: **PASS**,
  `all_passed=true`, 6/6 scenarios, 6/6 cleanups.

## 10. Proposed commit groups

The preferred grouping remains four commits. All groups are now ready for
selective review/staging, subject to explicit operator approval. No command
below was executed.

### COMMIT 1 — supervisor/control lifecycle hardening

Suggested message:

```text
fix(supervisor): harden Windows process lifecycle ownership
```

Exact proposed staging command:

```text
git add -- mt5/bootstrap.py services/control.py services/supervisor.py
```

### COMMIT 2 — deterministic regression coverage

Suggested message:

```text
test(supervisor): cover restart and ownership regressions
```

Exact proposed staging command:

```text
git add -- tests/test_phase255b_supervisor_restart.py
```

### COMMIT 3 — isolated controlled failure-path harness

Suggested message:

```text
test(acceptance): add isolated lifecycle failure harness
```

Exact proposed staging command:

```text
git add -- tools/phase256_failure_harness.py
```

### COMMIT 4 — acceptance and release documentation

Suggested message:

```text
docs(supervisor): record Phase 2.5 acceptance evidence
```

Exact proposed staging command:

```text
git add -- PHASE_2_5_5B_SUPERVISOR_RESTART_REPORT_2026-09-23.md PHASE_2_5_7_RELEASE_READINESS_REPORT_2026-09-23.md PHASE_2_5_8_GIT_BASELINE_2026-09-23.md docs/evidence/PHASE_2_5_6_ACCEPTANCE_EVIDENCE_2026-09-23.md
```

No `.gitignore` update is currently required.

## 11. Unrelated work preservation

The following must remain untouched and excluded from all four proposed
commits:

- `.env.example`, `api/app.py`, `config/settings.py`: existing config/API and
  remote-dashboard work.
- All listed `frontend/` source, tests, README, templates, and deployment
  files: frontend workstream.
- `tests/test_phase254_remote_security.py`: earlier remote-security coverage.
- `tests/test_phase255_performance.py`: earlier performance coverage.
- The five earlier Phase 2.5.3–2.5.5A reports: separate phase evidence.
- `phase255b_diff.txt`: review artifact, not source.
- `start_hidden.vbs`: launcher artifact requiring separate ownership review.
- `.env`, runtime registry, databases, logs, caches, PID/state files, and all
  temporary evidence directories: sensitive/generated runtime state.

None of these overlaps a candidate A/B/C/D file at the hunk level. Selective
path staging is sufficient; partial staging is not required.

## 12. Known limitations and remaining risks

Known limitations:

- The original failed harness rerun remains historical evidence; the post-fix
  targeted and complete harness reruns are green.
- The working tree is not a clean release branch.
- Historical real-process tests remain quarantine debt.
- Raw temporary evidence is intentionally not retained in Git.

Remaining risks:

- A future stage operation could accidentally include unrelated frontend work
  or ignored secrets if paths are not explicit.
- The four historical real-process tests remain intentionally quarantined.
- Reports contain historical acceptance context; reviewers must distinguish
  isolated controlled acceptance from manual failure injection.
- No release commit or tag currently anchors the Phase 2.5 baseline.

## 13. Recommended next action

The blocker is resolved. Review the exact four path lists above and obtain
explicit approval before executing any `git add` or commit operation. This
phase itself does not stage or commit anything.

## 14. Decision gate

1. **Is the repository safe to begin selective staging?** Yes. The exact path
   groups are isolated and all required post-fix acceptance is green; staging
   still requires explicit operator approval.
2. **Are any candidate files mixed with unrelated work?** No. No A/B/C/D file
   requires partial staging based on the inspected diffs.
3. **Are any secrets present in candidate commit content?** No real production
   secret was found. Synthetic test fixtures and configuration names are
   present; `.env` and runtime outputs are excluded.
4. **Are runtime artifacts adequately excluded?** Yes. `.gitignore` covers the
   observed registry, databases, logs, caches, PID/state, and local secrets;
   raw temporary evidence remains outside the repository.
5. **Do all bounded validations pass?** Yes. Corrected bounded tests, targeted
   10/10 cleanup, two complete green harness runs, Ruff, compileall, and diff
   check all pass.
6. **Is Phase 2.5 infrastructure ready for baseline commits?** Yes. It is
   ready for selective staging; no staging or commit was performed here.
7. **What exact commit sequence is recommended?** Commit 1 production
   hardening, Commit 2 regression tests, Commit 3 isolated harness, Commit 4
   documentation/evidence, using the exact `git add --` paths above.
8. **What must remain untouched?** Healthy runtime, Telegram Control, MT5,
   process registry, broker state, Pair Zone V1, Forward Shadow history/session,
   unrelated frontend/remote work, secrets, and generated artifacts.
9. **Is any production-code change still required before commit?** No further
   production change is required for this blocker. The narrowly scoped
   `services/supervisor.py` fix and deterministic regression are complete.
10. **After baseline commits, what development phase should begin next?** Phase
    2.6 strategy/research development under the locked infrastructure and
    execution-disabled baseline.
