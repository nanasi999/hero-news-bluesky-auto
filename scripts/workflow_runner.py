"""The two test entry routes share token resolution, locking and final status."""
import batch
from log_safety import diagnostic


def run(mode, coordinator, bluesky, threads, resolve_token, save, cancelled=lambda: False):
    if mode not in {"auto", "threads"}:
        raise ValueError("invalid mode")
    if cancelled():
        return 1, {"run": "cancelled"}
    try:
        if not coordinator.acquire():
            return 0, {"run": "busy"}
    except Exception as exc:
        print("lock: " + diagnostic(exc))
        return 1, {"lock": "failure"}
    code, results = 1, {}
    try:
        code, results = batch.run(mode,bluesky,threads,resolve_token,save,cancelled)
    finally:
        try:
            coordinator.release()
        except Exception as exc:
            print("lock release: " + diagnostic(exc))
            results["lock_release"] = "failure"
            code = 1
    return code, results

