"""Bounded API adapters. Never retry an ambiguous publication."""
import os
import time
import re
from datetime import datetime, timezone
import requests
from atproto import Client, client_utils, models
from log_safety import SafeFailure, checked_token, register_mask
from post_to_bluesky import login_with_retry


def bluesky_failure(exc):
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    content = getattr(response, "content", None)
    code = content.get("error") if isinstance(content, dict) else getattr(content, "error", None)
    known = {"RecordNotFound", "InvalidRequest", "InvalidToken", "ExpiredToken",
             "AuthenticationRequired", "RateLimitExceeded"}
    kind = code if isinstance(code, str) and code in known else "unclassified"
    cls = type(exc).__name__
    if cls in {"ValidationError", "AttributeError", "TypeError", "ModelError"}:
        kind = cls
    return ("HTTP " + str(status) + " " if type(status) is int else "") + kind


class GitHubHTTP:
    def __init__(self, repository, token, session=None):
        if len(repository.split("/")) != 2 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./" for c in repository):
            raise SafeFailure("Invalid repository")
        self.base = "https://api.github.com/repos/" + repository
        self.session = session or requests.Session()
        self.headers = {"Authorization": "Bearer " + checked_token(token),
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28"}

    def __call__(self, method, path, **kwargs):
        if not path.startswith("/") or "://" in path:
            raise SafeFailure("Invalid GitHub API path")
        try:
            response = self.session.request(method, self.base + path,
                headers=self.headers, timeout=(10,30), allow_redirects=False, **kwargs)
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError()
            return response.status_code, data
        except Exception:
            raise SafeFailure("GitHub request failed") from None


class BlueskyBackend:
    def __init__(self, client=None):
        self.client = client
        self.did = None

    def connect(self):
        if self.did:
            return
        client = self.client or Client()
        handle = os.environ.get("BLUESKY_HANDLE", "")
        password = os.environ.get("BLUESKY_APP_PASSWORD", "")
        if not handle or not password:
            raise SafeFailure("Bluesky credentials missing")
        try:
            profile = login_with_retry(client, handle, password)
            if not profile.did:
                raise ValueError()
            self.did, self.client = profile.did, client
        except Exception:
            raise SafeFailure("Bluesky authentication failed") from None

    def prepare(self, payload):
        self.connect()
        base = (payload["prefix"] + payload["title"]).strip()
        suffix = "\n読む"
        limit = 300 - len(suffix)
        if len(base) > limit:
            base = base[:limit-3].rstrip() + "..."
        text = client_utils.TextBuilder().text(base).text("\n").link("読む",payload["link"])
        record = models.AppBskyFeedPost.Record(
            text=text.build_text(), facets=text.build_facets(),
            created_at=datetime.now(timezone.utc).isoformat().replace("+00:00","Z"))
        return record.model_dump(by_alias=True, exclude_none=True)

    def new_bluesky_key(self, key, payload):
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise SafeFailure("Invalid journal identity")
        created = datetime.fromisoformat(payload["createdAt"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise SafeFailure("Missing record timestamp timezone")
        delta = created - datetime(1970, 1, 1, tzinfo=timezone.utc)
        micros = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
        value = (micros << 10) | (int(key[-3:], 16) & 1023)
        if not 0 < value < 2**63:
            raise SafeFailure("Record timestamp out of range")
        alphabet = "234567abcdefghijklmnopqrstuvwxyz"
        return "".join(alphabet[(value >> shift) & 31] for shift in range(60, -1, -5))

    def send_bluesky(self, key, payload):
        self.connect()
        try:
            result = self.client.com.atproto.repo.create_record(data={
                "repo":self.did, "collection":"app.bsky.feed.post",
                "rkey":key, "record":payload})
            return {"id":result.uri}
        except Exception as exc:
            raise SafeFailure("Bluesky publication uncertain: " + bluesky_failure(exc)) from None

    def lookup_bluesky(self, key):
        self.connect()
        try:
            result = self.client.com.atproto.repo.get_record(params={
                "repo":self.did,"collection":"app.bsky.feed.post","rkey":key})
            value = result.value
            if hasattr(value,"model_dump"):
                value = value.model_dump(by_alias=True, exclude_none=True)
            return {"id":result.uri,"payload":value}
        except Exception as exc:
            if bluesky_failure(exc) == "HTTP 400 RecordNotFound":
                return None
            raise SafeFailure("Bluesky result lookup failed: " + bluesky_failure(exc)) from None


class ThreadsBackend:
    def __init__(self, token, user_id, session=None, sleep=time.sleep):
        if not user_id or not user_id.isdigit():
            raise SafeFailure("Threads user ID missing or invalid")
        self.token, self.user_id = checked_token(token), user_id
        register_mask(self.token)
        self.session, self.sleep = session or requests.Session(), sleep

    def request(self, method, path, **kwargs):
        try:
            response = self.session.request(method,"https://graph.threads.net/v1.0/"+path,
                headers={"Authorization":"Bearer "+self.token},
                timeout=(10,30), allow_redirects=False, **kwargs)
            if not response.ok:
                raise SafeFailure("Threads HTTP " + str(int(response.status_code)))
            data = response.json()
            if not isinstance(data,dict):
                raise ValueError()
            return data
        except SafeFailure:
            raise
        except Exception:
            raise SafeFailure("Threads API response uncertain") from None

    def health(self):
        result=self.request("GET","me",params={"fields":"id"})
        if result.get("id") != self.user_id:
            raise SafeFailure("Threads account mismatch")

    def create_threads(self, payload):
        return self.request("POST",self.user_id+"/threads",
            data={"media_type":"TEXT","text":payload["text"]}).get("id")

    def publish_threads(self, container):
        return self.request("POST",self.user_id+"/threads_publish",
                            data={"creation_id":container})

    def threads_status(self, container):
        for attempt in range(3):
            status = self.request("GET",container,params={"fields":"status"}).get("status")
            if status != "IN_PROGRESS":
                return status
            if attempt < 2:
                self.sleep(10)
        return status
