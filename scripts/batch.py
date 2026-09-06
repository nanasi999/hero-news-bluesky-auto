"""Shared, injectable batch control for the isolated edition."""
from log_safety import diagnostic, checked_token, register_mask


def run(mode, bluesky, threads, resolve_token, save, cancelled=lambda: False):
    if mode not in {"auto", "threads"}:
        raise ValueError("invalid mode")
    result = {}
    for platform, action in (("bluesky", bluesky), ("threads", threads)):
        if mode == "threads" and platform == "bluesky":
            continue
        if cancelled():
            result[platform] = "cancelled"
            continue
        try:
            if platform == "threads":
                token = checked_token(resolve_token())
                register_mask(token)
                if cancelled():
                    result[platform] = "cancelled"
                    continue
                code = action(token)
            else:
                code = action()
            result[platform] = "success" if code == 0 else "failure"
        except Exception as exc:
            result[platform] = "failure"
            print(platform + ": " + diagnostic(exc))
    try:
        save()
    except Exception as exc:
        result["save"] = "failure"
        print("save: " + diagnostic(exc))
    return (0 if all(v == "success" for v in result.values()) else 1), result

