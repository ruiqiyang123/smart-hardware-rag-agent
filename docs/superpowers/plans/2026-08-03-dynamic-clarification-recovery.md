# Dynamic Clarification Choice and Concurrent Recovery Implementation Plan

Spec: `docs/superpowers/specs/2026-08-03-dynamic-clarification-recovery-design.md`

## Goal

Replace plain-text clarification options with context-driven, server-owned choices that carry a stable `choice_id` and validated routing semantics. Selecting a current choice must deterministically continue the graph without asking DeepSeek to reinterpret the short label. At the same time, make Streamlit reruns recover the same idempotent command instead of duplicating chat turns or displaying a false terminal failure while the command is still running.

## Task 1: Define the dynamic clarification contract

Files:

- Modify `agent/orchestration/state.py`
- Add `agent/orchestration/clarification.py`
- Modify `agent/nodes/triage.py`
- Modify `prompts/triage_prompt.txt`
- Extend `tests/test_ticket_state.py`
- Extend `tests/test_agent_contracts.py`
- Extend `tests/test_prompt_contract.py`

Steps:

1. Add a strict `ClarificationCandidate` model containing label, intent, category, risk level, risk flags, missing fields, and `diagnose|escalate` suggested route.
2. Add the persisted `ClarificationChoice` form with a server-generated stable ID.
3. Generate `choice_<16 hex>` from canonical validated semantics and list position; reject duplicate IDs and duplicate normalized labels.
4. Require an ambiguous Triage result to contain 2–5 dynamic candidates, and require a non-ambiguous result to contain none.
5. Validate label length, control characters, secret leakage, category-required fields, risk consistency, and legal route.
6. Update the prompt so DeepSeek generates mutually exclusive, context-dependent candidates instead of selecting from a fixed global list.
7. Normalize and validate candidates before any choice is saved or shown.

Verification:

```bash
pytest -q tests/test_ticket_state.py tests/test_agent_contracts.py tests/test_prompt_contract.py
```

## Task 2: Add deterministic graph recovery for a selected choice

Files:

- Modify `agent/orchestration/state.py`
- Modify `agent/orchestration/graph.py`
- Modify `agent/orchestration/routes.py`
- Extend `tests/test_orchestration_routes.py`
- Extend `tests/test_human_in_loop.py`
- Extend `tests/test_failure_fallbacks.py`

Steps:

1. Store structured choices in graph state and add a one-shot selected-choice field.
2. Preserve the selected server-owned choice through `await_user` while clearing the old question and option list only when the choice is consumed.
3. In the Triage node, detect a validated selected choice and project it into a legal Triage result without invoking the model runner.
4. Reapply ingress and sticky-risk floors; a selected choice may preserve or raise risk but never lower it.
5. Route low-risk `diagnose` choices to diagnosis and approved financial-security `escalate` choices to human handling.
6. Clear the selected choice after projection so it cannot be consumed twice.
7. Convert deterministic low-risk fallback clarification into structured generic choices; keep it distinct from the asset-specific policy examples.

Verification:

```bash
pytest -q tests/test_orchestration_routes.py tests/test_human_in_loop.py tests/test_failure_fallbacks.py
```

## Task 3: Migrate ticket persistence and validate current choices

Files:

- Modify `database/ticket_db.py`
- Extend `tests/test_ticket_repository.py`
- Extend `tests/test_ticket_db_migrations.py`
- Extend `tests/test_workbench_queries.py`

Steps:

1. Raise `SCHEMA_VERSION` from 8 to 9.
2. Store structured choice objects in the existing `clarification_options_json` array.
3. Add a transactional v8-to-v9 migration that converts string options into stable legacy choice objects without deleting existing tickets.
4. Validate persisted choice IDs, labels, enum values, list sizes, duplicates, and safe display fields at the repository boundary.
5. Keep legacy choices identifiable; legacy selection must re-enter Triage with explicit previous-choice context because it has no trusted route metadata.
6. Add a repository lookup that validates ticket status, waiting reason, and current choice membership before returning server-owned semantics.

Verification:

```bash
pytest -q tests/test_ticket_repository.py tests/test_ticket_db_migrations.py tests/test_workbench_queries.py
```

