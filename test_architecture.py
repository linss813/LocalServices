"""Real process tests using isolated data directories. No real email or service operations."""
import gzip
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from urllib.error import HTTPError
from unittest.mock import patch
import catalog
import mailer
import nodes
import storage

ROOT=Path(__file__).resolve().parent

def free_port():
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]

def request(url,body=None,headers=None):
    req=urllib.request.Request(url,data=json.dumps(body).encode() if body is not None else None,headers=headers or {})
    with urllib.request.urlopen(req,timeout=12) as r:
        raw=r.read()
        if r.headers.get('Content-Encoding')=='gzip':raw=gzip.decompress(raw)
        return json.loads(raw)

class Portable(unittest.TestCase):
    def test_remote_plaintext_rejected_and_loopback_allowed(self):
        for url in ('http://10.0.0.1:18767','https://a:b@example.com','https://example.com/private','http://127.0.0.1:3?q=x'):
            with self.assertRaises(ValueError):nodes.validate_url(url)
        self.assertEqual(nodes.validate_url('http://127.0.0.1:18767/'),'http://127.0.0.1:18767')
        self.assertEqual(nodes.validate_url('https://example.com:18767'),'https://example.com:18767')
    def test_catalog_preserves_unknown_and_escapable_name(self):
        known=catalog.describe('com.apple.WebKit.WebContent',aliases={})
        self.assertEqual(known['displayName'],'网页内容进程')
        unknown=catalog.describe('com.vendor.unknown',aliases={})
        self.assertEqual(unknown['displayName'],'com.vendor.unknown');self.assertIn('尚未',unknown['description'])
        override=catalog.describe('node','node',{'node':dict(name='<测试>',description='API')})
        self.assertEqual(override['displayName'],'<测试>')
    def test_subject_identifies_node_state_severity_category(self):
        m=mailer.Mailer(dict(id='test',name='生产服务器'))
        cfg=dict(host='test.invalid',port=465,encryption='ssl',**{'from':'a@example.com'},to='b@example.com')
        for state in ('触发','恢复','测试'):
            item=dict(state=state,title='CPU 超限',detail='95%',level='critical',category='CPU',target='知识库 API',time=time.time(),node=dict(id='test',name='生产服务器'))
            with patch.object(mailer.smtplib,'SMTP_SSL') as smtp:
                m.send(cfg,[item]);message=smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
                subject=message['Subject'];self.assertIn('['+state+']',subject);self.assertIn('[生产服务器]',subject);self.assertIn('[CPU]',subject);self.assertIn('知识库 API',subject)
                self.assertIn('严重' if state=='触发' else '正常' if state=='恢复' else '严重',subject)
    def test_batch_does_not_mix_recovery_node_or_category(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(storage,'DATA',Path(tmp)):
            storage.write('email.json',dict(enabled=True,enabledAt=0))
            m=mailer.Mailer(dict(id='a',name='A'))
            events=[dict(id='cpu',key='cpu',title='CPU',detail='95%',level='warning',time=1,recovered=2,node=dict(id='a',name='A')),
                    dict(id='mem',key='memory',title='Memory',detail='95%',level='warning',time=3,recovered=None,node=dict(id='b',name='B'))]
            m.enqueue_events(events)
            with patch.object(m,'send') as send:m.deliver()
            items=send.call_args.args[1];self.assertEqual(len(items),1)
            self.assertEqual(len(m.state['pending']),2)

class AgentConsole(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.agent_data=Path(cls.tmp.name)/'agent';cls.console_data=Path(cls.tmp.name)/'console'
        cls.ap=free_port();cls.cp=free_port();cls.ab=f'http://127.0.0.1:{cls.ap}';cls.cb=f'http://127.0.0.1:{cls.cp}'
        def spawn(mode,data,port):
            env=dict(os.environ,LOCAL_SERVICE_DATA=str(data))
            return subprocess.Popen([sys.executable,str(ROOT/'manager.py'),'--mode',mode,'--port',str(port),'--interval','5','--node-name','隔离测试节点'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        cls.agent=spawn('agent',cls.agent_data,cls.ap);cls.console=spawn('console',cls.console_data,cls.cp)
        try:
            for _ in range(100):
                try:
                    cls.key=(cls.agent_data/'agent-token').read_text().strip();cls.headers={'Authorization':'Bearer '+cls.key,'Content-Type':'application/json'}
                    snapshot=request(cls.ab+'/agent/status',headers=cls.headers)
                    page=urllib.request.urlopen(cls.cb).read().decode()
                    import re
                    cls.ui_token=re.search("const token='([^']+)'",page)[1]
                    if not snapshot.get('loading'):break
                except (OSError,HTTPError):pass
                time.sleep(.1)
            else:raise AssertionError('agents did not become ready')
            cls.ui_headers={'X-Manager-Token':cls.ui_token,'Content-Type':'application/json'}
            result=request(cls.cb+'/api/node-add',dict(name='远程测试节点',url=cls.ab,token=cls.key),cls.ui_headers);cls.node=result['id']
        except Exception:
            cls.tearDownClass();raise
    @classmethod
    def tearDownClass(cls):
        for process in (cls.agent,cls.console):
            if process.poll() is None:process.terminate();process.wait(timeout=10)
            if process.stderr:process.stderr.close()
        cls.tmp.cleanup()
    def test_authentication_and_no_ui_on_agent(self):
        with self.assertRaises(HTTPError) as caught:request(self.ab+'/agent/status')
        self.assertEqual(caught.exception.code,403)
        with self.assertRaises(HTTPError) as caught:request(self.ab+'/',headers=self.headers)
        self.assertEqual(caught.exception.code,404)
    def test_remote_status_and_private_token(self):
        listing=request(self.cb+'/api/nodes');self.assertNotIn(self.key,json.dumps(listing))
        result=request(self.cb+'/api/status?node='+self.node)
        self.assertEqual(result['node']['name'],'隔离测试节点');self.assertFalse(result['capabilities']['control']);self.assertIn('collectMs',result['monitor'])
        self.assertFalse((self.console_data/'node.json').exists(),'console must not instantiate a local monitor')
    def test_no_remote_service_control_by_default(self):
        with self.assertRaises(HTTPError) as caught:request(self.ab+'/agent/action',dict(id='invalid',action='start'),self.headers)
        self.assertIn('只读',json.load(caught.exception)['error'])
    def test_proxy_alias_roundtrip_and_credential_not_exposed(self):
        d=request(self.ab+'/agent/status',headers=self.headers);group=d['resources'][0]
        request(self.cb+'/api/alias?node='+self.node,dict(id=group['id'],name='自定义 <测试> 名称',description='这是隔离测试说明'),self.ui_headers)
        current=request(self.cb+'/api/status?node='+self.node)
        actual=next(g for g in current['resources'] if g['id']==group['id'])
        self.assertEqual(actual['displayName'],'自定义 <测试> 名称');self.assertNotIn(self.key,json.dumps(current))
    def test_gzip_snapshot_and_cache(self):
        req=urllib.request.Request(self.ab+'/agent/status',headers=dict(self.headers,**{'Accept-Encoding':'gzip'}))
        with urllib.request.urlopen(req) as r:
            raw=r.read();self.assertEqual(r.headers.get('Content-Encoding'),'gzip');self.assertGreater(len(gzip.decompress(raw)),len(raw))
    def test_duplicate_monitor_rejected(self):
        result=subprocess.run([sys.executable,str(ROOT/'agent.py'),'--port',str(free_port())],env=dict(os.environ,LOCAL_SERVICE_DATA=str(self.agent_data)),capture_output=True,text=True,timeout=10)
        self.assertNotEqual(result.returncode,0);self.assertIn('已有监控实例',result.stderr)
    def test_z_console_shutdown_does_not_stop_agent(self):
        before=request(self.ab+'/agent/status',headers=self.headers)['sampledAt']
        self.console.terminate();self.console.wait(timeout=10);time.sleep(5.3)
        after=request(self.ab+'/agent/status',headers=self.headers)
        self.assertGreater(after['sampledAt'],before);self.assertIsNone(after['error'])

if __name__=='__main__':unittest.main()
