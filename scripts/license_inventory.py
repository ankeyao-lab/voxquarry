"""Collect installed dependency licenses without copying package implementations."""
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
ROOT=Path(__file__).resolve().parents[1]
output=ROOT/'third_party/licenses';output.mkdir(parents=True,exist_ok=True)
items=[]
for dist in sorted(metadata.distributions(),key=lambda d:d.metadata['Name'].lower()):
    name,version=dist.metadata['Name'],dist.version
    if name.lower() in {'pip','setuptools','wheel'}:continue
    files=[]
    for entry in dist.files or []:
        if '.dist-info/' in str(entry) and (entry.name.lower().startswith(('license','copying','notice'))):
            src=Path(dist.locate_file(entry));target=output/(name+'-'+version+'-'+entry.name)
            if src.is_file():shutil.copyfile(src,target);files.append(str(target.relative_to(ROOT)))
    license_name=dist.metadata.get('License-Expression') or dist.metadata.get('License','').split('\n')[0] or ', '.join(x.split(' :: ')[-1] for x in dist.metadata.get_all('Classifier',[]) if x.startswith('License ::')) or 'See upstream license'
    items.append(dict(ecosystem='Python',name=name,version=version,license=license_name,notices=files))
lock=json.loads((ROOT/'web/package-lock.json').read_text())
for key,item in sorted(lock['packages'].items()):
    if not key:continue
    name=key.split('node_modules/')[-1];folder=ROOT/'web'/key;files=[]
    if folder.is_dir():
        for src in folder.iterdir():
            if src.is_file() and src.name.lower().startswith(('license','licence','copying','notice')):
                target=output/(name.replace('/','--')+'-'+item['version']+'-'+src.name)
                shutil.copyfile(src,target);files.append(str(target.relative_to(ROOT)))
    items.append(dict(ecosystem='npm',name=name,version=item['version'],license=item.get('license','See upstream license'),notices=files))
(ROOT/'third_party/inventory.json').write_text(json.dumps(items,ensure_ascii=False,indent=2)+'\n')
print(f'Inventoried {len(items)} dependency versions')
