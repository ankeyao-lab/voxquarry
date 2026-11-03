"""Package one clean commit; never recursively zip a developer workspace."""
import hashlib
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT).strip():
    raise SystemExit('Commit the reviewed public tree first; packaging requires a clean repository.')
commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip()
output=ROOT/'dist';output.mkdir(exist_ok=True)
archive=output/f'voxquarry-{commit[:12]}.zip'
subprocess.run(['git','archive','--format=zip','--prefix=voxquarry/','-o',str(archive),'HEAD'],cwd=ROOT,check=True)
digest=hashlib.sha256(archive.read_bytes()).hexdigest()
(archive.with_suffix('.sha256')).write_text(f'{digest}  {archive.name}\n')
print(archive);print(digest)
