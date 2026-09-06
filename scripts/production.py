"""Shared production entry; credentials and existing state remain in GitHub."""
import argparse
import base64
import json
import os
import signal
import time
from pathlib import Path
import post_to_bluesky as blue
import post_to_threads as threads
import threads_token_store as tokens
import workflow_runner
from github_protocol import GitHubStore, OwnerStatus, response
from live_adapters import GitHubHTTP, BlueskyBackend, ThreadsBackend
from log_safety import SafeFailure, diagnostic
from posting_state import Coordinator
from test_runtime import Runtime

LEDGER_BRANCH = "actions-state/social-posting"
STATES = {"bluesky":".bluesky-posted.json","threads":".threads-posted.json"}


def read_state(http, path):
    status,data=response(http,"GET","/contents/"+path,params={"ref":"main"})
    if status != 200:
        raise SafeFailure("Existing posted state unavailable; initialization refused")
    try:
        value=json.loads(base64.b64decode(data["content"]))
        if not isinstance(value.get("posted"),list) or not all(isinstance(x,str) for x in value["posted"]):
            raise ValueError()
        return data["sha"],value
    except Exception:
        raise SafeFailure("Posted state invalid") from None


def hydrate(http):
    for path in STATES.values():
        _,value=read_state(http,path)
        Path(path).write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")


def export_state(http):
    for path in STATES.values():
        local=json.loads(Path(path).read_text(encoding="utf-8"))
        for attempt in range(3):
            sha,remote=read_state(http,path)
            combined=sorted(set(remote["posted"]).union(local["posted"]))
            if combined == sorted(set(remote["posted"])):
                break
            remote["posted"]=combined
            if "updated_at" in local:
                remote["updated_at"]=local["updated_at"]
            status,_=response(http,"PUT","/contents/"+path,json={
                "branch":"main","sha":sha,"message":"Update confirmed social posting state",
                "content":base64.b64encode(json.dumps(remote,ensure_ascii=False).encode()).decode()})
            if status == 200:
                break
            if status != 409 or attempt == 2:
                raise SafeFailure("Posted state export not confirmed")


def legacy_active(http, current_run):
    posting_paths={".github/workflows/post-to-bluesky.yml",".github/workflows/post-to-threads.yml"}
    for state in ["in_progress","queued","waiting","pending","requested"]:
        status,data=response(http,"GET","/actions/runs",params={"status":state,"per_page":100})
        if status != 200 or data.get("total_count",101)>100:
            raise SafeFailure("Active execution check unavailable")
        for run in data.get("workflow_runs",[]):
            if str(run.get("id")) == str(current_run) or run.get("path") not in posting_paths:
                continue
            code,_=response(http,"GET","/contents/scripts/production.py",
                            params={"ref":run["head_sha"]})
            if code == 404:
                return True
            if code != 200:
                raise SafeFailure("Execution version check unavailable")
    return False


class PostingRuntime(Runtime):
    def post(self, platform, identifiers, payload, legacy_posted):
        if platform == "bluesky":
            payload=self.backend.prepare(payload)
        return super().post(platform,identifiers,payload,legacy_posted)

    def recover(self, platform):
        _,body=self.coordinator.snapshot()
        module=blue if platform=="bluesky" else threads
        state=module.load_state()
        failures=0
        for row in list(body["posts"].values()):
            if row["platform"] != platform:
                continue
            try:
                if row["stage"] != "confirmed":
                    self.coordinator.post(platform,row["identifiers"],row["payload"],self.backend,set(state["posted"]))
                if set(row["identifiers"]).issubset(state["posted"]):
                    continue
                state["posted"]=sorted(set(state["posted"]).union(row["identifiers"]))
                module.save_state(state)
            except Exception as exc:
                failures+=1
                print(platform+" recovery: "+diagnostic(exc))
        return failures


def execute(http, coordinator, mode, cancelled):
    prepared=False
    def prepare():
        nonlocal prepared
        if not prepared:
            hydrate(http)
            prepared=True
    def post_blue():
        prepare()
        runtime=PostingRuntime(coordinator,BlueskyBackend())
        recovery_failed=runtime.recover("bluesky")
        return int(bool(blue.main(runtime) or recovery_failed))
    def post_threads(token):
        prepare()
        backend=ThreadsBackend(token,os.environ.get("THREADS_USER_ID",""))
        backend.health()
        runtime=PostingRuntime(coordinator,backend)
        recovery_failed=runtime.recover("threads")
        return int(bool(threads.main(runtime) or recovery_failed))
    def save():
        if prepared:
            export_state(http)
    return workflow_runner.run(mode,coordinator,post_blue,post_threads,
        lambda:tokens.current_token(tokens.require_seed()),save,cancelled)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--mode",choices=["auto","threads"],default="auto")
    parser.add_argument("--health",action="store_true")
    args=parser.parse_args()
    try:
        if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REF") != "refs/heads/main":
            raise SafeFailure("Production entry requires the main Actions runner")
        if args.health:
            BlueskyBackend().connect()
            ThreadsBackend(tokens.current_token(tokens.require_seed()),os.environ.get("THREADS_USER_ID","")).health()
            print("Both authentication checks passed; no publication performed.")
            return 0
        http=GitHubHTTP(os.environ["GITHUB_REPOSITORY"],os.environ["GH_STATE_TOKEN"])
        if legacy_active(http,os.environ["GITHUB_RUN_ID"]):
            print("Previous-version posting run active; safely deferred.")
            return 0
        stop=[False]
        deadline=time.monotonic()+1200
        def on_signal(*unused):stop[0]=True
        signal.signal(signal.SIGTERM,on_signal)
        signal.signal(signal.SIGINT,on_signal)
        cancelled=lambda:stop[0] or time.monotonic()>=deadline
        owner="run:"+os.environ["GITHUB_RUN_ID"]+":attempt:"+os.environ["GITHUB_RUN_ATTEMPT"]
        coordinator=Coordinator(GitHubStore(http,LEDGER_BRANCH),owner,OwnerStatus(http),cancelled)
        code,result=execute(http,coordinator,args.mode,cancelled)
        print("Posting results: "+json.dumps(result,sort_keys=True))
        return code
    except Exception as exc:
        print("Posting failed: "+diagnostic(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

