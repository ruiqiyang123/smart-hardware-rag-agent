# Customer-Directed Handoff and Narrow Risk Routing Implementation Plan

Spec: `docs/superpowers/specs/2026-08-02-customer-directed-handoff-risk-routing-design.md`

## Goal

Make human handoff a customer-controlled action from the first answered turn, preserve deterministic pre-persistence secret redaction, restrict automatic escalation to explicit financial-security incidents, and turn low-risk Triage schema failures into a safe clarification instead of an unnecessary human ticket.

## Task 1: Lock the configuration and risk contracts

Files:

- Modify `config/orchestration.yml`
- Modify `utils/config_handler.py`
- Modify `agent/orchestration/state.py`
- Modify `tests/test_orchestration_config.py`
- Modify `tests/test_ticket_state.py`

Steps:

1. Add failing tests that the orchestration config no longer accepts or requires `customer_handoff.ai_attempt_threshold`.
2. Add failing tests that `manual_gate_actions` no longer controls `device_reset`, `bootloader_recovery`, or `warranty_decision` routing.
3. Add risk-contract tests distinguishing physical `device_loss_damage` from confirmed `asset_loss`.
4. Keep `asset_loss`, `secret_exposure`, and `phishing` at the critical/P0/escalate floor.
5. Keep `address_mismatch` and `suspicious_signature` at the high/P1/escalate floor.
6. Stop treating a bare `unofficial_firmware`, `device_auth_failure`, or generic `remote_control` flag as sufficient for automatic escalation; explicit phishing or credential exposure remains critical.
7. Implement the narrow configuration and validator changes.

Verification:

```bash
pytest -q tests/test_orchestration_config.py tests/test_ticket_state.py
```

## Task 2: Add the runtime Triage policy pack

Files:

- Add `config/triage_policy.yml`
- Modify `utils/config_handler.py`
- Modify `agent/nodes/triage.py`
- Modify `prompts/triage_prompt.txt`
- Modify `tests/test_prompt_contract.py`
- Extend `tests/test_agent_contracts.py`
- Extend `tests/test_ticket_state.py`

Steps:

1. Add strict config tests for policy definitions, escalation examples, non-escalation examples, and clarification options.
2. Define `device_loss_damage` as physical device loss/damage without evidence of unauthorized asset movement.
3. Define `asset_loss` as observed or explicitly suspected unauthorized asset movement, not a keyword match on “丢失”.
4. Define ordinary transaction failures, pending transactions, restore-zero-balance, and account-sync issues as AI-first paths.
5. Add contrastive examples for device loss versus asset theft, transaction failure versus unauthorized transfer, and secret knowledge questions versus confirmed disclosure.
6. Load the policy pack explicitly into the Triage payload or safe prompt context; do not rely on Codex `SKILL.md` discovery.
7. Keep the payload bounded, deterministic, serializable, and free of external or user-controlled file paths.
8. Add tests for the exact bad case “设备丢了或坏了，资产还能恢复吗？” and for the matching high-risk counterexample.

Verification:

```bash
pytest -q tests/test_prompt_contract.py tests/test_agent_contracts.py tests/test_ticket_state.py
```

## Task 3: Remove action-based automatic handoff

Files:

- Modify `agent/orchestration/graph.py`
- Modify `agent/nodes/diagnosis.py` if action normalization requires it
- Modify `tests/test_human_in_loop.py`
- Modify `tests/test_agent_contracts.py`
- Modify `tests/test_review_hardening.py`

Steps:

1. Add failing tests showing low-risk `device_reset`, `bootloader_recovery`, and `warranty_decision` recommendations do not automatically set `requires_human`.
2. Preserve non-execution boundaries: AI may provide preparation and safety guidance but cannot claim it executed a reset, recovery action, or warranty approval.
3. Remove `manual_gate_actions` from graph construction and validation without weakening risk-driven escalation.
4. Ensure Review and Policy Guard reject claims of completed external actions or unsupported warranty outcomes.
5. Keep customer-controlled handoff available for users who want those actions handled by an operator.

Verification:

```bash
pytest -q tests/test_human_in_loop.py tests/test_agent_contracts.py tests/test_review_hardening.py
```

## Task 4: Implement low-risk Triage fallback clarification

Files:

- Modify `agent/orchestration/graph.py`
- Modify `agent/orchestration/state.py` only if a stable fallback reason field is needed
- Modify `agent/orchestration/runtime.py`
- Modify `agent/ui_progress.py`
- Extend `tests/test_failure_fallbacks.py`
- Extend `tests/test_orchestration_routes.py`
- Extend `tests/test_ui_progress.py`

Steps:

1. Add failing tests for two exhausted Triage validation attempts on a low/medium ingress with no risk flags.
2. Return a deterministic clarification result and valid `pending_user` state through legal state transitions.
3. Record `TRIAGE_FALLBACK_CLARIFICATION` in the audited event trail without persisting model raw output or exception text.
4. Clear prior-turn category, summary, evidence, draft, priority, and risk presentation so the UI cannot present stale values as the failed turn’s result.
5. Add the fixed clarification prompt covering device failure, connection, recovery, transaction display, and unauthorized asset movement.
6. Keep existing fail-closed escalation when ingress or sticky state contains high/critical risk, critical/high whitelist flags, or `requires_human`.
7. Keep timeout, retry, lease, checkpoint, and command-idempotency behavior unchanged.

Verification:

```bash
pytest -q tests/test_failure_fallbacks.py tests/test_orchestration_routes.py tests/test_ui_progress.py
```

## Task 5: Make customer-requested handoff immediately available

Files:

- Modify `agent/orchestration/runtime.py`
- Modify `agent/orchestration/handoff_intent.py`
- Modify `app.py`
- Modify `tests/test_handoff_intent.py`
- Modify `tests/test_user_confirmed_closure.py`
- Modify `tests/test_app_v2_contract.py`

Steps:

1. Add failing tests that any active `pending_user` ticket can execute `customer_request_human` after its first AI answer.
2. Remove `count_ai_answers` from the authorization decision while keeping it available only if useful for analytics.
3. Remove threshold parameters from `SupportOrchestrator` construction and validation.
4. Keep deterministic natural-language handoff recognition and button actions outside Triage.
5. Keep idempotency, command lease, required status, audit event, and `manual_gate_reason=customer_requested_human`.
6. Make the customer-page handoff button always enabled for an active answered ticket.
7. Remove `1/3`, `2/3`, `3/3`, lock messages, and AI-first denial copy.
8. Render a distinct confirmation for customer-requested handoff.

Verification:

```bash
pytest -q tests/test_handoff_intent.py tests/test_user_confirmed_closure.py tests/test_app_v2_contract.py
```

## Task 6: Align workbench reasons, evaluation cases, and documentation

Files:

- Modify `app.py`
- Modify `eval/multi_agent_cases.json`
- Modify `tests/test_orchestration_eval.py`
- Modify `README.md`
- Modify `docs/DEMO_SCRIPT.md`
- Modify `tests/test_demo_readiness.py`

Steps:

1. Show separate workbench labels for customer-requested handoff and financial-security auto escalation.
2. Keep low-risk Triage fallback out of the human queue while exposing its audit event.
3. Update the device-loss and damaged-device evaluation cases to expect AI-first `pending_user` behavior.
4. Add or update cases for ordinary transaction failure, unauthorized transfer, confirmed credential disclosure, and low-risk clarification.
5. Remove README and demo-script claims about the three-answer handoff threshold.
6. Document the narrow escalation whitelist, fallback clarification, and unchanged pre-persistence redaction boundary.
7. Keep all claims honest: no production pressure reduction, response-rate improvement, or unsupported V2 accuracy metric.

Verification:

```bash
pytest -q tests/test_orchestration_eval.py tests/test_demo_readiness.py tests/test_app_v2_contract.py
```

## Task 7: Full regression and browser acceptance

Files:

- Modify only files required by failures attributable to this approved design

Steps:

1. Run the full automated suite.
2. Run compile and whitespace checks.
3. Restart or reload the local Streamlit app so the current contract version is active.
4. In one customer session, verify a first-turn answer can be handed to a human immediately.
5. Verify “设备丢了或坏了，资产还能恢复吗？” receives an AI answer on both first and later turns.
6. Verify “交易失败了” remains AI-first.
7. Verify “我的钱不见了” asks a bounded clarification question.
8. Verify “发现一笔不是我发起的转账” automatically escalates with a financial-security reason.
9. Verify a test secret is redacted before persistence and a confirmed disclosure automatically escalates.
10. Inject or simulate a low-risk Triage validation failure and verify clarification instead of human handoff.
11. Confirm customer status, workbench reason, audit trail, and input placement stay consistent.
12. Commit the implementation without pushing unless the user requests publishing.

Verification:

```bash
pytest -q
python -m compileall -q agent database model rag utils app.py
git diff --check
```

## Completion Gate

Do not mark this change complete until all focused and full tests pass, the exact observed bad case no longer escalates, first-answer customer handoff works, secret redaction remains pre-persistence, explicit financial-security incidents still auto-escalate, the browser acceptance flow passes, and no stale prior-turn classification is shown after a low-risk Triage fallback.
