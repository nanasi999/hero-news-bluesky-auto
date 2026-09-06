"""Durable test coordinator. No production storage or SNS binding is provided."""
import copy
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from log_safety import SafeFailure


class Held(SafeFailure):
    pass


class Coordinator:
    def __init__(self, store, owner, owner_status, cancelled=lambda: False):
        if not owner or not isinstance(owner, str):
            raise SafeFailure("owner identity required")
        self.store, self.owner = store, owner
        self.owner_status, self.cancelled = owner_status, cancelled
        self.acquired = False

    def acquire(self):
        revision, body = self.store.read()
        previous = body["lock"]
        if previous is not None:
            # No TTL: unknown status never grants permission to steal a lock.
            try:
                status = self.owner_status(previous)
            except Exception:
                raise Held("lock owner status unavailable") from None
            if status in {"queued", "in_progress", "waiting", "pending", "requested"}:
                return False
            if status != "completed":
                raise Held("lock owner status unknown")
        body["lock"] = self.owner
        self.acquired = self.store.cas(revision, body)
        return self.acquired

    def snapshot(self):
        revision, body = self.store.read()
        if not self.acquired or body["lock"] != self.owner:
            raise Held("posting lock not owned")
        return revision, body

    def change(self, operation):
        revision, body = self.snapshot()
        result = operation(body)
        if not self.store.cas(revision, body):
            raise Held("checkpoint conflict")
        return result

    def before_send(self):
        self.snapshot()
        if self.cancelled():
            raise Held("run cancelled before send")

    def release(self):
        if self.acquired:
            self.change(lambda body: body.update(lock=None))
            self.acquired = False

    def post(self, platform, identifiers, payload, backend, legacy_posted):
        if platform not in {"bluesky", "threads"}:
            raise SafeFailure("unsupported platform")
        ids = sorted(set(str(x).strip() for x in identifiers if str(x).strip()))
        if not ids:
            raise SafeFailure("article identity missing")
        self.before_send()
        _, body = self.snapshot()
        matches = [(key, row) for key, row in body["posts"].items() if row["platform"] == platform and set(ids).intersection(row["identifiers"])]
        if len(matches) > 1:
            raise Held("ambiguous article identity")
        if matches:
            key, row = matches[0]
            combined = sorted(set(row["identifiers"]).union(ids))
            if combined != row["identifiers"]:
                self.change(lambda data: data["posts"][key].update(identifiers=combined))
                row["identifiers"] = combined
            if row["stage"] == "confirmed":
                return copy.deepcopy(row)
        elif set(ids).intersection(legacy_posted):
            return {"stage": "confirmed", "legacy": True}
        else:
            key = hashlib.sha256((platform + "\n" + ids[0]).encode()).hexdigest()
            row = {"platform":platform,"identifiers":ids,"payload":copy.deepcopy(payload),"stage":"intent"}
            self.change(lambda data: data["posts"].update({key: row}))
        # Recovery uses the original payload, not a later edited article.
        payload = row["payload"]

        def stage(name, **fields):
            def update(data):
                data["posts"][key].update(stage=name, **fields)
                return copy.deepcopy(data["posts"][key])
            return self.change(update)

        if platform == "bluesky":
            if row["stage"] != "intent":
                existing = backend.lookup_bluesky(key)
                if existing is None or existing.get("payload") != payload or not existing.get("id"):
                    raise Held("Bluesky result uncertain; no resend")
                return stage("confirmed", result=existing["id"])
            stage("sending")
            self.before_send()
            result = backend.send_bluesky(key, payload)
            if not isinstance(result, dict) or not result.get("id"):
                raise Held("Bluesky response uncertain")
            return stage("confirmed", result=result["id"])

        if row["stage"] == "intent":
            stage("creating")
            self.before_send()
            container = backend.create_threads(payload)
            if not isinstance(container, str) or not container:
                raise Held("Threads creation uncertain")
            row = stage("prepared", container=container)
        if row["stage"] == "creating":
            raise Held("Threads container unknown; no recreation")
        if row["stage"] == "publishing":
            status = backend.threads_status(row["container"])
            if status == "PUBLISHED":
                return stage("confirmed", published_container=row["container"])
            raise Held("Threads publication uncertain; no republish")
        if row["stage"] != "prepared":
            raise Held("unexpected journal stage")
        readiness = backend.threads_status(row["container"])
        if readiness == "PUBLISHED":
            return stage("confirmed", published_container=row["container"])
        if readiness != "FINISHED":
            raise Held("Threads container not ready; publication deferred")
        stage("publishing")
        self.before_send()
        result = backend.publish_threads(row["container"])
        if not isinstance(result, dict) or not result.get("id"):
            raise Held("Threads publication response uncertain")
        return stage("confirmed", result=result["id"])

