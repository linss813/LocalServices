"""Private, platform-specific storage; no credentials in audit records."""
import json
import os
import platform
import time
from pathlib import Path

SYSTEM = platform.system()
DATA = (Path.home()/'Library/Application Support/LocalServiceManager' if SYSTEM=='Darwin' else
        Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'LocalServiceManager' if SYSTEM=='Windows' else
        Path(os.environ.get('XDG_DATA_HOME',str(Path.home()/'.local/share')))/'local-service-manager')

if os.environ.get('LOCAL_SERVICE_DATA'):
    DATA=Path(os.environ['LOCAL_SERVICE_DATA']).expanduser().resolve()

def read(name, default):
    p=DATA/name
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else default

def write(name, value):
    DATA.mkdir(parents=True,exist_ok=True)
    if os.name!='nt': DATA.chmod(0o700)
    p=DATA/name
    tmp=p.with_suffix('.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    if os.name!='nt': tmp.chmod(0o600)
    tmp.replace(p)

def audit(action, ident='', outcome='ok'):
    DATA.mkdir(parents=True,exist_ok=True)
    p=DATA/'audit.jsonl'
    if p.exists() and p.stat().st_size>2*1024*1024:
        p.replace(DATA/'audit.previous.jsonl')
    with p.open('a',encoding='utf-8') as f:
        f.write(json.dumps(dict(time=time.time(),action=action,id=str(ident)[:160],outcome=str(outcome)[:400]),ensure_ascii=False)+'\n')
    if os.name!='nt': p.chmod(0o600)

def audit_tail():
    p=DATA/'audit.jsonl'
    if not p.exists():return []
    with p.open('rb') as f:
        f.seek(max(0,p.stat().st_size-65536))
        lines=f.read().decode(errors='replace').splitlines()
    result=[]
    for line in lines[-40:]:
        try:result.append(json.loads(line))
        except ValueError:pass
    return result[::-1]
