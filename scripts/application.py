"""Fully injected test entry; intentionally no live backend or CLI defaults."""
import post_to_bluesky
import post_to_threads
import threads_token_store
import workflow_runner
from test_runtime import Runtime


def resolve():
    return threads_token_store.current_token(threads_token_store.require_seed())


def run(mode, coordinator, bluesky_backend, threads_backend_factory,
        save, resolve_token=resolve, cancelled=lambda: False):
    def blue():
        return post_to_bluesky.main(Runtime(coordinator, bluesky_backend))
    def threads(token):
        backend = threads_backend_factory(token)
        return post_to_threads.main(Runtime(coordinator, backend))
    return workflow_runner.run(mode, coordinator, blue, threads,
                               resolve_token, save, cancelled)

