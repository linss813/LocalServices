"""Task/systemd entry point. Never interprets the command through a shell."""
import json
from pathlib import Path
import signal
import subprocess
import sys
import psutil

def main():
    data=Path(sys.argv[1]);label=sys.argv[2]
    item=next(x for x in json.loads((data/'services.json').read_text(encoding='utf-8')) if x['label']==label)
    log=data/(label+'.log')
    if log.exists() and log.stat().st_size>10*1024*1024:log.replace(data/(label+'.previous.log'))
    state=data/(label+'.state.json')
    with log.open('ab',buffering=0) as output:
        child=subprocess.Popen(item['command'],cwd=item['cwd'],stdout=output,stderr=output)
        def stop(*_):
            try:
                p=psutil.Process(child.pid)
                for c in p.children(recursive=True):c.terminate()
                p.terminate()
            except psutil.NoSuchProcess:pass
        signal.signal(signal.SIGTERM,stop)
        signal.signal(signal.SIGINT,stop)
        state.write_text(json.dumps(dict(pid=child.pid,created=psutil.Process(child.pid).create_time())),encoding='utf-8')
        try:return child.wait()
        finally:state.unlink(missing_ok=True)
if __name__=='__main__':sys.exit(main())
