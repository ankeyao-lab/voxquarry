"""Opt-in official SDK regression without real models, credentials or data.

Uses the pinned analysis image and current archived adapters. One disposable
container hosts the synthetic broker and separate provider process; production
isolation is independently checked by vm_smoke and manager tests.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--expected-engine-id', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get('WENQUAN_RUN_VM_SMOKE') != '1':
        raise SystemExit('Opt in with WENQUAN_RUN_VM_SMOKE=1')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', args.image):
        raise SystemExit('An immutable image ID is required')
    docker = ['docker', '--context', 'colima-voxquarry']
    observed = json.loads(subprocess.check_output(docker + ['info', '--format', '{{json .}}']))
    if observed['ID'] != args.expected_engine_id or observed['Name'] != 'colima-voxquarry':
        raise SystemExit('Dedicated research engine identity mismatch')
    root = Path(__file__).resolve().parent
    names = ('native_service.py', 'native-service.mjs', 'native-service.patch.yml',
             'native-providers.mjs', 'native-web.mjs', 'research-tools.mjs', 'research.patch.yml',
             'session_resume.py', 'session-resume.mjs')
    fixture = {name: (root / name).read_text() for name in names}
    skills = root.parents[2] / 'agent/research_skills'
    for path in skills.rglob('*.md'):
        fixture['skills/' + path.relative_to(skills).as_posix()] = path.read_text()
    name = 'gtw-a1-sdk-synthetic'
    command = docker + ['run', '--rm', '-i', '--name', name, '--network', 'none',
        '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--user', '10000:10000', '--memory', '3g', '--pids-limit', '256',
        '--env', 'HOME=/workspace', '--add-host', 'relay:127.0.0.1']
    for path, size in (('/workspace', '768m'), ('/state', '256m'), ('/tmp', '256m'),
                       ('/opt/voxquarry', '2m'), ('/run/voxquarry', '1m')):
        command += ['--tmpfs', path + ':rw,nosuid,nodev,exec,size=' + size + ',uid=10000,gid=10000,mode=0700']
    command += ['--entrypoint', 'python', args.image, '-c', (root / 'native_sdk_fixture.py').read_text()]
    try:
        with args.output.open('xb') as output:
            result = subprocess.run(command, input=json.dumps(fixture).encode(),
                                    stdout=output, stderr=subprocess.STDOUT, timeout=240)
        if result.returncode:
            raise SystemExit('SDK fixture failed; inspect the synthetic evidence log')
    finally:
        subprocess.run(docker + ['rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(json.dumps({'status': 'passed', 'image': args.image, 'evidence': str(args.output), 'real_model_calls': 0}))


if __name__ == '__main__':
    main()
