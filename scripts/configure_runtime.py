"""Operator-invoked build/configuration for an explicitly named dedicated engine.

Does not select a global Docker context or start/replace containers.
"""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--context',required=True);p.add_argument('--broker-url',required=True);p.add_argument('--output',type=Path,default=ROOT/'.runtime/harness');args=p.parse_args()
if args.context in {'default','desktop-linux'}:raise SystemExit('Use an explicitly dedicated Docker context.')
u=urlsplit(args.broker_url)
if u.scheme not in {'http','https'} or not u.hostname or u.username or u.password or u.path.rstrip('/')!='/wenquan/internal/broker' or u.query or u.fragment:raise SystemExit('Provide an exact private broker URL ending in /wenquan/internal/broker.')
prefix=['docker','--context',args.context]
def read(*command):return subprocess.check_output(prefix+list(command),cwd=ROOT).decode().strip()
info=json.loads(read('info','--format','{{json .}}'))
if info['Name'] not in {'colima-voxquarry','voxquarry'}:raise SystemExit('Engine must be dedicated and named colima-voxquarry or voxquarry.')
if read('ps','-aq'):raise SystemExit('Initial configuration requires an empty dedicated engine; no existing containers were changed.')
config={'expected_engine_id':info['ID'],'expected_engine_name':info['Name'],'broker_base_url':args.broker_url,'research_concurrency':1}
for target in ['runner','analysis','relay','manager']:
    tag='voxquarry-'+target+':alpha'
    subprocess.run(prefix+['build','-f','runtime/harness/Dockerfile','--target',target,'-t',tag,'.'],cwd=ROOT,check=True)
    config[target+'_image']=read('image','inspect','--format','{{.Id}}',tag)
args.output.mkdir(parents=True,exist_ok=True,mode=0o700)
for name,value in [('manager.json',json.dumps(config,indent=2)+'\n'),('management-token',secrets.token_urlsafe(48)+'\n')]:
    target=args.output/name
    with target.open('x') as stream:
        target.chmod(0o600);stream.write(value)
print('Built and pinned images; wrote private configuration to '+str(args.output.resolve()))
print('No containers started. Follow docs/runtime.md for operator-controlled startup and smoke verification.')
