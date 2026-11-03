"""Synthetic SDK fixture, run only inside native_sdk_smoke.py's disposable container."""
import json, os, subprocess, threading, socket, time, sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from deepseek_harness import DeepSeekHarness
fixture=json.load(sys.stdin)
os.umask(0o077)
for name, content in fixture.items():
 path = Path('/opt/voxquarry')/name
 path.parent.mkdir(parents=True, exist_ok=True)
 path.write_text(content)
# Separate process runs native providers; this synthetic-only fixture has no keys/data.
daemon=subprocess.Popen(['python','/opt/voxquarry/native_service.py'], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=open('/tmp/native-errors','w'))
for _ in range(400):
 if Path('/tmp/voxquarry-native.sock').exists(): break
 if daemon.poll() is not None: raise RuntimeError(Path('/tmp/native-errors').read_text())
 time.sleep(.05)
Path('/run/voxquarry/capability').write_text('synthetic-a1-token')
Path('/run/voxquarry/request.json').write_text(json.dumps({'session_key':'a1-native','model_id':'deepseek-v4.1-flash','reasoning_effort':'max','max_output_tokens':1024}))
Path('/run/voxquarry/tools.json').write_text(json.dumps([{'name':name,'description':'Synthetic independent read',
 'parameters':{'type':'object','properties':{},'additionalProperties':False},'concurrency_safe':True}
 for name in ['synthetic_left','synthetic_right']]))
os.environ['DSH_SYSTEM_PROMPT']='Synthetic official SDK tool verification.'
results=[];requests=[];compactions=[];retries=[];case=0;step=0
steps=[('write',{'file_path':'/workspace/test.txt','content':'alpha\nbeta\n'}),('read',{'file_path':'/workspace/test.txt'}),('edit',{'file_path':'/workspace/test.txt','old_string':'beta','new_string':'gamma'}),('bash',{'command':"python -c \"print(open('/workspace/test.txt').read())\"",'description':'Read the file with Python'}),('bash',{'command':"sleep 1; printf 'background-ok'",'description':'Verify a background process completes','run_in_background':True}),('job_list',{}),('job_output',{'job_id':'bash-1','wait':True}),('glob',{'pattern':'test.txt','path':'/workspace'}),('grep',{'pattern':'gamma','path':'/workspace/test.txt'})]
steps += [('skill', {'name': 'product-research'}), ('skill', {'name': 'user-research'}),
 ('read', {'file_path': '/opt/voxquarry/skills/shared/methods.md'}), ('__parallel__', {}),
 ('web_search', {'queries':['product','manual']}), ('web_fetch', {'url':'https://example.test/manual'})]
