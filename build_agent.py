#!/usr/bin/env python3
"""Build an allowlisted monitor-only archive. Never includes local data or SMTP secrets."""
import argparse
from pathlib import Path
import zipfile

FILES=('agent.py','manager.py','monitor.py','backends.py','macos_backend.py','storage.py','catalog.py','mailer.py',
       'mail_template.py','nodes.py','service_runner.py','configure_email.py','requirements.txt','AGENT.md','volumes.py','web_access.py')

def build(output):
    root=Path(__file__).resolve().parent;output=Path(output).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as bundle:
        for name in FILES:bundle.write(root/name,'local-service-agent/'+name)
    return output

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='dist/local-service-agent.zip');args=parser.parse_args()
    print(build(args.output))
