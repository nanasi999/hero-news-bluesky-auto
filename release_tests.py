"""Real SDK / parser with all network blocked and only synthetic credentials."""
import sys
sys.dont_write_bytecode=True
import os
from pathlib import Path
ROOT=Path(__file__).resolve().parent
local_dependencies=ROOT.parent/"test-dependencies"
if local_dependencies.exists():
    sys.path.insert(0,str(local_dependencies))
sys.path.insert(0,str(ROOT/"scripts"))
for key in list(os.environ):
    if key.startswith(("GITHUB_","GH_","THREADS_","BLUESKY_","BLOG_")):
        os.environ.pop(key,None)
import base64
import copy
import io
import contextlib
import json
import re
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import feedparser
import yaml
from atproto import models
import live_adapters as adapters
import production
import post_to_bluesky as blue
import post_to_threads as threads
from posting_state import Coordinator, Held
from github_protocol import GitHubStore
from log_safety import SafeFailure

def boundary(event,args):
    if event.startswith("socket.") or event in {"subprocess.Popen","os.system"}:
        raise PermissionError("External execution forbidden in regression tests")
    if event=="open":
        path,mode,flags=args
        if (isinstance(mode,str) and any(x in mode for x in "wax+")) or (flags or 0)&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC):
            if not Path(path).resolve().is_relative_to(ROOT):
                raise PermissionError("Write outside fixture directory")
sys.addaudithook(boundary)


class API:
    def __init__(self):
        self.files={"test-ledger.json":{"lock":None,"posts":{}},
            ".bluesky-posted.json":{"posted":[],"preserve":"fixture"},
            ".threads-posted.json":{"posted":[],"preserve":"fixture"}}
        self.rev={key:0 for key in self.files}
        self.fail_export=False
        self.running=[]
    def __call__(self,method,path,**kw):
        if path.startswith("/git/ref/heads/"):
            return 200,{"ref":"refs/heads/"+path.removeprefix("/git/ref/heads/"),
                        "object":{"type":"commit","sha":"a"*40}}
        if path=="/actions/runs":
            return 200,{"total_count":len(self.running),"workflow_runs":self.running}
        if path=="/contents/scripts/production.py":
            return 404,{}
        key=path.removeprefix("/contents/")
        if method=="GET":
            return 200,{"sha":str(self.rev[key]),"encoding":"base64",
                "content":base64.b64encode(json.dumps(self.files[key]).encode()).decode()}
        if self.fail_export and key!="test-ledger.json":return 500,{}
        value=kw["json"]
        if value["sha"]!=str(self.rev[key]):return 409,{}
        self.rev[key]+=1
        self.files[key]=json.loads(base64.b64decode(value["content"]))
        return 200,{"content":{"sha":str(self.rev[key])}}


