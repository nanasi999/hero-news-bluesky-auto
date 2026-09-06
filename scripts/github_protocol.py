"""GitHub REST contracts tested through injected mock HTTP only."""
import base64
import json
import re
from log_safety import SafeFailure


def response(call, method, path, **kwargs):
    try:
        status, data = call(method, path, **kwargs)
    except Exception:
        raise SafeFailure("GitHub transport failed") from None
    if type(status) is not int or not isinstance(data, dict):
        raise SafeFailure("GitHub response invalid")
    return status, data


class GitHubStore:
    def __init__(self, call, branch, path="test-ledger.json"):
        if not (branch.startswith("test/") or branch == "actions-state/social-posting") or path != "test-ledger.json":
            raise SafeFailure("Only test ledger destinations are permitted")
        self.call, self.branch, self.path = call, branch, "/contents/"+path

    def read(self):
        status, data = response(self.call,"GET",self.path,params={"ref":self.branch})
        if status != 200:
            raise SafeFailure("Ledger must be provisioned before posting")
        try:
            if data["encoding"] != "base64" or not isinstance(data["sha"],str) or not data["sha"]:
                raise ValueError()
            body=json.loads(base64.b64decode(data["content"]))
            if not isinstance(body,dict) or "lock" not in body or not isinstance(body.get("posts"),dict):
                raise ValueError()
            return data["sha"],body
        except Exception:
            raise SafeFailure("Ledger content invalid") from None

    def cas(self, revision, body):
        if not revision:
            raise SafeFailure("Ledger revision required")
        payload={"message":"Update test ledger","branch":self.branch,"sha":revision,
                 "content":base64.b64encode(json.dumps(body).encode()).decode()}
        status,data=response(self.call,"PUT",self.path,json=payload)
        if status == 409:
            return False
        if status != 200 or not isinstance(data.get("content"),dict) or not data["content"].get("sha"):
            raise SafeFailure("Ledger checkpoint not confirmed")
        return True


class OwnerStatus:
    def __init__(self, call):self.call=call
    def __call__(self, owner):
        match=re.fullmatch(r"run:(\d+):attempt:(\d+)",owner)
        if not match:raise SafeFailure("Lock owner format invalid")
        run,attempt=map(int,match.groups())
        status,data=response(self.call,"GET",f"/actions/runs/{run}")
        if status != 200 or data.get("id") != run or type(data.get("run_attempt")) is not int or data["run_attempt"] < attempt:
            raise SafeFailure("Lock owner could not be verified")
        state=data.get("status")
        if state not in {"completed","queued","in_progress","waiting","pending","requested"}:
            raise SafeFailure("Lock owner state unknown")
        return state

