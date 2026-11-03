"""Reproducible public-tree checks. Reports file/rule names, never secret values."""
import argparse
import re
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('--tracked',action='store_true');args=parser.parse_args()
command=['git','ls-files','-z'] if args.tracked else ['git','ls-files','-z','--cached','--others','--exclude-standard']
files=sorted(set(p for p in subprocess.check_output(command,cwd=ROOT).decode().split('\0') if p))
rules={
 'private_key':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
 'github_token':re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{60,})\b'),
 'provider_key':re.compile(r'\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{32,}\b'),
 'aws_key':re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
 'personal_path':re.compile(r'/Users/[A-Za-z][^/\s]+/'),
 'retired_brand':re.compile('Ground'+'Truth|ground'+'truth|'+'沃'+'土|'+'峰'+'巡|'+'灼'+'见|'+'成'+'木|'+'问'+'泉'),
 'private_work_order':re.compile(r'GT-(?:WO|DSP)-\d{8}'),
}
problems=[]
for name in files:
    path=ROOT/name
    if path.is_symlink():problems.append((name,'symlink'));continue
    if not path.is_file():problems.append((name,'missing'));continue
    if any(part in {'.runtime','.venv','node_modules','data','var','__pycache__'} for part in path.relative_to(ROOT).parts):problems.append((name,'runtime_data'))
    if path.suffix.lower() in {'.sqlite','.sqlite3','.db','.pem','.key','.log','.jsonl','.har','.pdf','.zip'}:problems.append((name,'forbidden_artifact'))
    if path.name.startswith('.env') and path.name!='.env.example':problems.append((name,'environment_file'))
    if path.stat().st_size>5*1024*1024:problems.append((name,'large_asset'))
    try:content=path.read_text()
    except UnicodeError:continue
    if name=='scripts/release_scan.py':continue
    for rule,regex in rules.items():
        if regex.search(content):problems.append((name,rule))
if problems:
    for name,rule in problems:print(f'{rule}: {name}')
    raise SystemExit(f'Public-tree scan failed: {len(problems)} findings')
print(f'Public-tree scan passed: {len(files)} files; no configured forbidden artifacts or patterns. This is not a proof of absence of all secrets.')
