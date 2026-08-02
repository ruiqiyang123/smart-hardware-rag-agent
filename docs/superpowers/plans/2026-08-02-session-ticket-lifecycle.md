# Session Ticket Lifecycle Implementation Plan

Spec: `docs/superpowers/specs/2026-08-02-session-ticket-lifecycle-design.md`

## Goal

Turn the current issue-shaped pending ticket into a continuous customer-service session: every turn is safely re-triaged, low-risk topic changes stay in the same ticket, 30 minutes of inactivity ends the session as `closed`, explicit resolution remains `resolved`, and human handoff is available after three safe AI answers or immediately for risk/system gates.

## Task 1: Lock the state and configuration contracts

Files:

- Modify `agent/orchestration/state.py`
- Modify `agent/orchestration/routes.py`
- Modify `utils/config_handler.py`
- Modify `config/orchestration.yml`
- Modify `tests/test_ticket_state.py`
- Modify `tests/test_orchestration_routes.py`
- Modify `tests/test_orchestration_config.py`

Steps:

1. Add failing tests for `Status.CLOSED` and `pending_user -> closed`.
2. Add failing tests that `closed` is terminal and cannot become `resolved`, `pending_user`, or `escalated`.
3. Add strict configuration tests for 1800-second inactivity timeout, 30-second poll interval, and three-answer handoff threshold.
4. Remove `wallet_recovery` from `manual_gate_actions` expectations while retaining controlled operations.
5. Implement the enum, transition and configuration changes.
6. Run the focused tests.

Verification:

```bash
pytest -q tests/test_ticket_state.py tests/test_orchestration_routes.py tests/test_orchestration_config.py
```

## Task 2: Migrate persistence to Schema v8

Files:

- Modify `database/ticket_db.py`
- Modify `tests/test_ticket_repository.py`

Steps:

1. Add failing migration tests from Schema v7 with existing tickets, commands and events.
2. Add `idle_expires_at`, `closed_at`, and `close_reason` fields and their projections.
3. Add `customer_request_human` and `system_idle_close` command types.
4. Rebuild state-constrained SQLite tables without dropping existing rows.
5. Add a partial index for due `pending_user` sessions.
6. Add repository methods to set/clear deadlines, list due tickets, conditionally close a due ticket, and count successful customer-visible answers.
7. Ensure conditional close returns a no-op when the user path already cleared the deadline.
8. Run repository tests.

Verification:

```bash
pytest -q tests/test_ticket_repository.py
```

## Task 3: Implement the expiry service and runtime commands

Files:

- Add `agent/orchestration/session_expiry.py`
- Modify `agent/orchestration/runtime.py`
- Modify `agent/orchestration/graph.py`
- Add `tests/test_session_expiry.py`
- Extend `tests/test_failure_fallbacks.py`

Steps:

1. Add tests using an injected UTC clock; never sleep in tests.
2. Implement deadline calculation, clearing and bounded expiry scanning.
3. Write idempotent `system_idle_close` command/event records.
4. Clear the deadline when user input wins the command lease.
5. Set a new deadline after each customer-visible AI or human response.
6. Add strict runtime result validation for `closed`.
7. Ensure `escalated` and `resolved` never expire.
8. Cover close/input races and repeated scans.

Verification:

```bash
pytest -q tests/test_session_expiry.py tests/test_failure_fallbacks.py
```

## Task 4: Keep cross-topic questions in one active session

Files:

- Modify `app.py`
- Modify `agent/orchestration/runtime.py`
- Add `tests/test_session_ticket_flow.py`
- Extend `tests/test_user_confirmed_closure.py`

Steps:

1. Add a test that bluetooth, device-loss recovery and firmware questions reuse one `pending_user` ticket within 30 minutes.
2. Re-run Triage/Diagnosis/Review for every turn so the latest category and evidence replace current-turn fields while history remains in events.
3. When an active ticket is `closed`, create a new ticket and conversation id with `parent_ticket_id` pointing to the old ticket.
4. Keep explicit resolution on the existing audited `pending_user -> resolved` path.
5. Ensure a high-risk later turn escalates the current active session and preserves risk stickiness.

Verification:

```bash
pytest -q tests/test_session_ticket_flow.py tests/test_user_confirmed_closure.py tests/test_human_in_loop.py
```

## Task 5: Add customer-requested human handoff

Files:

- Add `agent/orchestration/human_intent.py`
- Modify `agent/orchestration/runtime.py`
- Modify `app.py`
- Add `tests/test_human_intent.py`
- Extend `tests/test_session_ticket_flow.py`

Steps:

1. Add deterministic, bounded natural-language recognition for explicit human requests.
2. Derive the AI answer count from successful `response_finalized` events.
3. Before three answers, return fixed AI-first guidance without sending “找人工” through hardware diagnosis.
4. At three answers, expose one idempotent customer handoff command.
5. Transition `pending_user -> escalated` with `manual_gate_reason=customer_requested_after_ai`.
6. Verify high/critical and system failure paths bypass the threshold.

Verification:

```bash
pytest -q tests/test_human_intent.py tests/test_session_ticket_flow.py tests/test_ingress_guard.py
```

## Task 6: Narrow the manual action gate

Files:

- Modify `config/orchestration.yml`
- Modify `agent/orchestration/graph.py` only if required by stricter validation
- Modify `tests/test_human_in_loop.py`
- Modify `tests/test_agent_contracts.py`

Steps:

1. Add a failing test for low-risk device loss with verified backup.
2. Assert that `wallet_recovery` can reach Review/Finalize without manual gating when risk is low.
3. Keep device reset, bootloader recovery and warranty decision gated.
4. Keep all existing risk flags and security incidents immediately escalated.
5. Run the focused security and graph tests.

Verification:

```bash
pytest -q tests/test_human_in_loop.py tests/test_agent_contracts.py tests/test_ingress_guard.py
```

## Task 7: Fix the customer layout and workbench

Files:

- Modify `app.py`
- Modify `agent/ui_progress.py`
- Modify `tests/test_app_v2_contract.py`
- Modify `tests/test_ui_progress.py`

Steps:

1. Render messages, ticket state and processing output into containers above `st.chat_input`.
2. Keep the input available for `closed` so the next message starts a new ticket.
3. Add “服务中 / 已结束 / 已解决 / 待人工” labels.
4. Show the 30-minute inactivity rule and AI answer count.
5. Rename follow-up to “未解决，继续追问”.
6. Add the visible human entry and its locked/unlocked explanation.
7. Add a 30-second `st.fragment` expiry scan.
8. Add `closed` workbench filtering, read-only rendering, timestamps and reason display.
9. Ensure no internal prompt, tool payload or chain-of-thought is rendered.

Verification:

```bash
pytest -q tests/test_app_v2_contract.py tests/test_ui_progress.py
```

## Task 8: Documentation, regression and browser acceptance

Files:

- Modify `README.md`
- Modify demo-readiness tests when exact documentation contracts change

Steps:

1. Update the architecture flow, eight-state machine, 30-minute lifecycle, human policy and demo script.
2. Run the full suite.
3. Run compile and whitespace checks.
4. Restart/reload the local Streamlit app if necessary.
5. Browser test the six acceptance scenarios from the approved spec.
6. Confirm the input stays below processing and final messages.
7. Commit the implementation without pushing or deploying unless requested.

Verification:

```bash
pytest -q
python -m compileall -q agent database utils app.py
git diff --check
```

## Completion Gate

Do not call the implementation complete until all automated tests pass, the existing database migrates without data loss, all six browser scenarios pass, and the customer UI never labels inactivity as problem resolution.
