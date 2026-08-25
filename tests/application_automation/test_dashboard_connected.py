from __future__ import annotations

import importlib.util
import json
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from application_automation.api import create_app
from application_automation.store import apply_migrations, connect


ROOT = Path(__file__).resolve().parents[2]
BUILD_PATH = ROOT / "tools" / "build_job_dashboard.py"
DASHBOARD_PATH = ROOT / "dashboard.html"
SERVICE_PATH = ROOT / "tools" / "apply_service.py"
CAPABILITIES_PATH = ROOT / "config" / "provider-capabilities.json"


def builder():
    spec = importlib.util.spec_from_file_location("dashboard_builder", BUILD_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def embedded_data(page: str) -> dict:
    start = page.index('<script id="job-data" type="application/json">') + len(
        '<script id="job-data" type="application/json">'
    )
    end = page.index("</script>", start)
    return json.loads(page[start:end])


def test_generated_dashboard_matches_builder_and_safely_embeds_json() -> None:
    module = builder()
    fixture = {
        "generated_at": "2026-07-15T00:00:00Z",
        "roles": [{"id": "fixture-role", "status": "materials_ready"}],
        "note": "</ScRiPt><script>not executable</script> & 서울 🚀\u2028\u2029",
    }

    page = module.render_dashboard(fixture)
    assert "\\u003c/ScRiPt>" in page
    assert "</ScRiPt>" not in page
    assert "\\u0026" in page
    assert "서울 🚀" in page
    assert embedded_data(page) == fixture
    collision_fixture = {**fixture, "generated_at": "__JOB_DATA__", "note": "__JOB_DATA__ __GENERATED_AT__"}
    collision_page = module.render_dashboard(collision_fixture)
    assert embedded_data(collision_page) == collision_fixture
    assert "<footer>As of __JOB_DATA__" in collision_page

    generated = DASHBOARD_PATH.read_text(encoding="utf-8")
    canonical = json.loads(module.DATA_PATH.read_text(encoding="utf-8"))
    assert generated == module.render_dashboard(canonical)


def test_generated_dashboard_javascript_parses() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    page = DASHBOARD_PATH.read_text(encoding="utf-8")
    script = page.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    result = subprocess.run(
        [node, "--check", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
def test_dashboard_runtime_fixture_controls() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    page = DASHBOARD_PATH.read_text(encoding="utf-8")
    script = page.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    harness = r'''
const vm=require("vm");
const source=process.argv[1].replace("renderAll();\nresetOfflineSurface();\nloadConnectedState();","");
const instrumentedSource="let fetchCalls=0,pollSchedules=0,reconnectSchedules=0;\nconst dashboardFetch=fetch;\nfetch=(...args)=>{fetchCalls++;return dashboardFetch(...args)};\n"+source.replace("function schedulePolling() {","function schedulePolling() { pollSchedules++;").replace("function scheduleReconnect() {","function scheduleReconnect() { reconnectSchedules++;");
const role={id:"role",status:"materials_ready",company:"C",title:"T",tier:"precision",apply_url:"https://fixture.local/job"};
const data={generated_at:"safe",roles:[role]};
const snap=(state,kill=false)=>({automation:{fixture_mode:true,kill_switch_active:kill},catalog_revision:"revision",worker:state==="running"?{state,automatic_progress:true,can_queue:true}:state==="manual"?{state,automatic_progress:false,can_queue:true}:{state:"unavailable",automatic_progress:false,can_queue:false},roles:[{role_id:"role",status:"materials_ready"}]});
function runtime({protocol="http:",storage,fetch}) {
  let capturedExport="";
  const nodes=new Map(), node=()=>({classList:{add(){},remove(){}},style:{},handlers:{},addEventListener(type,fn){this.handlers[type]=fn},setAttribute(){},click(){},textContent:"",innerHTML:"",hidden:false});
  const document={getElementById(id){if(id==="job-data")return {textContent:JSON.stringify(data)};if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)},querySelectorAll(){return []},createElement(){return node()}};
  const DashboardURL=function(...args){return new URL(...args)}; DashboardURL.createObjectURL=()=> "blob:fixture"; DashboardURL.revokeObjectURL=()=>{};
  const Blob=class {constructor(parts){capturedExport=parts.join("")}};
  const timers=[], ctx={document,console,URL:DashboardURL,Blob,Intl,CSS:{escape:x=>x},confirm:()=>true,window:{location:{protocol,hostname:"127.0.0.1"}},localStorage:storage,setTimeout(fn){timers.push(fn);return timers.length},clearTimeout(){},fetch};
  vm.runInNewContext(instrumentedSource+`\nrenderAll=()=>{};\nglobalThis.d={loadConnectedState,queueApplication,queueControl,setStatus,get connected(){return isConnected},get warning(){return storageWarning},get mode(){return document.getElementById("modeLabel").textContent},get queue(){return document.getElementById("queueStateLabel").textContent},get reset(){return document.getElementById("resetBtn").handlers.click},get fetchCalls(){return fetchCalls},get pollSchedules(){return pollSchedules},get reconnectSchedules(){return reconnectSchedules}};`,ctx);
  return {ctx,nodes,timers,get exported(){return capturedExport}};
}
(async()=>{
  for(const storage of [
    {getItem(){throw Error("denied")},setItem(){throw Error("denied")},removeItem(){throw Error("denied")}},
    {getItem(){return "{}"},setItem(){throw Error("denied")},removeItem(){throw Error("denied")}},
    {getItem(){return "{bad"},setItem(){},removeItem(){throw Error("denied")}},
  ]) {
    const r=runtime({protocol:"file:",storage,fetch(){return Promise.resolve({ok:false})}});
    r.ctx.d.setStatus("role","applied");
    r.ctx.d.reset();
    if(!r.ctx.d.warning||!r.nodes.get("storageWarning").textContent)throw Error("storage warning");
    await r.ctx.d.loadConnectedState();
    if(r.ctx.d.fetchCalls!==0||r.ctx.d.pollSchedules!==0||r.ctx.d.reconnectSchedules!==0)throw Error("static network or scheduling");
  }
  let responses=[],posts=0,enqueues=0,loseResponse=true,keys=new Map();
  const connected=runtime({storage:{getItem(){return null},setItem(){},removeItem(){}},fetch(_url,opt){
    if(opt?.method==="POST"){posts++;const key=opt.headers["Idempotency-Key"];if(!keys.has(key)){keys.set(key,"command-1");enqueues++;if(loseResponse){loseResponse=false;return Promise.reject(Error("lost response"))}}return Promise.resolve({ok:true,json:async()=>({state:"accepted",id:keys.get(key)})})}
    return Promise.resolve(responses.shift());
  }});
  for(const [state,copy] of [["running","Automatic"],["manual","Manual"],["unavailable","Unavailable"]]) {
    responses=[{ok:true,json:async()=>({csrf_token:"csrf",fixture_mode:true})},{ok:true,json:async()=>snap(state)}];
    await connected.ctx.d.loadConnectedState();
    if(!connected.ctx.d.mode.includes(copy))throw Error("worker "+state);
    if(state==="unavailable"&&connected.ctx.d.queueControl(role)!=="")throw Error("unavailable queue");
  }
  responses=[{ok:true,json:async()=>({csrf_token:"csrf",fixture_mode:true})},{ok:true,json:async()=>snap("manual",true)}];
  await connected.ctx.d.loadConnectedState();
  if(connected.ctx.d.queueControl(role)!==""||connected.ctx.d.queue!=="Queue unavailable")throw Error("kill switch");
  connected.nodes.get("exportBtn").handlers.click();
  if(JSON.parse(connected.exported).provenance.canonical_real_application_record!==false)throw Error("export provenance");
  await connected.ctx.d.queueApplication("role");
  if(posts)throw Error("kill switch POST");
  responses=[{ok:true,json:async()=>({csrf_token:"csrf",fixture_mode:false})},{ok:true,json:async()=>snap("manual")}];
  await connected.ctx.d.loadConnectedState();
  if(connected.ctx.d.connected||connected.ctx.d.queueControl(role)!=="")throw Error("provenance");
  responses=[{ok:true,json:async()=>({csrf_token:"csrf",fixture_mode:true})},{ok:true,json:async()=>snap("manual")}];
  await connected.ctx.d.loadConnectedState();
  await connected.ctx.d.queueApplication("role");
  await connected.ctx.d.queueApplication("role");
  if(posts!==2||enqueues!==1)throw Error("sequential idempotency");
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    assert '<span class="mode-label" id="modeLabel" role="status" aria-live="polite" aria-atomic="true">' in page
    result = subprocess.run([node, "-e", harness, script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_dashboard_queue_replay_keeps_one_fixture_command_after_lost_response(tmp_path: Path) -> None:
    database = connect(tmp_path / "dashboard.sqlite")
    apply_migrations(database)
    application_dir = tmp_path / "applications" / "role"
    application_dir.mkdir(parents=True)
    (application_dir / "job.md").write_text("# Fixture role", encoding="utf-8")
    (application_dir / "resume.md").write_text("fixture resume", encoding="utf-8")
    material = application_dir / "resume.pdf"
    material.write_bytes(b"%PDF-fixture")
    dashboard = tmp_path / "dashboard.html"
    dashboard.write_text("<html></html>", encoding="utf-8")
    source_data = tmp_path / "jobs.json"
    source_data.write_text('{"roles":[]}', encoding="utf-8")
    master_resume = tmp_path / "resume.md"
    master_resume.write_text("fixture resume", encoding="utf-8")
    catalog = {
        "role": {
            "score": 8,
            "location": "Vancouver, BC",
            "posting_active": True,
            "remote": False,
            "remote_country": None,
            "automation_status": "materials_ready",
            "canonical_identity": "fixture:role",
            "application_dir": str(application_dir),
            "material_path": str(material),
            "material_sha256": hashlib.sha256(material.read_bytes()).hexdigest(),
        }
    }
    client = TestClient(
        create_app(
            database,
            bootstrap_token="bootstrap",
            fixture_mode=True,
            catalog=catalog,
            dashboard_path=dashboard,
            source_data_path=source_data,
            master_resume_path=master_resume,
        ),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    )
    try:
        bootstrap = client.post(
            "/app/v1/bootstrap",
            headers={"origin": "http://127.0.0.1", "content-type": "application/x-www-form-urlencoded"},
            content="token=bootstrap",
            follow_redirects=False,
        )
        assert bootstrap.status_code == 303
        csrf_token = client.get("/api/v1/session").json()["csrf_token"]
        headers = {
            "origin": "http://127.0.0.1",
            "x-csrf-token": csrf_token,
            "idempotency-key": "dashboard-queue-role-revision",
        }
        body = {"mode": "batch", "idempotency_key": "dashboard-queue-role-revision"}
        first = client.post("/api/v1/roles/role/commands", headers=headers, json=body)
        assert first.status_code == 200
        replay = client.post("/api/v1/roles/role/commands", headers=headers, json=body)
        assert replay.status_code == 200
        assert replay.json()["id"] == first.json()["id"]
        assert database.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 1
    finally:
        client.close()
        database.close()


def test_dashboard_fails_closed_to_fixture_only_connected_mode() -> None:
    page = DASHBOARD_PATH.read_text(encoding="utf-8")

    assert "const staticMode = window.location.protocol === 'file:';" in page
    assert "function isSupportedLoopbackOrigin()" in page
    assert "window.location.hostname === '127.0.0.1'" in page
    assert page.index("fetch('/api/v1/session'") < page.index("fetch('/api/v1/snapshot'")
    assert "candidate?.automation?.fixture_mode !== true" in page
    assert "snapshot?.automation?.kill_switch_active === false" in page
    assert "typeof candidate.catalog_revision !== 'string'" in page
    assert "const hasQueueAuthority" in page
    assert "snapshot.catalog_revision" in page
    assert "data.generated_at" not in page[page.index("const hasQueueAuthority"):page.index("const safeExternalUrl")]
    assert "body:JSON.stringify({mode:'batch',idempotency_key:key})" in page
    assert "mode:'fill_only'" not in page
    assert "credentials:'same-origin', cache:'no-store'" in page
    assert "Fixture only · nothing is submitted to real providers, and queue results are not evidence of real applications." in page
    assert "Add to fixture queue (does not submit)" in page
    assert "Fixture only · no real application is submitted." in page
    assert "provenance: isConnected" in page
    assert "canonical_real_application_record:false" in page


def test_dashboard_disconnect_is_read_only_and_errors_are_sanitized() -> None:
    page = DASHBOARD_PATH.read_text(encoding="utf-8")

    assert "const disconnected = !staticMode;" in page
    assert "Disconnected · read-only" in page
    assert "Automation and status changes are unavailable." in page
    assert "Reading embedded data only until the service connection recovers. Local state is not modified." in page
    assert "if (!staticMode || isConnected) return;" in page
    assert "safeConnectionMessage" in page
    assert "safeQueueMessage" in page
    assert "error.message" not in page
    assert "body.detail" not in page


def test_dashboard_row_links_are_named_and_accessible() -> None:
    page = DASHBOARD_PATH.read_text(encoding="utf-8")

    assert "material-link" in page
    assert "application-link" in page
    assert "resume-link" not in page
    assert 'aria-label="Open ${esc(role.company)} materials"' in page
    assert 'title="Open materials"' in page
    assert 'aria-label="Open ${esc(role.company)} posting page"' in page
    assert 'title="Open posting page"' in page


def test_service_and_capabilities_default_to_fixture_only_authority() -> None:
    service = SERVICE_PATH.read_text(encoding="utf-8")
    capabilities = json.loads(CAPABILITIES_PATH.read_text(encoding="utf-8"))

    assert 'parser.add_argument("--fixture", action="store_true"' in service
    assert 'and args.fixture is not True:' in service
    assert 'is fixture-only; pass --fixture' in service
    assert capabilities["default"]["allow_live_traffic"] is False
    assert capabilities["default"]["allowed_operations"] == []
    provider = capabilities["providers"]["aside_fixture"]
    assert provider["mode"] == "fixture_only"
    assert provider["allow_live_traffic"] is False
    assert provider["loopback_origin"] == "http://127.0.0.1"
    fixture = provider["fixture"]
    script = ROOT / fixture["script"]
    schema = ROOT / fixture["result_schema_artifact"]
    assert fixture["result_schema_canonicalization"] == "utf-8 exact bytes"
    assert hashlib.sha256(script.read_bytes()).hexdigest() == fixture["script_sha256"]
    assert hashlib.sha256(schema.read_bytes()).hexdigest() == fixture["result_schema_sha256"]