class Session:
    def __init__(self):
        self.calls=[];self.published=False;self.lost=False
    def request(self,method,url,**kw):
        self.calls.append((method,url,kw))
        if url.endswith("/me"):data={"id":"123"}
        elif url.endswith("/threads"):data={"id":"456"}
        elif url.endswith("/threads_publish"):
            self.published=True
            if self.lost:raise TimeoutError("FAKE_PRIVATE")
            data={"id":"789"}
        else:data={"status":"PUBLISHED" if self.published else "FINISHED"}
        return types.SimpleNamespace(ok=True,status_code=200,json=lambda:data)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{"BLUESKY_HANDLE":"fixture.invalid",
            "BLUESKY_APP_PASSWORD":"FAKE_PASSWORD","THREADS_USER_ID":"123"},clear=True)
        self.env.start()
        self.temp=tempfile.TemporaryDirectory(dir=ROOT)
        self.cwd=os.getcwd();os.chdir(self.temp.name)
        self.api=API()
        self.c=Coordinator(GitHubStore(self.api,production.LEDGER_BRANCH),"run:1:attempt:1",lambda owner:"completed")
        self.client=Mock()
        self.client.login.return_value=types.SimpleNamespace(did="did:plc:synthetic")
        self.record=None
        def create(data):
            models.ComAtprotoRepoCreateRecord.Data(**data)
            self.assertRegex(data["rkey"],r"^[234567abcdefghij][234567abcdefghijklmnopqrstuvwxyz]{12}$")
            self.record=copy.deepcopy(data["record"])
            self.record_key=data["rkey"]
            return types.SimpleNamespace(uri="at://did:plc:synthetic/app.bsky.feed.post/fixture")
        self.client.com.atproto.repo.create_record.side_effect=create
        def get(params):
            if self.record is None or params["rkey"] != self.record_key:
                from atproto_client.exceptions import BadRequestError
                raise BadRequestError(types.SimpleNamespace(status_code=400,content={"error":"RecordNotFound"}))
            return types.SimpleNamespace(uri="fixture-uri",value=self.record)
        self.client.com.atproto.repo.get_record.side_effect=get
        self.backend=adapters.BlueskyBackend(self.client)
        self.session=Session()
        self.tb=adapters.ThreadsBackend("FAKE_TOKEN","123",self.session,lambda seconds:None)
        self.payload={"prefix":"新着記事: ","title":"合成記事","link":"https://example.invalid/article"}
    def tearDown(self):
        os.chdir(self.cwd)
        self.temp.cleanup();self.env.stop()
    def run_post(self,mode="auto",resolve=lambda:"FAKE_TOKEN"):
        feed=feedparser.parse(b"<rss><channel><item><guid>fixture</guid><title>Title</title><link>https://example.invalid/article</link></item></channel></rss>")
        with patch.object(production,"BlueskyBackend",return_value=self.backend),patch.object(production,"ThreadsBackend",return_value=self.tb),patch.object(production.tokens,"require_seed",return_value="FAKE_SEED"),patch.object(production.tokens,"current_token",side_effect=lambda seed:resolve()),patch.object(feedparser,"parse",return_value=feed),patch.object(blue,"fetch_homepage_entries",return_value=[]),patch.object(threads,"fetch_homepage_entries",return_value=[]),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            return production.execute(self.api,self.c,mode,lambda:False)

    def test_sdk_record_and_utf8_facets(self):
        record=self.backend.prepare(self.payload)
        models.AppBskyFeedPost.Record(**record)
        facet=record["facets"][0]
        raw=record["text"].encode()
        self.assertEqual(raw[facet["index"]["byteStart"]:facet["index"]["byteEnd"]].decode(),"記事を読む")
        self.assertEqual(facet["features"][0]["uri"],self.payload["link"])

    def test_normal_both_and_second_run_no_duplicates(self):
        self.assertEqual(self.run_post()[0],0)
        self.assertEqual(self.run_post()[0],0)
        self.assertEqual(self.client.com.atproto.repo.create_record.call_count,1)
        self.assertEqual(sum(url.endswith("/threads_publish") for _,url,_ in self.session.calls),1)
        self.assertIn("fixture",self.api.files[".bluesky-posted.json"]["posted"])
        self.assertIn("fixture",self.api.files[".threads-posted.json"]["posted"])

    def test_stale_read_after_intent_does_not_lose_row(self):
        original=self.api
        stale=[]
        def lagging(method,path,**kw):
            if method=="GET" and path=="/contents/test-ledger.json" and stale:
                return stale.pop(0)
            before=original("GET",path) if method=="PUT" else None
            result=original(method,path,**kw)
            if method=="PUT" and path=="/contents/test-ledger.json":
                if any(r["stage"]=="intent" for r in original.files["test-ledger.json"]["posts"].values()):
                    stale.append(before)
            return result
        self.c.store.call=lagging
        self.c.store.sleep=lambda seconds:None
        self.assertEqual(self.run_post()[0],0)
        self.assertEqual(self.run_post()[0],0)
        self.assertEqual(self.client.com.atproto.repo.create_record.call_count,1)
        self.assertEqual(sum(url.endswith("/threads_publish") for _,url,_ in self.session.calls),1)

    def test_checkpoint_response_lost_is_reconciled(self):
        original=self.api
        lost=[False]
        def transport(method,path,**kw):
            result=original(method,path,**kw)
            if method=="PUT" and path=="/contents/test-ledger.json" and not lost[0]:
                lost[0]=True
                raise SafeFailure("GitHub request failed")
            return result
        self.c.store.call=transport
        self.c.store.sleep=lambda seconds:None
        self.assertEqual(self.run_post()[0],0)

    def test_checkpoint_transient_conflict_same_revision_retries(self):
        original=self.api
        conflict=[True]
        def transport(method,path,**kw):
            if method=="PUT" and conflict[0]:
                conflict[0]=False
                return 409,{}
            return original(method,path,**kw)
        self.c.store.call=transport
        self.c.store.sleep=lambda seconds:None
        self.assertEqual(self.run_post()[0],0)

    def test_true_checkpoint_conflict_never_overwrites_other_owner(self):
        self.assertTrue(self.c.acquire())
        revision,body=self.c.store.read()
        body["posts"]["fixture"]={"stage":"intent"}
        self.api.files["test-ledger.json"]["lock"]="run:2:attempt:1"
        self.api.rev["test-ledger.json"]+=1
        self.assertFalse(self.c.store.cas(revision,body))
        self.assertEqual(self.api.files["test-ledger.json"]["lock"],"run:2:attempt:1")
        self.assertEqual(self.api.files["test-ledger.json"]["posts"],{})

    def test_permanent_stale_read_never_falls_back_to_cached_lock(self):
        self.c.store.sleep=lambda seconds:None
        self.assertTrue(self.c.acquire())
        stale=self.api("GET","/contents/test-ledger.json")
        self.c.change(lambda body:body["posts"].update({"fixture":{"stage":"intent"}}))
        original=self.api
        transport=Mock(side_effect=lambda method,path,**kw:original(method,path,**kw) if path.startswith("/git/ref/") else stale)
        self.c.store.call=transport
        with self.assertRaises(SafeFailure):self.c.before_send()
        self.assertEqual(transport.call_count,9)
        self.client.com.atproto.repo.create_record.assert_not_called()
        self.assertEqual(self.session.calls,[])

    def test_stale_prepared_and_publishing_reads_do_not_repeat_send(self):
        original=self.api
        stale=[]
        def lagging(method,path,**kw):
            if method=="GET" and path=="/contents/test-ledger.json" and stale:
                return stale.pop(0)
            before=original("GET",path) if method=="PUT" else None
            result=original(method,path,**kw)
            if method=="PUT" and path=="/contents/test-ledger.json":
                if any(r["stage"] in {"prepared","publishing","confirmed"} for r in original.files["test-ledger.json"]["posts"].values()):
                    stale.append(before)
            return result
        self.c.store.call=lagging
        self.c.store.sleep=lambda seconds:None
        self.assertEqual(self.run_post()[0],0)
        self.assertEqual(self.run_post()[0],0)
        self.assertEqual(sum(url.endswith("/threads") for _,url,_ in self.session.calls),1)
        self.assertEqual(sum(url.endswith("/threads_publish") for _,url,_ in self.session.calls),1)

    def test_persistent_branch_cache_lag_releases_using_live_tip(self):
        self.c.store.sleep=lambda seconds:None
        self.assertTrue(self.c.acquire())
        stale=self.api("GET","/contents/test-ledger.json")
        self.c.change(lambda body:body["posts"].update({"fixture":{"stage":"confirmed"}}))
        original=self.api
        calls=[]
        def lagging(method,path,**kw):
            calls.append((method,path,kw))
            if method=="GET" and path=="/contents/test-ledger.json" and kw.get("params",{}).get("ref")==production.LEDGER_BRANCH:
                return stale
            return original(method,path,**kw)
        self.c.store.call=lagging
        self.c.release()
        self.assertIsNone(self.api.files["test-ledger.json"]["lock"])
        self.assertEqual(self.api.files["test-ledger.json"]["posts"]["fixture"]["stage"],"confirmed")
        self.assertTrue(any(kw.get("params",{}).get("ref")=="a"*40 for _,_,kw in calls))
        self.assertEqual(self.session.calls,[])

    def test_pinned_tip_with_other_owner_never_authorizes_send(self):
        self.assertTrue(self.c.acquire())
        stale=self.api("GET","/contents/test-ledger.json")
        self.c.change(lambda body:body.update(marker=True))
        original=self.api
        self.api.files["test-ledger.json"]["lock"]="run:2:attempt:1"
        self.api.rev["test-ledger.json"]+=1
        def lagging(method,path,**kw):
            if path=="/contents/test-ledger.json" and kw.get("params",{}).get("ref")==production.LEDGER_BRANCH:
                return stale
            return original(method,path,**kw)
        self.c.store.call=lagging
        with self.assertRaises(Held):self.c.before_send()
        self.assertEqual(self.api.files["test-ledger.json"]["lock"],"run:2:attempt:1")

    def test_pinned_tip_failure_does_not_use_cached_ownership(self):
        for data in [{},{"ref":"refs/heads/wrong","object":{"type":"commit","sha":"a"*40}},
                     {"ref":"refs/heads/"+production.LEDGER_BRANCH,"object":{"type":"blob","sha":"a"*40}}]:
            self.c.store.call=Mock(return_value=(200,data))
            with self.assertRaises(SafeFailure):self.c.store._read_tip()
            self.assertEqual(self.c.store.call.call_count,1)

    def test_checkpoint_conflict_retry_is_bounded(self):
        self.c.store.sleep=lambda seconds:None
        original=self.api
        writes=[]
        def conflict(method,path,**kw):
            if method=="PUT":
                writes.append(kw)
                return 409,{}
            return original(method,path,**kw)
        self.c.store.call=conflict
        self.assertEqual(self.run_post()[0],1)
        self.assertEqual(len(writes),3)
        self.assertEqual(self.session.calls,[])

    def test_github_get_transient_timeout_and_html_error_recover(self):
        session=Mock()
        bad=types.SimpleNamespace(status_code=502,json=Mock(side_effect=ValueError("FAKE_PRIVATE")))
        good=types.SimpleNamespace(status_code=200,json=lambda:{"ok":True})
        session.request.side_effect=[adapters.requests.Timeout("FAKE_PRIVATE"),bad,good]
        sleeps=[]
        http=adapters.GitHubHTTP("fixture/repo","FAKE_TOKEN",session,sleeps.append)
        self.assertEqual(http("GET","/contents/test-ledger.json"),(200,{"ok":True}))
        self.assertEqual(sleeps,[1,2])

    def test_github_get_auth_failure_is_not_retried(self):
        for status in [401,403,404]:
            session=Mock()
            session.request.return_value=types.SimpleNamespace(status_code=status,json=lambda:{})
            http=adapters.GitHubHTTP("fixture/repo","FAKE_TOKEN",session,lambda seconds:self.fail("unexpected retry"))
            self.assertEqual(http("GET","/contents/test-ledger.json")[0],status)
            self.assertEqual(session.request.call_count,1)

    def test_github_get_invalid_json_recovers_with_bounded_read_retry(self):
        session=Mock()
        session.request.side_effect=[
            types.SimpleNamespace(status_code=200,json=Mock(side_effect=ValueError("FAKE_PRIVATE"))),
            types.SimpleNamespace(status_code=200,json=lambda:{"ok":True})]
        http=adapters.GitHubHTTP("fixture/repo","FAKE_TOKEN",session,lambda seconds:None)
        self.assertEqual(http("GET","/contents/test-ledger.json"),(200,{"ok":True}))
        self.assertEqual(session.request.call_count,2)

    def test_github_put_is_not_blindly_retried_by_transport(self):
        session=Mock()
        session.request.side_effect=adapters.requests.Timeout("FAKE_PRIVATE")
        http=adapters.GitHubHTTP("fixture/repo","FAKE_TOKEN",session,lambda seconds:None)
        with self.assertRaises(SafeFailure) as error:http("PUT","/contents/test-ledger.json")
        self.assertNotIn("FAKE",str(error.exception))
        self.assertEqual(session.request.call_count,1)

    def test_github_get_permanent_transport_failure_is_bounded(self):
        session=Mock()
        session.request.side_effect=adapters.requests.ConnectionError("FAKE_PRIVATE")
        http=adapters.GitHubHTTP("fixture/repo","FAKE_TOKEN",session,lambda seconds:None)
        with self.assertRaises(SafeFailure):http("GET","/contents/test-ledger.json")
        self.assertEqual(session.request.call_count,3)

    def test_source_transient_failure_retries_without_sns_calls(self):
        from source_entries import collect
        parser=Mock(side_effect=ValueError("FAKE_PRIVATE"))
        homepage=Mock(side_effect=[OSError("FAKE_PRIVATE"),[{"link":"https://example.invalid/article"}]])
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            rows=collect(parser,homepage,"https://example.invalid/feed",sleep=lambda seconds:None)
        self.assertEqual(len(rows),1)
        self.assertEqual(homepage.call_count,2)
        self.assertNotIn("FAKE",output.getvalue())
        self.assertEqual(self.session.calls,[])

    def test_source_total_failure_remains_failure_after_three_attempts(self):
        from source_entries import collect
        parser=Mock(side_effect=ValueError("FAKE_PRIVATE"))
        homepage=Mock(side_effect=OSError("FAKE_PRIVATE"))
        with contextlib.redirect_stdout(io.StringIO()),self.assertRaises(SafeFailure):
            collect(parser,homepage,"https://example.invalid/feed",sleep=lambda seconds:None)
        self.assertEqual(parser.call_count,3)
        self.assertEqual(homepage.call_count,3)

    def test_safe_diagnostic_identifies_class_without_exception_values(self):
        from log_safety import diagnostic
        for exc in [KeyError("FAKE_PRIVATE"),ValueError("FAKE_PRIVATE"),Exception("FAKE_PRIVATE")]:
            self.assertNotIn("FAKE",diagnostic(exc))
        self.assertIn("KeyError",diagnostic(KeyError("FAKE_PRIVATE")))

    def test_bluesky_response_lost_matches_saved_record(self):
        original=self.client.com.atproto.repo.create_record.side_effect
        def lost(data):original(data);raise TimeoutError("FAKE_PRIVATE")
        self.client.com.atproto.repo.create_record.side_effect=lost
        self.assertEqual(self.run_post()[0],1)
        self.assertEqual(self.run_post()[0],0)
        self.assertEqual(self.client.com.atproto.repo.create_record.call_count,1)

    def test_threads_response_lost_is_not_republished(self):
        self.session.lost=True
        self.assertEqual(self.run_post()[0],1)
        self.assertEqual(self.run_post()[0],0)
        self.assertEqual(sum(url.endswith("/threads_publish") for _,url,_ in self.session.calls),1)

    def test_token_failure_only_stops_threads(self):
        def fail():raise ValueError("FAKE_PRIVATE")
        code,result=self.run_post(resolve=fail)
        self.assertEqual(code,1);self.assertEqual(result["bluesky"],"success")
        self.assertEqual(self.session.calls,[])

    def test_manual_only_runs_threads(self):
        self.assertEqual(self.run_post("threads")[0],0)
        self.client.com.atproto.repo.create_record.assert_not_called()

    def test_export_failure_recovers_confirmed_rows(self):
        self.api.fail_export=True
        self.assertEqual(self.run_post()[0],1)
        self.api.fail_export=False
        self.assertEqual(self.run_post()[0],0)
        self.assertEqual(self.client.com.atproto.repo.create_record.call_count,1)

    def test_export_merges_remote_and_preserves_metadata(self):
        production.hydrate(self.api)
        Path(".bluesky-posted.json").write_text(json.dumps({"posted":["local"]}))
        self.api.files[".bluesky-posted.json"]["posted"]=["remote"]
        production.export_state(self.api)
        self.assertEqual(self.api.files[".bluesky-posted.json"],{"posted":["local","remote"],"preserve":"fixture"})

    def export_fixture(self):
        production.hydrate(self.api)
        Path(".bluesky-posted.json").write_text(json.dumps({"posted":["local"]}))

    def test_export_lost_response_reads_back_without_second_write(self):
        for failure in (500, "timeout"):
            with self.subTest(failure=failure):
                self.api=API();self.export_fixture();writes=[]
                def call(method,path,**kw):
                    result=self.api(method,path,**kw)
                    if method == "PUT":
                        writes.append(path)
                        if failure == "timeout":raise TimeoutError("FAKE_PRIVATE")
                        return failure,{}
                    return result
                production.export_state(call,sleep=lambda _:None)
                self.assertEqual(len(writes),1)
                self.assertEqual(self.api.files[".bluesky-posted.json"]["posted"],["local"])

    def test_export_transient_failure_retries_with_remote_merge(self):
        self.export_fixture();writes=[];waits=[]
        def call(method,path,**kw):
            if method == "PUT":
                writes.append(path)
                if len(writes) == 1:
                    self.api.files[".bluesky-posted.json"]["posted"]=["concurrent"]
                    self.api.rev[".bluesky-posted.json"]+=1
                    return 503,{}
            return self.api(method,path,**kw)
        production.export_state(call,sleep=waits.append)
        self.assertEqual(len(writes),2);self.assertEqual(waits,[1])
        self.assertEqual(self.api.files[".bluesky-posted.json"],{"posted":["concurrent","local"],"preserve":"fixture"})

    def test_export_stale_branch_read_recovers_at_pinned_tip(self):
        self.export_fixture();path="/contents/.bluesky-posted.json"
        stale=self.api("GET",path)
        self.api.files[".bluesky-posted.json"]["posted"]=["concurrent"]
        self.api.rev[".bluesky-posted.json"]+=1
        writes=[]
        def call(method,url,**kw):
            if method == "GET" and url == path and kw["params"]["ref"] == "main":return stale
            if method == "PUT":writes.append(url)
            return self.api(method,url,**kw)
        production.export_state(call,sleep=lambda _:None)
        self.assertEqual(len(writes),2)
        self.assertEqual(self.api.files[".bluesky-posted.json"]["posted"],["concurrent","local"])

    def test_export_auth_failure_never_retries(self):
        for status in (401,403):
            self.export_fixture();writes=[]
            def call(method,path,**kw):
                if method == "PUT":writes.append(path);return status,{}
                return self.api(method,path,**kw)
            with self.assertRaises(SafeFailure):production.export_state(call,sleep=lambda _:self.fail("retry"))
            self.assertEqual(len(writes),1)

    def test_export_persistent_failure_is_bounded(self):
        self.export_fixture();writes=[];waits=[]
        def call(method,path,**kw):
            if method == "PUT":writes.append(path);return 500,{}
            return self.api(method,path,**kw)
        with self.assertRaises(SafeFailure):production.export_state(call,sleep=waits.append)
        self.assertEqual(len(writes),3);self.assertEqual(waits,[1,2])

    def test_export_unverifiable_tip_does_not_replay_write(self):
        self.export_fixture();writes=[]
        def call(method,path,**kw):
            if method == "PUT":writes.append(path);return 500,{}
            if path.startswith("/git/ref/"):return 200,{"object":{"type":"commit","sha":"a"*40}}
            return self.api(method,path,**kw)
        with self.assertRaises(SafeFailure):production.export_state(call,sleep=lambda _:self.fail("retry"))
        self.assertEqual(len(writes),1)

    def test_threads_headers_and_no_secret_query(self):
        self.tb.health();container=self.tb.create_threads({"text":"fixture"})
        self.assertEqual(self.tb.threads_status(container),"FINISHED")
        self.tb.publish_threads(container)
        for method,url,kw in self.session.calls:
            self.assertNotIn("FAKE",url)
            self.assertEqual(kw["headers"]["Authorization"],"Bearer FAKE_TOKEN")
            self.assertFalse(kw["allow_redirects"])
            self.assertEqual(kw["timeout"],(10,30))

    def test_api_errors_do_not_disclose_response(self):
        self.session.request=Mock(side_effect=OSError("FAKE_PRIVATE"))
        with self.assertRaises(SafeFailure) as error:self.tb.health()
        self.assertNotIn("FAKE",str(error.exception))

    def test_github_http_contract(self):
        session=Mock()
        session.request.return_value=types.SimpleNamespace(status_code=200,json=lambda:{"ok":True})
        http=adapters.GitHubHTTP("fixture/repo","FAKE_TOKEN",session)
        self.assertEqual(http("GET","/actions/runs")[0],200)
        args=session.request.call_args
        self.assertFalse(args.kwargs["allow_redirects"])
        self.assertEqual(args.kwargs["timeout"],(10,30))

    def test_bluesky_safe_error_codes(self):
        exc=Exception("FAKE_PRIVATE")
        exc.response=types.SimpleNamespace(status_code=400,content={"error":"RecordNotFound","message":"FAKE_PRIVATE"})
        self.assertEqual(adapters.bluesky_failure(exc),"HTTP 400 RecordNotFound")
        exc.response.content["error"]="FAKE_PRIVATE"
        self.assertEqual(adapters.bluesky_failure(exc),"HTTP 400 unclassified")

    def test_tid_roundtrip_and_stability(self):
        from atproto_client.models.string_formats import validate_tid
        payload=self.backend.prepare(self.payload)
        key=self.backend.new_bluesky_key("a"*64,payload)
        self.assertEqual(validate_tid(key,None),key)
        self.assertEqual(key,self.backend.new_bluesky_key("a"*64,payload))
        value=0
        for c in key:value=value*32+"234567abcdefghijklmnopqrstuvwxyz".index(c)
        from datetime import datetime
        actual=datetime.fromisoformat(payload["createdAt"].replace("Z","+00:00"))
        self.assertAlmostEqual((value>>10)/1000000,actual.timestamp(),places=5)

    def test_legacy_missing_hash_migrates_once(self):
        import hashlib
        self.c.acquire()
        key=hashlib.sha256(b"bluesky\nfixture").hexdigest()
        payload=self.backend.prepare(self.payload)
        self.api.files["test-ledger.json"]["posts"][key]={"platform":"bluesky","identifiers":["fixture"],"payload":payload,"stage":"sending"}
        row=self.c.post("bluesky",["fixture"],payload,self.backend,set())
        self.assertEqual(row["stage"],"confirmed")
        self.assertEqual(len(row["record_key"]),13)
        self.c.post("bluesky",["fixture"],payload,self.backend,set())
        self.assertEqual(self.client.com.atproto.repo.create_record.call_count,1)

    def test_lookup_transport_failure_never_sends(self):
        self.client.com.atproto.repo.get_record.side_effect=TimeoutError("FAKE_PRIVATE")
        self.assertEqual(self.run_post()[0],1)
        self.client.com.atproto.repo.create_record.assert_not_called()

    def test_legacy_existing_record_is_confirmed_without_resend(self):
        import hashlib
        self.c.acquire()
        key=hashlib.sha256(b"bluesky\nfixture").hexdigest()
        payload=self.backend.prepare(self.payload)
        self.api.files["test-ledger.json"]["posts"][key]={"platform":"bluesky","identifiers":["fixture"],"payload":payload,"stage":"sending"}
        self.record,self.record_key=payload,key
        self.assertEqual(self.c.post("bluesky",["fixture"],payload,self.backend,set())["stage"],"confirmed")
        self.client.com.atproto.repo.create_record.assert_not_called()

    def test_new_tid_unknown_result_is_not_blindly_retried(self):
        self.client.com.atproto.repo.create_record.side_effect=TimeoutError("FAKE_PRIVATE")
        self.assertEqual(self.run_post()[0],1)
        self.assertEqual(self.run_post()[0],1)
        self.assertEqual(self.client.com.atproto.repo.create_record.call_count,1)

    def test_journal_tid_collision_prevents_send(self):
        import hashlib
        self.c.acquire()
        key=hashlib.sha256(b"bluesky\nfixture").hexdigest()
        payload=self.backend.prepare(self.payload)
        tid=self.backend.new_bluesky_key(key,payload)
        self.api.files["test-ledger.json"]["posts"]["other"]={"platform":"bluesky","identifiers":["other"],"payload":payload,"stage":"confirmed","record_key":tid}
        with self.assertRaises(Held):self.c.post("bluesky",["fixture"],payload,self.backend,set())
        self.client.com.atproto.repo.create_record.assert_not_called()

    def test_remote_key_collision_never_sends(self):
        self.client.com.atproto.repo.get_record.side_effect=lambda params:types.SimpleNamespace(uri="occupied",value={"text":"other"})
        self.assertEqual(self.run_post()[0],1)
        self.client.com.atproto.repo.create_record.assert_not_called()

    def test_legacy_run_defers(self):
        self.api.running=[{"id":2,"head_sha":"old",".github":"unused","path":".github/workflows/post-to-bluesky.yml"}]
        self.assertTrue(production.legacy_active(self.api,1))
        self.api.running=[]
        self.assertFalse(production.legacy_active(self.api,1))

    def test_missing_state_fails_closed(self):
        with self.assertRaises(SafeFailure):production.read_state(lambda *a,**k:(404,{}),".threads-posted.json")

    def nonposting_fixture(self, run_id=32219049176):
        number,sha=production.NONPOSTING_RUNS[run_id]
        run={"id":run_id,"run_number":number,"head_sha":sha,"run_attempt":1,
             "status":"queued","event":"repository_dispatch",
             "path":".github/workflows/post-to-bluesky.yml"}
        def http(method,path,**kw):
            self.assertEqual(method,"GET")
            if path=="/actions/runs":
                rows=[run] if kw["params"]["status"]==run["status"] else []
                return 200,{"total_count":len(rows),"workflow_runs":rows}
            if path.endswith(".yml"):
                self.assertEqual(kw["params"]["ref"],sha)
                return 200,{"sha":production.NONPOSTING_WORKFLOW_BLOB}
            if path.endswith("/jobs"):
                self.assertEqual(kw["params"]["filter"],"all")
                return 200,{"total_count":0,"jobs":[]}
            return 404,{}
        return run,http

    def test_all_three_false_job_conditions_allow_migration(self):
        for run_id in production.NONPOSTING_RUNS:
            with self.subTest(run_id=run_id):
                run,http=self.nonposting_fixture(run_id)
                self.assertFalse(production.legacy_active(http,1))

    def test_nonposting_metadata_changes_block_migration(self):
        for key,value in {"id":999,"run_number":18455,"head_sha":"different",
                          "run_attempt":2,"status":"in_progress",
                          "event":"workflow_dispatch"}.items():
            with self.subTest(field=key):
                run,http=self.nonposting_fixture()
                run[key]=value
                self.assertTrue(production.legacy_active(http,1))

    def test_nonposting_proof_api_failure_or_jobs_blocks(self):
        for suffix,result in [(".yml",(403,{})),(".yml",(200,{"sha":"changed"})),
                              ("/jobs",(500,{})),("/jobs",(200,{})),
                              ("/jobs",(200,{"total_count":1,"jobs":[{}]})),
                              ("/jobs",(200,{"total_count":0,"jobs":[{}]}))]:
            with self.subTest(suffix=suffix,result=result):
                run,http=self.nonposting_fixture()
                def failing(method,path,**kw):
                    return result if path.endswith(suffix) else http(method,path,**kw)
                self.assertTrue(production.legacy_active(failing,1))

    def test_job_condition_truth_table_independent(self):
        for number in range(18420,18461):
            expression=("repository_dispatch" != "repository_dispatch" or
                        str(number).endswith("0") or str(number).endswith("5"))
            self.assertEqual(expression, number % 5 == 0)
        for number,sha in production.NONPOSTING_RUNS.values():
            self.assertNotEqual(number % 5,0)

    def test_production_guard(self):
        with patch.object(sys,"argv",["production"]),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(production.main(),1)

    def test_workflow_guards_and_schedule(self):
        path=ROOT/".github"/"workflows"
        for name in ["post-to-bluesky.yml","post-to-threads.yml","refresh-threads-token.yml"]:
            obj=yaml.safe_load((path/name).read_text())
            for job in obj["jobs"].values():
                self.assertIn("refs/heads/main",job["if"])
                self.assertLessEqual(job["timeout-minutes"],30)
        auto=(path/"post-to-bluesky.yml").read_text()
        self.assertIn('17,47 * * * *',auto)
        self.assertIn("actions: read",auto)
        self.assertNotIn("600",auto)
        self.assertNotIn("secrets.",(path/"verify-auto-post.yml").read_text())
        refresh=(path/"refresh-threads-token.yml").read_text()
        self.assertNotIn("push:",refresh.split("permissions:")[0])

    def test_network_is_blocked(self):
        import socket
        with self.assertRaises(PermissionError):socket.socket()