parallel_reads=threading.Barrier(2); parallel_settled=[]; web_requests=[]
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def reply(self,status,value):
  raw=json.dumps(value).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
 def do_POST(self):
  global step
  value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  if self.path in {'/broker/tools/synthetic_left','/broker/tools/synthetic_right'}:
   parallel_reads.wait(timeout=5);parallel_settled.append(self.path)
   return self.reply(200,{'result':'independent-read-complete'})
  if self.path=='/broker/tools/search_web':
   web_requests.append(value);query=value['arguments']['query']
   return self.reply(200,{'provider':'synthetic','query':query,'source_id':'src_synthetic_'+query,
    'registered_urls':['https://example.test/'+query],'content':[{'type':'text','text':'Synthetic public source'}]})
  if self.path=='/broker/tools/read_web_page':
   web_requests.append(value)
   return self.reply(200,{'url':value['arguments']['url'],'source_id':'src_synthetic_page','text':'Synthetic manual content'})
  if self.path=='/broker/native':
   with socket.socket(socket.AF_UNIX) as s:
    s.settimeout(30);s.connect('/tmp/voxquarry-native.sock');s.sendall(json.dumps(value).encode()+b'\n');parts=[]
    while chunk:=s.recv(65536):parts.append(chunk)
   return self.reply(200,json.loads(b''.join(parts)))
  if self.path!='/broker/model/v1/chat/completions':return self.reply(200,{'saved':True})
  requests.append(value)
  if len(requests)==1:return self.reply(503,{'error':{'message':'Synthetic transient error'}})
  if step==0: print(json.dumps({'tools':[t['function']['name'] for t in value.get('tools',[])]}),flush=True)
  if value.get('max_tokens')==8192:
   delta={'role':'assistant','content':'Earlier request: Run synthetic tools. The saved file test.txt contains alpha and gamma; background task completed.'};reason='stop'
  elif step < len(steps):
   name,args=steps[step]
   batch=[('synthetic_left',{}),('synthetic_right',{})] if name=='__parallel__' else [(name,args)]
   delta={'role':'assistant','tool_calls':[{'index':index,'id':f'call-{step}-{index}','type':'function','function':{'name':tool,'arguments':json.dumps(arguments)}} for index,(tool,arguments) in enumerate(batch)]};reason='tool_calls'
  else: delta={'role':'assistant','content':'fixture-complete. Saved /workspace/test.txt contains alpha and gamma. '+('synthetic summary detail '*1000 if step==len(steps) else '')};reason='stop'
  if value.get('max_tokens')!=8192:step+=1
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Connection','close');self.end_headers()
  for d,r in [(delta,None),({},reason)]:self.wfile.write(('data: '+json.dumps({'id':'test','choices':[{'index':0,'delta':d,'finish_reason':r}], 'usage':{'prompt_tokens':max(1,len(json.dumps(value))//4),'completion_tokens':20,'total_tokens':max(1,len(json.dumps(value))//4)+20}})+'\n\n').encode())
  self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush();self.close_connection=True
server=ThreadingHTTPServer(('127.0.0.1',9000),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
def notify(item):
 if item.method=='session.event' and item.payload.get('event',{}).get('type')=='llm/retry':retries.append(item.payload['event']['data'])
 if item.method=='session.event' and item.payload.get('event',{}).get('type','').startswith('compaction/'):
  compactions.append(item.payload['event']['type'])
  assert not item.payload['event']['data'].get('error'), item.payload['event']['data']
 if item.method=='session.event' and item.payload.get('event',{}).get('type')=='tool/call': print(json.dumps({'tool_call':item.payload['event']['data']}),flush=True)
 if item.method=='session.event' and item.payload.get('event',{}).get('type')=='tool/result':
  result=item.payload['event']['data'];results.append(result);print(json.dumps({'tool_result':result}),flush=True)
try:
 with DeepSeekHarness(profile='sdk',cwd='/workspace',dsh_home='/state/dsh',patches=('/opt/voxquarry/research.patch.yml',),model='deepseek-v4.1-flash',reasoning_effort='max',max_tokens=32768,api_key='synthetic-a1-token',base_url='http://relay:9000/broker/model/v1',request_timeout_seconds=180) as h:
  sys.path.insert(0,'/opt/voxquarry')
  from session_resume import session_exists
  assert not session_exists('a1-native')
  result=h.run('Run synthetic tools. '+('synthetic historical context '*3000),session_id='a1-native',on_notification=notify)
 assert 'background-ok' in json.dumps(results), results
 assert all(name in json.dumps(requests[0]['messages']) for name in ('product-research','user-research')), 'Native skill discovery missing'
 assert 'wenquan-research' not in json.dumps(requests[0]['messages']), 'Retired skill still exposed'
 assert 'skill_content' in json.dumps(results), 'Native skill load missing'
 assert '画像与比较' in json.dumps(results, ensure_ascii=False), 'Relative resource read missing'
 assert os.environ['DSH_SYSTEM_PROMPT'] in json.dumps(requests[0]['messages'])
 # Trigger the native policy without falsifying the provider's 1M capability.
 os.environ['DSH_CONTEXT_WINDOW']='1000000'
 os.environ['DSH_RESEARCH_CONTEXT_TOKENS']='20000'
 # A fresh SDK process resumes the same official JSONL session.
 with DeepSeekHarness(profile='sdk',cwd='/workspace',dsh_home='/state/dsh',patches=('/opt/voxquarry/research.patch.yml',),model='deepseek-v4.1-flash',reasoning_effort='max',max_tokens=1024,api_key='synthetic-a1-token',base_url='http://relay:9000/broker/model/v1',request_timeout_seconds=180) as h:
  sys.path.insert(0,'/opt/voxquarry')
  from session_resume import resume_session
  assert session_exists('a1-native')
  followup=resume_session(h,'a1-native','Continue with the earlier analysis.',on_notification=notify)
 assert 'Run synthetic tools.' in json.dumps(requests[-1]['messages'])
 assert 'gamma' in json.dumps(requests[-1]['messages'])
 print(json.dumps({'finish':result.finish_reason,'answer':result.final_response[:90],'calls':len(requests),'compactions':compactions,'retries':retries,'summary_output_budgets':[r.get('max_tokens') for r in requests if r.get('max_tokens')==8192],'effort':requests[0].get('reasoning_effort')}),flush=True)
 assert len(results)==len(steps)+1
 assert len(parallel_settled)==2, 'Official SDK did not overlap independent read tools'
 assert len(web_requests)==3, web_requests
 assert 'src_synthetic_page' in json.dumps(results), 'Native web lost source provenance'
 assert len(retries)==1, retries
 assert 'compaction/end' in compactions, compactions
 assert any(r.get('max_tokens')==8192 for r in requests)
 assert all(not row['message']['content'][0].get('isError') for row in results), results
 assert 'gamma' in requests[-1]['messages'][-2]['content'] or Path('/workspace/test.txt').read_text()=='alpha\ngamma\n'
finally:
 server.shutdown();daemon.terminate();daemon.wait(timeout=10)
