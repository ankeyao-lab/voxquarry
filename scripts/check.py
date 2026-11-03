"""Run independent module suites with no deployment/provider credentials."""
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
env={key:value for key,value in os.environ.items() if not key.startswith(('VOXQUARRY_','VQ_','FIRECRAWL_')) and key not in {'DATA_DIR','BROWSER_EXECUTABLE'}}
env['PYTHONWARNINGS']='ignore::DeprecationWarning'
suites=[['collector/tests','collector'],['reporting/tests','reporting'],['agent/tests','agent'],['account_admin/tests','accounts'],['retail_landscape/tests','products'],['tests','integration'],['runtime/harness','runtime']]
failed=[]
for folder,name in suites:
    print(f'Checking {name}',flush=True)
    result=subprocess.run([sys.executable,'-m','unittest','discover','-s',folder],cwd=ROOT,env=env)
    if result.returncode:failed.append(name)
if failed:raise SystemExit('Failed: '+', '.join(failed))
print('All module suites passed. Runtime document-render tests may require optional image dependencies.')