## Task 4: Add choice commands and idempotent runtime recovery

Files:

- Modify `agent/orchestration/runtime.py`
- Modify `utils/ui_command_state.py`
- Extend `tests/test_runtime_persistence.py`
- Extend `tests/test_runtime_idempotency.py`
- Extend `tests/test_user_confirmed_closure.py`

Steps:

1. Add a choice-resume API that accepts only `ticket_id`, `choice_id`, and `request_id` from the UI.
2. Load label and routing metadata from the repository; never trust route, risk, or category values supplied by the client.
3. Reject unknown, stale, cross-ticket, already-consumed, or non-clarification choices before any graph side effect.
4. Include `choice_id` in the command payload fingerprint and reuse the same request/command ID across retries.
5. Show the server-owned label as the user turn while passing structured choice semantics to the graph.
6. Preserve the existing free-text resume path through Triage.
7. Expose a read-only command-status query for the UI poller without executing or renewing a command.

Verification:

```bash
pytest -q tests/test_runtime_persistence.py tests/test_runtime_idempotency.py tests/test_user_confirmed_closure.py
```

## Task 5: Make Streamlit submission two-phase and rerun-safe

Files:

- Modify `app.py`
- Extend `tests/test_app_v2_contract.py`
- Extend `tests/test_ui_command_state.py`

Steps:

1. Split submission into an enqueue render and an execute/recover render.
2. Append exactly one user turn and one processing placeholder, freeze the request payload, set the pending request ID, then rerun before model execution.
3. Disable chat input, example buttons, clarification buttons, solve/continue controls, and human-handoff controls while a customer command is pending.
4. Render dynamic option buttons with keys derived from `ticket_id + choice_id`; submit the choice ID rather than the label.
5. Treat `CommandInProgressError` and recoverable timeout as non-terminal: retain the processing placeholder and pending state.
6. Add a read-only fragment that polls at most every two seconds and performs a full rerun only for completed, failed, or lease-expired commands.
7. Replace the placeholder once when a command completes; show the generic failure once only for a terminal failed command.
8. Keep the single input box at the bottom after the complete conversation history.

Verification:

```bash
pytest -q tests/test_app_v2_contract.py tests/test_ui_command_state.py
```

## Task 6: Align documentation and evaluation coverage

Files:

- Modify `README.md`
- Modify `docs/DEMO_SCRIPT.md`
- Modify `eval/multi_agent_cases.json` if evaluation fixtures require structured choices
- Modify `tests/test_orchestration_eval.py`
- Modify `tests/test_demo_readiness.py`

Steps:

1. Explain that clarification choices are dynamically generated for the current question, not selected from five universal answers.
2. Explain the deterministic choice-resume boundary and why it prevents repeat-question loops.
3. Document that free-text replies still use AI Triage and that automatic escalation remains limited to the approved financial-security whitelist.
4. Document idempotent command recovery without exposing internal implementation details in customer UI copy.
5. Add evaluation cases for power, Bluetooth, firmware, recovery, and transaction ambiguity, including a never-before-hardcoded label.

Verification:

```bash
pytest -q tests/test_orchestration_eval.py tests/test_demo_readiness.py
```

## Task 7: Full regression and local acceptance

Files:

- Modify only files required by failures attributable to this approved design

Steps:

1. Run the complete test suite in the project Python 3.11 test environment.
2. Run `compileall` and `git diff --check`.
3. Bump the Streamlit orchestrator contract version and restart localhost:8503.
4. Verify an ambiguous power question produces power-specific choices.
5. Select a choice and confirm the selected label appears once and the old clarification is not repeated.
6. Verify a new free-text question still invokes Triage and remains in AI handling when low risk.
7. Verify repeated reruns while DeepSeek is running do not create duplicate user turns or false safe-failure notices.
8. Verify an explicit financial-security choice still routes to the human workbench.

Success criteria:

- Dynamic choices require no Python branch per new label.
- A current choice is consumed once and does not loop back to the same clarification.
- A stale or forged choice cannot affect ticket state.
- `in_progress` never appears as a terminal customer failure.
- Low-risk consecutive questions remain answerable by AI and all regression checks pass.