class JapaneseTagTests(unittest.TestCase):
    def test_same_title_conditions(self):
        from bluesky_text import article_tags
        for title, tag in [("仮面ライダー", "仮面ライダー"), ("kAmEn RiDeR", "仮面ライダー"), ("戦隊", "スーパー戦隊"), ("Super Sentai", "スーパー戦隊"), ("ウルトラセブン", "ウルトラマン"), ("Ultraman", "ウルトラマン"), ("ゴジラ", "ゴジラ"), ("Godzilla", "ゴジラ"), ("ガメラ", "ガメラ"), ("Gamera", "ガメラ")]:
            self.assertEqual(article_tags(title), ["特撮", tag])
        for title in ["ライダー", "スパイダーマン", "Batman", "特撮", "Unknown", "Ultramans"]:
            self.assertEqual(article_tags(title), [])
        self.assertEqual(article_tags("新作Ultramanの話"), ["特撮", "ウルトラマン"])
        self.assertEqual(article_tags("Kamen　Rider"), ["特撮", "仮面ライダー"])
        self.assertEqual(article_tags("仮面ライダー ウルトラマン 戦隊 ゴジラ ガメラ"), ["特撮", "ウルトラマン", "仮面ライダー", "スーパー戦隊"])

    def test_layout_and_utf8_facets(self):
        from bluesky_text import build_post
        title = "【仮面ライダー】この怪人ってなんか怪獣みたいだよね"
        text = build_post(title, "https://example.invalid/article", "新着記事: ")
        self.assertEqual(text.build_text(), "新着記事: " + title + "\n#特撮 #仮面ライダー\n\n記事を読む")
        raw = text.build_text().encode("utf-8")
        facets = text.build_facets()
        self.assertEqual(len(facets), 3)
        for facet, expected in zip(facets, ["#特撮", "#仮面ライダー", "記事を読む"]):
            self.assertEqual(raw[facet.index.byte_start:facet.index.byte_end].decode(), expected)
        self.assertEqual(facets[0].features[0].tag, "特撮")
        self.assertEqual(facets[-1].features[0].uri, "https://example.invalid/article")

    def test_length_and_production_path(self):
        from bluesky_text import build_post
        for title in ["Batman", "あ" * 500 + "仮面ライダー", "😀" * 400 + "ゴジラ"]:
            payload = {"title":title,"link":"https://example.invalid/article","prefix":"新着記事: "}
            backend = adapters.BlueskyBackend()
            backend.did = "did:plc:synthetic"
            record = backend.prepare(payload)
            self.assertLessEqual(len(record["text"]), 300)
            self.assertEqual(record["text"], build_post(title, payload["link"], payload["prefix"]).build_text())
            self.assertEqual(blue.build_post(title,payload["link"]).build_text(), record["text"])
        self.assertNotIn("#", build_post("Batman", "https://example.invalid/article", "新着記事: ").build_text())


if __name__=="__main__":
    unittest.main(verbosity=2)
