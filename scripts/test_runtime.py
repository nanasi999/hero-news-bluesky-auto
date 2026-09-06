"""Explicit injection only: real credential/storage adapters are intentionally absent."""
from log_safety import SafeFailure


class Runtime:
    def __init__(self, coordinator, backend):
        self.coordinator, self.backend = coordinator, backend

    def post(self, platform, identifiers, payload, legacy_posted):
        return self.coordinator.post(platform, identifiers, payload, self.backend, legacy_posted)


def require_runtime(runtime):
    if runtime is None:
        raise SafeFailure("Test edition requires an injected durable runtime; live execution disabled")
    runtime.coordinator.before_send()
    return runtime

