"""Opt-in, synthetic-only smoke on colima-voxquarry; no real model.

Run while the dedicated VM has no live research. Manager startup intentionally
reclaims its labelled orphan resources. Never switch the global Docker context.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent.harness_manager import HarnessManager

SPEC = importlib.util.spec_from_file_location("wenquan_manager_smoke", Path(__file__).with_name("manager.py"))
manager_module = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(manager_module)

FAKE_BROKER = r'''
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json, time
calls = 0
seen_tool = False
mode = 'probe'
submitted = []
native_events = []
native_bridge = None
seen_requests = []
class Handler(BaseHTTPRequestHandler):
 def log_message(self, *args): pass
 def do_GET(self):
  body=json.dumps({"model_calls":calls,"seen_tool":seen_tool}).encode()
  self.send_response(200);self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
 def do_POST(self):
  global calls, seen_tool
  request=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))))
  if self.path=="/wenquan/internal/broker/native" and native_bridge:
   body=json.dumps(native_bridge(request)).encode()
   self.send_response(200);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body);return
  if self.path=="/wenquan/internal/broker/trajectory":
   native_events.append(request)
   body=b'{"saved":true}'
   self.send_response(200);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body);return
  if self.path=="/wenquan/internal/broker/tools/probe":
   seen_tool=True;body=b'{"verified":true}'
   self.send_response(200);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body);return
  if self.path=="/wenquan/internal/broker/tools/submit_answer":
   submitted.append(request.get("arguments",{}).get("answer"))
   body=json.dumps({"accepted":mode=="accepted_answer" or len(submitted)>1}).encode()
   self.send_response(200);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body);return
  if self.path!="/wenquan/internal/broker/model/v1/chat/completions":
   self.send_error(404);return
  calls+=1
  seen_requests.append(request)
  if calls==1 and mode=="probe": time.sleep(8)
  tool=calls <= (2 if mode=="rejected_answer" else 1)
  tool_name="probe" if mode=="probe" else "submit_answer"
  arguments={} if mode=="probe" else {"answer":"synthetic rejected draft" if mode=="rejected_answer" and calls==1 else "synthetic saved answer"}
  if mode in {"native", "continuity"}:
   tool=calls <= (2 if mode=="native" else 1)
   tool_name="read" if calls==2 and mode=="native" else "bash"
   code="from pathlib import Path; import os; assert not Path('/run/voxquarry/capability').exists(); assert not Path('/var/run/docker.sock').exists(); assert sorted(os.listdir('/sys/class/net'))==['lo']; "
   code += "Path('/workspace/persistent.txt').write_text('native-persistent-evidence'); print('isolation-ok')" if mode=="native" else "assert Path('/workspace/persistent.txt').read_text()=='native-persistent-evidence'; print('continuity-ok')"
   arguments={"file_path":"/workspace/persistent.txt"} if tool_name=="read" else {"command":"python -c "+__import__('shlex').quote(code), "description":"Synthetic sandbox and continuity verification"}
  delta={"role":"assistant","tool_calls":[{"index":0,"id":"call_fixture_"+str(calls),"type":"function","function":{"name":tool_name,"arguments":json.dumps(arguments)}}]} if tool else {"role":"assistant","content":"隔离执行验证通过"}
  reason="tool_calls" if tool else "stop"
  if request.get("stream"):
   self.send_response(200);self.send_header("Content-Type","text/event-stream");self.send_header("Connection","close");self.end_headers()
   for entry in [{"id":"fixture","object":"chat.completion.chunk","model":"deepseek-v4.1-flash","choices":[{"index":0,"delta":delta,"finish_reason":None}]}, {"id":"fixture","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":reason}],"usage":{"prompt_tokens":20,"completion_tokens":5,"total_tokens":25}}]:
    self.wfile.write(("data: "+json.dumps(entry,ensure_ascii=False)+"\n\n").encode());self.wfile.flush()
   self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush();self.close_connection=True
  else:
   if tool: delta["tool_calls"][0].pop("index")
   body=json.dumps({"id":"fixture","object":"chat.completion","choices":[{"index":0,"message":delta,"finish_reason":reason}],"usage":{"prompt_tokens":20,"completion_tokens":5,"total_tokens":25}}).encode()
   self.send_response(200);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
ThreadingHTTPServer(("0.0.0.0",9010),Handler).serve_forever()
'''


class DedicatedTestEngine(manager_module.DockerEngine):
    def __init__(self, *, production_guest=False):
        super().__init__()
        # Test-only host adapter. Production manager has no context support.
        self.prefix = (["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"] if production_guest else
                       ["docker", "--host", "unix:///run/voxquarry-docker.sock"])

    def call(self, args, **kwargs):
        if os.environ.get("WENQUAN_SYNTHETIC_DEBUG") == "1" and args[0] == "create" and args[args.index("--name") + 1].endswith("-runner"):
            # Opt-in diagnostics contain only this script's synthetic fixture.
            debug = "from pathlib import Path;exec(compile(Path('/opt/voxquarry/runner.py').read_text().replace('\\\"type\\\": type(exc).__name__', '\\\"type\\\": type(exc).__name__, \\\"synthetic_detail\\\": str(exc)'), '/opt/voxquarry/runner.py', 'exec'))"
            args = args[:-1] + ["--entrypoint", "python", args[-1], "-c", debug]
        return super().call(args, **kwargs)


def wait_until(action, *, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = action()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("condition not reached within the smoke deadline")


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--expected-engine-id", required=True); parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--production-guest", action="store_true", help="Run only inside the authorized dedicated Linux research VM")
    parser.add_argument("--image-tag", default="development")
    parser.add_argument("--manager-image", help="Optional immutable manager image ID for a bounded runtime correction")
    args = parser.parse_args()
    if os.environ.get("WENQUAN_RUN_VM_SMOKE") != "1":
        raise SystemExit("Set WENQUAN_RUN_VM_SMOKE=1 only for the dedicated synthetic VM smoke")
    if os.environ.get("WENQUAN_SYNTHETIC_DEBUG") == "1":
        parse = manager_module.strict_json
        def diagnostic(raw):
            value = parse(raw)
            if isinstance(value, dict) and "error" in value:
                print(json.dumps(value), flush=True)
            return value
        manager_module.strict_json = diagnostic
    engine = DedicatedTestEngine(production_guest=args.production_guest)
    policy = {"expected_engine_id": args.expected_engine_id}
    if args.production_guest:
        if args.image_tag == "development":
            raise SystemExit("Production guest smoke requires an explicit immutable candidate image tag")
        policy.update(expected_engine_name="voxquarry", resource_profile="production-single")
    engine.guard(policy)
    # A smoke may create and clean synthetic resources only on an idle engine.
    if engine.call(["ps", "-aq", "--filter", "label=" + manager_module.LABEL]).strip():
        raise SystemExit("Live or retained research resources exist; smoke refused")
    prefix = "voxquarry"
    images = {kind + "_image": engine.call(["image", "inspect", "--format", "{{.Id}}", f"{prefix}-{kind}:{args.image_tag}"]).decode().strip() for kind in ("analysis", "runner", "relay", "manager")}
    if args.manager_image:
        if not manager_module.IMAGE.fullmatch(args.manager_image):
            raise SystemExit("An immutable manager image ID is required")
        images["manager_image"] = args.manager_image
    fake = "voxquarry-synthetic-broker"
    broker_server = broker_scope = None
    if args.production_guest:
        # The production guest probe runs on the VM host network. The native
        # SDK needs its filesystem/spawn bridge even before the first model call.
        broker_scope = {}
        exec(FAKE_BROKER.rsplit("ThreadingHTTPServer(", 1)[0], broker_scope)
        broker_ip = engine.call(["network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"]).decode().strip()
        broker_server = broker_scope["ThreadingHTTPServer"]((broker_ip, 9010), broker_scope["Handler"])
        threading.Thread(target=broker_server.serve_forever, daemon=True).start()
    else:
        engine.call(["rm", "-f", fake], check=False)
        broker_args = manager_module.Manager.container(fake, memory="128m", cpus="0.5", pids="16", network="bridge")
        # No manager label: the fixture is independent of manager recovery cleanup.
        index = broker_args.index("--label"); del broker_args[index:index + 2]
        engine.call(broker_args + ["--entrypoint", "python", images["analysis_image"], "-u", "-c", FAKE_BROKER]); engine.call(["start", fake])
        broker_ip = engine.call(["inspect", "--format", "{{(index .NetworkSettings.Networks \"bridge\").IPAddress}}", fake]).decode().strip()
    config = {**images, **policy, "broker_base_url": f"http://{broker_ip}:9010/wenquan/internal/broker"}
    manager = server = None
    evidence = {"engine_id": args.expected_engine_id, "images": images}
    try:
        manager = manager_module.Manager(config, engine=engine)
        token = secrets.token_urlsafe(48)
        server = manager_module.make_server(manager, token, ("127.0.0.1", 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        client = HarnessManager(f"http://127.0.0.1:{server.server_address[1]}", token)
        if broker_scope is not None:
            broker_scope["native_bridge"] = lambda payload: client.native_io(attempt, payload)
        attempt = "smoke_analysis"
        started = client.start({"attempt_id": attempt, "run_id": "smoke_run", "mode": "analysis", "deadline_seconds": 180})
        assert started["status"] == "running"
        wait_until(lambda: client.status(attempt)["ready"])
        shard = b'{"rating":5,"body":"synthetic"}\n' * 600000
        assert len(shard) > 16 * 1024 * 1024
        upload = client.put_input(attempt, "reviews.jsonl", shard)
        assert upload["sha256"] == hashlib.sha256(shard).hexdigest()
        code = '''import os, socket, json, duckdb
def reachable(ip,port):
 s=socket.socket();s.settimeout(.2)
 try:return s.connect_ex((ip,port))==0
 finally:s.close()
try:
 open('/inputs/reviews.jsonl','ab').write(b'x'); readonly=False
except OSError: readonly=True
count=duckdb.sql("select count(*) from read_json_auto('/inputs/reviews.jsonl')").fetchone()[0]
import pandas as pd
pd.DataFrame({'rating':[5], 'count':[count]}).to_csv('/workspace/counts.csv',index=False)
v=os.statvfs('/workspace')
print(json.dumps({'uid':os.getuid(),'count':count,'inputs_readonly':readonly,'workspace_bytes':v.f_frsize*v.f_blocks,'network':reachable('1.1.1.1',443),'host':reachable('172.17.0.1',22),'docker_socket':os.path.exists('/var/run/docker.sock'),'capability':os.path.exists('/run/voxquarry/capability'),'secret_env':any(k in os.environ for k in ['VOXQUARRY_LLM_API_KEY','VOXQUARRY_LLM_API_KEY','DOCKER_HOST'])}))'''
        result = client.tool(attempt, "run_python", {"code": code})
        assert result["exit_code"] == 0, result
        observed = json.loads(result["stdout"])
        assert observed == {"uid": 10000, "count": 600000, "inputs_readonly": True, "workspace_bytes": 2 * 1024**3, "network": False, "host": False, "docker_socket": False, "capability": False, "secret_env": False}, observed
        assert client.read_output(attempt, "/workspace/counts.csv") == b"rating,count\n5,600000\n"
        evidence["analysis"] = {**observed, "input_bytes": len(shard), "input_sha256": upload["sha256"]}
        assert client.tool(attempt, "run_shell", {"command": "printf 'shell-ready' > /workspace/shell.txt"})["exit_code"] == 0
        assert client.read_output(attempt, "/workspace/shell.txt") == b"shell-ready"
        client.tool(attempt, "run_shell", {"command": "ln -s /etc/passwd /workspace/escape"})
        try:
            client.tool(attempt, "read_file", {"path": "/workspace/escape"})
            raise AssertionError("symlink unexpectedly readable")
        except Exception as exc:
            assert getattr(exc, "code", None) == "analysis_file_unavailable", exc
        document_code = '''from docx import Document
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont
d=Document();d.add_paragraph('Battery runtime is 50 minutes.');d.save('/workspace/document.docx')
w=Workbook();s=w.active;s.append(['topic','count']);s.append(['battery',15]);s.append(['formula','=1+2']);w.save('/workspace/sheet.xlsx')
im=Image.new('RGB',(700,120),'white');draw=ImageDraw.Draw(im);font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',28);draw.text((15,35),'Battery runtime is 50 minutes.',fill='black',font=font);im.save('/workspace/image.png')
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
p=PdfWriter();page=p.add_blank_page(width=500,height=300)
font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
stream=DecodedStreamObject();stream.set_data(b'BT /F1 18 Tf 40 200 Td (Battery runtime is 50 minutes.) Tj ET');page[NameObject('/Contents')]=p._add_object(stream)
with open('/workspace/document.pdf','wb') as f:p.write(f)
'''
        assert client.tool(attempt, "run_python", {"code": document_code})["exit_code"] == 0
        parsed = {}
        for filename in ("document.docx", "sheet.xlsx", "image.png", "document.pdf"):
            data = client.read_output(attempt, "/workspace/" + filename)
            item = client.put_input(attempt, filename, data)
            value = client.tool(attempt, "parse_attachment", {"filename": filename, "source_sha256": "sha256:" + item["sha256"]})
            assert value["source_sha256"] == "sha256:" + item["sha256"]
            assert "15" in value["text"] if filename.endswith("xlsx") else "50" in value["text"], value
            assert value["locators"] and all(0 <= row["start"] < row["end"] <= len(value["text"]) for row in value["locators"])
            parsed[filename] = {"characters": len(value["text"]), "locators": len(value["locators"]), "parser_version": value["parser_version"]}
        evidence["parsers"] = parsed
        client.stop(attempt)
        attempt = "smoke_harness"
        client.start({"attempt_id": attempt, "run_id": "smoke_run", "model_id": "deepseek-v4.1-flash", "deadline_seconds": 90,
                      "system_prompt": "Use the probe tool and then state the result.", "prompt": "Run the fixture probe.", "capability": "synthetic-" + "c" * 48,
                      "tools": [{"name": "probe", "description": "A fixed synthetic probe.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}]})
        wait_until(lambda: client.status(attempt)["ready"])
        state = manager.attempts[attempt]; runner = state["prefix"] + "-runner"
        wait_until(lambda: engine.call(["inspect", "--format", "{{.State.Running}}", runner], check=False).strip() == b"true")
        network = json.loads(engine.call(["network", "inspect", state["network"]]))[0]
        gateway = network["IPAM"]["Config"][0]["Gateway"]
        probe = f'''import socket,json
def connected(host,port):
 s=socket.socket();s.settimeout(1)
 try:s.connect((host,port));return True
 except OSError:return False
 finally:s.close()
try:socket.getaddrinfo('example.com',80);dns=True
except OSError:dns=False
print(json.dumps({{'relay':connected('relay',9000),'vm':connected({gateway!r},22),'direct_broker':connected({broker_ip!r},9010),'internet':connected('1.1.1.1',443),'dns':dns}}))'''
        observed = json.loads(engine.call(["exec", runner, "python", "-c", probe], timeout=15))
        if os.environ.get("WENQUAN_SYNTHETIC_DEBUG") == "1":
            print("relay_state", engine.call(["inspect", "--format", "{{json .State}}", state["prefix"] + "-relay"]).decode(), flush=True)
            debug = "import runpy,traceback; m=runpy.run_path('/opt/voxquarry/relay.py');\ntry:print(m['RelayConfig'].from_files())\nexcept Exception:traceback.print_exc(file=__import__('sys').stdout)"
            print("relay_config", engine.call(["exec", state["prefix"] + "-relay", "python", "-c", debug], check=False).decode(), flush=True)
            print("network_observed", observed, flush=True)
        result = wait_until(lambda: (value if (value := client.status(attempt))["status"] != "running" else None), timeout=60)
        assert result["status"] == "completed", result
        assert observed == {"relay": True, "vm": False, "direct_broker": False, "internet": False, "dns": False}, observed
        assert result["result"]["answer"] == "隔离执行验证通过", result
        upstream = ({"model_calls": broker_scope["calls"], "seen_tool": broker_scope["seen_tool"]} if broker_scope is not None else
                    json.loads(engine.call(["exec", fake, "python", "-c", "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9010/status').read().decode())"])))
        assert upstream == {"model_calls": 2, "seen_tool": True}, upstream
        evidence["runner"] = {"status": result["status"], "events": len(result["events"]), **upstream, "network": observed}
        client.stop(attempt)
        # A deadline kills the process tree and retains only a fixed reader for
        # already-created checkpoint output; stop then destroys all task volumes.
        attempt = "smoke_deadline"
        client.start({"attempt_id": attempt, "run_id": "smoke_run", "mode": "analysis", "deadline_seconds": 5})
        wait_until(lambda: client.status(attempt)["ready"])
        client.tool(attempt, "write_file", {"path": "/workspace/checkpoint.txt", "content": "saved before deadline"})
        try:
            client.tool(attempt, "run_python", {"code": "import time;time.sleep(30)", "timeout_seconds": 30})
            raise AssertionError("deadline must not return a late tool result")
        except Exception as exc:
            assert getattr(exc, "code", None) in {"attempt_inactive", "analysis_result_invalid", "engine_operation_failed"}, exc
        result = client.status(attempt)
        assert result["status"] == "failed" and result["result"] is None, result
        assert client.read_output(attempt, "/workspace/checkpoint.txt") == b"saved before deadline"
        client.stop(attempt); evidence["deadline"] = {"status": result["status"], "checkpoint_recovered": True}
        assert not engine.call(["ps", "-aq", "--filter", "label=" + manager_module.LABEL]).strip()
        assert not engine.call(["volume", "ls", "-q", "--filter", "label=" + manager_module.LABEL]).strip()
        args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        print(json.dumps({"status": "passed", "evidence": str(args.output), "real_model_calls": 0}))
    finally:
        if server:
            server.shutdown(); server.server_close()
        if manager:
            manager.closed.set()
            for attempt_id in tuple(manager.attempts):
                def cleaned():
                    try:
                        manager.stop(attempt_id)
                        return True
                    except manager_module.ManagerError:
                        return False
                wait_until(cleaned, timeout=60)
        if broker_server:
            broker_server.shutdown(); broker_server.server_close()
        else:
            engine.call(["rm", "-f", fake], check=False)


if __name__ == "__main__":
    main()
