from collections.abc import MutableMapping

from agent.orchestration.runtime import project_safe_history


REQUEST_ID_SESSION_KEY = "keyguard_v2_request_id"
REQUEST_HISTORY_ID_SESSION_KEY = "keyguard_v2_request_history_id"
REQUEST_HISTORY_SESSION_KEY = "keyguard_v2_request_history"


def get_or_freeze_safe_history(
    state: MutableMapping[str, object],
    request_id: str,
    messages: object,
) -> list[dict[str, str]]:
    """Freeze the safe pre-request history for every retry of one request ID."""
    if state.get(REQUEST_HISTORY_ID_SESSION_KEY) == request_id:
        return project_safe_history(state.get(REQUEST_HISTORY_SESSION_KEY))

    snapshot = project_safe_history(messages)
    state[REQUEST_HISTORY_ID_SESSION_KEY] = request_id
    state[REQUEST_HISTORY_SESSION_KEY] = [dict(item) for item in snapshot]
    return [dict(item) for item in snapshot]


def clear_frozen_request(state: MutableMapping[str, object]) -> None:
    state.pop(REQUEST_ID_SESSION_KEY, None)
    state.pop(REQUEST_HISTORY_ID_SESSION_KEY, None)
    state.pop(REQUEST_HISTORY_SESSION_KEY, None)
