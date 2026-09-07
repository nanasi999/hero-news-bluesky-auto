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
            self.record=copy.deepcopy(data["record"])
            return types.SimpleNamespace(uri="at://did:plc:synthetic/app.bsky.feed.post/fixture")
        self.client.com.atproto.repo.create_record.side_effect=create
        self.client.com.atproto.repo.get_record.side_effect=lambda params:types.SimpleNamespace(uri="fixture-uri",value=self.record)
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
        self.assertEqual(raw[facet["index"]["byteStart"]:facet["index"]["byteEnd"]].decode(),"読む")
        self.assertEqual(facet["features"][0]["uri"],self.payload["link"])

    def test_normal_both_and_second_run_no_duplicates(self):
        self.assertEqual(self.run_post()[0],0)
        self.assertEqual(self.run_post()[0],0)
        self.assertEqual(self.client.com.atproto.repo.create_record.call_count,1)
        self.assertEqual(sum(url.endswith("/threads_publish") for _,url,_ in self.session.calls),1)
        self.assertIn("fixture",self.api.files[".bluesky-posted.json"]["posted"])
        self.assertIn("fixture",self.api.files[".threads-posted.json"]["posted"])

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


if __name__=="__main__":
    unittest.main(verbosity=2)
