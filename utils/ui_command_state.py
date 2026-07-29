from collections.abc import Callable, MutableMapping

from agent.orchestration.runtime import project_safe_history


REQUEST_ID_SESSION_KEY = "keyguard_v2_request_id"
REQUEST_HISTORY_ID_SESSION_KEY = "keyguard_v2_request_history_id"
REQUEST_HISTORY_SESSION_KEY = "keyguard_v2_request_history"
REQUEST_PAYLOAD_SESSION_KEY = "keyguard_v2_request_payload"
ACTION_ID_SESSION_PREFIX = "keyguard_v2_action_id:"
ACTION_PAYLOAD_SESSION_PREFIX = "keyguard_v2_action_payload:"
EDITOR_SESSION_PREFIX = "keyguard_v2_editor:"
_MISSING = object()


def get_or_freeze_request_command(
    state: MutableMapping[str, object],
    request_id: str,
    messages: object,
    prepare: Callable[[], object],
) -> tuple[object, list[dict[str, str]], bool]:
    """Atomically freeze a prepared payload and pre-request history."""
    if state.get(REQUEST_HISTORY_ID_SESSION_KEY) == request_id:
        prepared = state.get(REQUEST_PAYLOAD_SESSION_KEY, _MISSING)
        history = state.get(REQUEST_HISTORY_SESSION_KEY, _MISSING)
        if prepared is _MISSING or history is _MISSING:
            raise ValueError("request retry snapshot 不完整")
        return prepared, project_safe_history(history), True

    snapshot = project_safe_history(messages)
    prepared = prepare()
    state[REQUEST_HISTORY_ID_SESSION_KEY] = request_id
    state[REQUEST_HISTORY_SESSION_KEY] = [dict(item) for item in snapshot]
    state[REQUEST_PAYLOAD_SESSION_KEY] = prepared
    return prepared, [dict(item) for item in snapshot], False


def _action_payload_key(action_key: str) -> str:
    if not isinstance(action_key, str) or not action_key.startswith(
        ACTION_ID_SESSION_PREFIX
    ):
        raise ValueError("action session key 非法")
    return ACTION_PAYLOAD_SESSION_PREFIX + action_key[len(ACTION_ID_SESSION_PREFIX) :]


def get_or_freeze_action_command(
    state: MutableMapping[str, object],
    action_key: str,
    action_id: str,
    prepare: Callable[[], object],
) -> tuple[object, bool]:
    """Freeze one safe human-action payload for its stable action ID."""
    if state.get(action_key) != action_id:
        raise ValueError("action id 状态不一致")
    payload_key = _action_payload_key(action_key)
    existing = state.get(payload_key, _MISSING)
    if existing is not _MISSING:
        if (
            not isinstance(existing, tuple)
            or len(existing) != 2
            or existing[0] != action_id
        ):
            raise ValueError("action retry snapshot 不完整")
        return existing[1], True
    prepared = prepare()
    state[payload_key] = (action_id, prepared)
    return prepared, False


def clear_frozen_request(state: MutableMapping[str, object]) -> None:
    state.pop(REQUEST_ID_SESSION_KEY, None)
    state.pop(REQUEST_HISTORY_ID_SESSION_KEY, None)
    state.pop(REQUEST_HISTORY_SESSION_KEY, None)
    state.pop(REQUEST_PAYLOAD_SESSION_KEY, None)


def clear_frozen_action(
    state: MutableMapping[str, object], action_key: str
) -> None:
    state.pop(action_key, None)
    state.pop(_action_payload_key(action_key), None)


def clear_frozen_actions(state: MutableMapping[str, object]) -> None:
    for key in list(state):
        if isinstance(key, str) and (
            key.startswith(ACTION_ID_SESSION_PREFIX)
            or key.startswith(ACTION_PAYLOAD_SESSION_PREFIX)
        ):
            state.pop(key, None)


def clear_editor_state(state: MutableMapping[str, object]) -> None:
    """Clear editor widgets only before their next instantiation."""
    for key in list(state):
        if isinstance(key, str) and key.startswith(EDITOR_SESSION_PREFIX):
            state.pop(key, None)
