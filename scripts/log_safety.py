"""Only fixed diagnostics may cross the logging boundary."""
import os


class SafeFailure(RuntimeError):
    pass


def diagnostic(exc):
    # Never stringify an arbitrary exception, response, or URL.
    if isinstance(exc, SafeFailure):
        return str(exc)
    if isinstance(exc, TimeoutError):
        return "operation failed: timeout"
    if isinstance(exc, OSError):
        return "operation failed: I/O error"
    return "operation failed: unexpected error"


def checked_token(value):
    if not isinstance(value, str) or not value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise SafeFailure("token value is missing or invalid")
    return value


def register_mask(value):
    checked_token(value)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("::add-mask::" + value, flush=True)


def get_json(http, url, params, stage):
    if stage not in {"refresh", "profile"}:
        raise SafeFailure("invalid request stage")
    try:
        response = http.get(url, params=params, timeout=30)
    except Exception:
        raise SafeFailure(stage + " failed: transport error") from None
    if not response.ok:
        status = response.status_code
        code = str(status) if type(status) is int and 100 <= status <= 599 else "unknown"
        raise SafeFailure(stage + " failed: HTTP " + code)
    try:
        payload = response.json()
    except Exception:
        raise SafeFailure(stage + " failed: invalid JSON") from None
    if not isinstance(payload, dict):
        raise SafeFailure(stage + " failed: invalid response")
    return payload

