"""Opt-in official SDK retry proof inside one disposable, network-none container.

Uses the existing immutable runner image and current trusted patch. No manager,
real model, account, shared dataset, published port or host mount is involved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess


CONTAINER_SCRIPT = r'''
import importlib.metadata, json, os, sys, threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from deepseek_harness import DeepSeekHarness

os.umask(0o077)
fixture=json.load(sys.stdin)
assert importlib.metadata.version('deepseek-harness-sdk') == '0.1.5rc1'
root=Path('/state/fixture');root.mkdir()
for name,content in fixture['files'].items():
 (Path('/opt/voxquarry')/name).write_text(content)
token='synthetic-retry-only-capability-'+ 'x'*40
Path('/run/voxquarry/capability').write_text(token)
tools=[{'name':name,'description':'Synthetic proof only','parameters':{'type':'object','properties':{},'additionalProperties':False}} for name in ('record_side_effect','submit_answer')]
Path('/run/voxquarry/tools.json').write_text(json.dumps(tools))
state={}
def chunk(tool,call,content=''):
 delta={'role':'assistant'}
 if content:delta['content']=content
 if tool:delta['tool_calls']=[{'index':0,'id':'synthetic-call-'+str(call),'type':'function','function':{'name':tool,'arguments':'{}'}}]
 return {'id':'synthetic','object':'chat.completion.chunk','choices':[{'index':0,'delta':delta,'finish_reason':None}]}
class Handler(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*args):pass
 def json_reply(self,status,value):
  raw=json.dumps(value).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.send_header('Connection','close');self.end_headers();self.wfile.write(raw);self.close_connection=True
 def do_POST(self):
  assert self.headers.get('Authorization')=='Bearer '+token
  request=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
  if self.path=='/broker/tools/record_side_effect':
   state['side_effects']+=1
   self.json_reply(503 if state['case']=='tool_failure' else 200,{'error':{'code':'synthetic_tool_failure'}} if state['case']=='tool_failure' else {'recorded':True});return
  if self.path=='/broker/tools/submit_answer':
   state['submits']+=1;self.json_reply(200,{'accepted':True});return
  if self.path!='/broker/model/v1/chat/completions':self.json_reply(404,{'error':{'code':'route_rejected'}});return
  state['model_calls']+=1;call=state['model_calls'];case=state['case']
  if case.startswith('http_') and (call==1 or case in ('http_401','http_403','http_404')):
   self.json_reply(int(case[5:]),{'error':{'message':'synthetic request failure'}});return
  prior_tool=case in ('recovered_after_tool','residual_after_tool_failure','tool_failure')
  failing=(case=='persistent_failure' or case=='residual_after_tool_failure' and call>1 or case=='recovered_after_tool' and call==2)
  tool='record_side_effect' if prior_tool and call==1 else 'submit_answer'
  if failing:tool='record_side_effect'
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Connection','close');self.end_headers()
  # Failed streams include a tool-call delta deliberately. Neither that tool
  # nor partial text may execute/enter the reconstructed successful history.
  content='prior step process note' if prior_tool and call==1 else 'discard failed partial' if failing else ''
  self.wfile.write(('data: '+json.dumps(chunk(tool,call,content))+'\n\n').encode());self.wfile.flush()
  if failing:self.close_connection=True;return
  finish={'id':'synthetic','object':'chat.completion.chunk','choices':[{'index':0,'delta':{},'finish_reason':'tool_calls'}]}
  self.wfile.write(('data: '+json.dumps(finish)+'\n\ndata: [DONE]\n\n').encode());self.wfile.flush();self.close_connection=True

server=ThreadingHTTPServer(('127.0.0.1',9000),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
cases=[]
try:
 for case,expected_calls,expected_tools,expected_submits,expected_finish in (
  ('recovered_after_tool',3,1,1,'completed'),
  ('persistent_failure',2,0,0,'error'),
  ('residual_after_tool_failure',3,1,0,'error'),
  ('http_401',1,0,0,'error'),('http_403',1,0,0,'error'),('http_404',1,0,0,'error'),
  ('http_503',2,0,1,'completed'),('http_429',2,0,1,'completed'),
  ('tool_failure',2,1,1,'completed')):
  state.clear();state.update(case=case,model_calls=0,side_effects=0,submits=0)
  observed=[]
  def notify(item):
   if item.method=='session.event':
    event=item.payload.get('event',{});data=event.get('data',{})
    if event.get('type')=='llm/retry':observed.append({'retry':data.get('retry'),'max_retries':data.get('maxRetries'),'failure_code':data.get('failure',{}).get('code')})
  with DeepSeekHarness(provider='deepseek-official',model='deepseek-v4.1-flash-1017',reasoning_effort='high',max_tokens=16384,
     cwd='/workspace',dsh_home='/state/dsh-'+case,profile='sdk-minimal',patches=('/opt/voxquarry/research.patch.yml',),
     base_url='http://relay:9000/broker/model/v1',api_key=token,initialize_timeout_seconds=30,request_timeout_seconds=30) as harness:
   result=harness.run('Run only the synthetic fixture.',session_id='synthetic-'+case,on_notification=notify)
  assert (state['model_calls'],state['side_effects'],state['submits'],result.finish_reason)==(expected_calls,expected_tools,expected_submits,expected_finish),(case,state,result.finish_reason)
  expected_retries=1 if case in ('recovered_after_tool','persistent_failure','residual_after_tool_failure','http_503','http_429') else 0
  assert len(observed)==expected_retries and all(row['retry']==1 and row['max_retries']==1 for row in observed),(case,observed)
  if case=='residual_after_tool_failure':assert result.final_response=='prior step process note'
  cases.append({**state,'finish_reason':result.finish_reason,'residual_answer_present':bool(result.final_response),'retries':observed})
finally:
 server.shutdown();server.server_close()
print(json.dumps({'status':'passed','sdk_version':importlib.metadata.version('deepseek-harness-sdk'),'network':'none','real_model_calls':0,'cases':cases}))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--expected-engine-id', required=True)
    parser.add_argument('--runner-image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get('WENQUAN_RUN_VM_SMOKE') != '1' or not re.fullmatch(r'sha256:[a-f0-9]{64}', args.runner_image):
        raise SystemExit('Explicit synthetic opt-in and immutable runner image required')
    if not args.output.is_absolute() or args.output.exists() or args.output.parent.is_symlink():
        raise SystemExit('New private output path required')
    base = ['docker', '--context', 'colima-voxquarry']
    info = json.loads(subprocess.check_output(base + ['info', '--format', '{{json .}}']))
    if info['ID'] != args.expected_engine_id or info['Name'] != 'colima-voxquarry':
        raise SystemExit('Dedicated engine identity mismatch')
    root = Path(__file__).resolve().parent
    files = {name: (root / name).read_text() for name in ('research.patch.yml', 'research-tools.mjs')}
    name = 'voxquarry-sdk-retry-synthetic-' + secrets.token_hex(5)
    command = base + ['run', '--rm', '-i', '--name', name, '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                     '--security-opt', 'no-new-privileges', '--user', '10000:10000', '--memory', '1536m', '--cpus', '2', '--pids-limit', '128',
                     '--add-host', 'relay:127.0.0.1']
    for path, size in (('/state', '512m'), ('/workspace', '64m'), ('/run/voxquarry', '1m'), ('/opt/voxquarry', '1m'), ('/tmp', '64m')):
        command += ['--tmpfs', path + ':rw,nosuid,nodev,size=' + size + ',uid=10000,gid=10000,mode=0700']
    command += ['--entrypoint', 'python', args.runner_image, '-c', CONTAINER_SCRIPT]
    try:
        output = subprocess.run(command, input=json.dumps({'files': files}).encode(), capture_output=True, timeout=180)
        if output.returncode:
            # No real data exists, but avoid normalizing verbose runtime errors
            # into repository or app logs. A private failure artifact is enough.
            evidence = {'status': 'failed', 'returncode': output.returncode,
                        'synthetic_stderr': output.stderr.decode(errors='replace')[-16000:]}
        else:
            evidence = json.loads(output.stdout)
        evidence.update(engine_id=args.expected_engine_id, runner_image=args.runner_image,
                        source_sha256={key: hashlib.sha256(value.encode()).hexdigest() for key, value in files.items()})
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(evidence, handle, ensure_ascii=False, indent=2)
        if evidence['status'] != 'passed':
            raise SystemExit('Synthetic SDK proof failed; see private evidence')
        print(json.dumps({'status': 'passed', 'real_model_calls': 0, 'cases': len(evidence['cases']), 'evidence': str(args.output)}))
    finally:
        subprocess.run(base + ['rm', '-f', name], capture_output=True, check=False)


if __name__ == '__main__':
    main()
