"""Integration test: operates only its own temporary service."""
import json
import os
import tempfile
import re
import socket
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

BASE=os.environ.get('TEST_MANAGER_URL','http://127.0.0.1:18765')
page=urllib.request.urlopen(BASE).read().decode()
token=re.search("const token='([^']+)'",page)[1]
node_list=json.load(urllib.request.urlopen(BASE+'/api/nodes'))['nodes']
node=os.environ.get('TEST_MANAGER_NODE',node_list[0]['id'] if node_list else 'local')
def request(path,body=None,auth=True):
    headers={'Content-Type':'application/json'}
    if auth: headers['X-Manager-Token']=token
    req=urllib.request.Request(BASE+'/api/'+path+'?node='+urllib.parse.quote(node),data=json.dumps(body).encode() if body else None,headers=headers)
    return json.load(urllib.request.urlopen(req,timeout=60))
def wait(predicate):
    for _ in range(80):
        data=request('status')
        if predicate(data): return data
        time.sleep(.4)
    raise AssertionError('服务状态未达到预期')
with socket.socket() as sock:
    sock.bind(('127.0.0.1',0))
    port=sock.getsockname()[1]
name='Manager integration '+str(port)
ident=None
try:
    try:
        request('create',{'name':name},auth=False)
        raise AssertionError('缺少 token 未拒绝')
    except urllib.error.HTTPError as exc:
        assert exc.code==403
    request('create',dict(name=name,cwd=tempfile.gettempdir(),command=['/usr/bin/python3' if sys.platform=='darwin' else sys.executable,'-u','-m','http.server',str(port),'--bind','127.0.0.1']))
    wait(lambda d:any(s['name']==name for s in d.get('services',[])))
    service=next(s for s in request('status')['services'] if s['name']==name)
    ident=service['id']
    def act(op,**kw): request('action',dict(id=ident,action=op,**kw))
    def ours(d): return next(s for s in d['services'] if s['id']==ident)
    act('start')
    wait(lambda d: port in ours(d)['ports'])
    urllib.request.urlopen(f'http://127.0.0.1:{port}').read()
    assert 'GET' in request('logs',{'id':ident})['text']
    act('autostart',enabled=True)
    wait(lambda d:ours(d)['autostart'])
    act('autostart',enabled=False)
    wait(lambda d:not ours(d)['autostart'])
    act('restart')
    wait(lambda d: port in ours(d)['ports'])
    act('stop')
    wait(lambda d: not ours(d)['pid'])
    act('start')
    wait(lambda d: port in ours(d)['ports'])
    print('PASS: token validation, registration, start, ports, logs, autostart toggle, restart, stop, start again')
finally:
    if ident:
        request('action',dict(id=ident,action='delete'))
        label=ident.split(':',1)[1]
        from storage import DATA
        (DATA/(label+'.log')).unlink(missing_ok=True)
        wait(lambda d:not any(s['id']==ident for s in d['services']))
        print('PASS: removal and temporary service cleanup')
