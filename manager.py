#!/usr/bin/env python3
"""Console, standalone monitor agent, or combined local mode. No implicit elevation."""
import argparse
import gzip
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs
import backends
import catalog
import web_access
from monitor import Monitor
from nodes import Nodes
from storage import DATA,read,write,audit

ROOT=Path(__file__).resolve().parent
CONTROL={'action','create','logs','web-publish'}
ROUTES=CONTROL|{'rules','watch','ack','email-test','alias','web-link'}

class APIError(ValueError):pass

def local_operation(monitor,path,body,allow_control):
    if path in CONTROL and not allow_control:raise APIError('该节点为只读监控；启停与日志访问需在代理显式启用 --allow-control')
    created_bind=None
    if path=='action':backends.action(body)
    elif path=='create':
        lan=(monitor.data or {}).get('webAccess',{}).get('lanAddresses',[])
        prepared=web_access.web_command(body,lan);created_bind=prepared.get('webBind');backends.create(prepared)
    elif path=='rules':
        with monitor.lock:monitor.alerts.configure(body)
    elif path=='watch':
        if not isinstance(body.get('enabled'),bool):raise ValueError('enabled 必须是布尔值')
        with monitor.lock:
            rules=dict(monitor.alerts.rules);watched=set(rules['watched'])
            if body['enabled']:
                if body.get('id') not in [s['id'] for s in (monitor.data or {}).get('services',[]) if s['kind'] not in ('process','socket')]:raise ValueError('仅可监控已识别的服务')
                watched.add(body['id'])
            else:watched.discard(body.get('id'))
            rules['watched']=sorted(watched);monitor.alerts.configure(rules)
    elif path=='ack':
        with monitor.lock:monitor.alerts.acknowledge(body.get('id'))
    elif path=='email-test':monitor.mailer.test()
    elif path=='alias':
        with monitor.lock:
            data=monitor.data or {};valid={s.get('aliasId',s['id']) for s in data.get('services',[])}|{g['id'] for g in data.get('resources',[])}
            catalog.set_alias(body,valid);catalog.annotate(data)
    elif path=='web-link':
        entries=(monitor.data or {}).get('webAccess',{}).get('confirmed',[])
        if body.get('key') not in [e['key'] for e in entries]:raise ValueError('Web 端口尚未识别或已停止')
        links=read('web-links.json',{});url=web_access.validate_url(body.get('url',''))
        if url:links[body['key']]=url
        else:links.pop(body['key'],None)
        write('web-links.json',links)
        with monitor.lock:monitor.refresh_web()
    elif path=='web-publish':
        with monitor.lock:
            access=(monitor.data or {}).get('webAccess',{})
            entry=next((e for e in access.get('confirmed',[]) if e['key']==body.get('key')),None)
            if body.get('enabled') is True and (not entry or body.get('pid')!=entry.get('pid') or body.get('created')!=entry.get('created')):raise ValueError('目标进程发生变化，请刷新后重新确认发布')
            monitor.publications.configure(body,access.get('confirmed',[]),access.get('lanAddresses',[]),monitor.own_ports)
            monitor.refresh_web()
    elif path=='logs':
        item=next((s for s in backends.configs() if 'custom:'+s['label']==body.get('id')),None)
        if not item:raise ValueError('仅支持自定义服务日志')
        path=DATA/(item['label']+'.log');content='暂无日志'
        if path.exists():
            with path.open('rb') as f:f.seek(max(0,path.stat().st_size-32768));content=f.read().decode(errors='replace') or content
        return {'text':content}
    else:raise ValueError('未知操作')
    if path in CONTROL:
        monitor.invalidate();monitor.detail_times.pop('sockets',None)
    audit(path+':'+str(body.get('action','')),body.get('id',''))
    return dict(ok=True,webBind=created_bind) if path=='create' else {'ok':True}

class Handler(BaseHTTPRequestHandler):
    def setup(self):super().setup();self.connection.settimeout(20)
    def reply(self,status,data,mime='application/json'):
        raw=json.dumps(data,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode() if mime=='application/json' else data.encode()
        zipped='gzip' in self.headers.get('Accept-Encoding','') and len(raw)>4096
        if zipped:raw=gzip.compress(raw,compresslevel=1)
        self.send_response(status);self.send_header('Content-Type',mime+'; charset=utf-8')
        self.send_header('X-Local-Services','monitor');self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        if zipped:self.send_header('Content-Encoding','gzip')
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'unsafe-inline' 'self'; style-src 'unsafe-inline' 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        if self.command!='HEAD':self.wfile.write(raw)
    def is_agent(self):return self.server.mode=='agent'
    def authorize(self,write_request=False):
        if self.is_agent():
            return secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.agent_token)
        valid=self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}',f'[::1]:{self.server.server_port}')
        return valid and self.headers.get('Sec-Fetch-Site')!='cross-site' and (not write_request or secrets.compare_digest(self.headers.get('X-Manager-Token',''),self.server.ui_token))
    def local_status(self):
        result=self.server.monitor.get();result['node']=dict(self.server.monitor.node)
        result['capabilities']=dict(result.get('capabilities',{}),control=self.server.allow_control,pythonExecutable=getattr(__import__('sys'),'_base_executable',__import__('sys').executable))
        return result
    def do_HEAD(self):self.reply(405,{'error':'Use GET'})
    def do_GET(self):
        if not self.authorize():return self.reply(403,{'error':'认证或本机访问校验失败'})
        parsed=urlsplit(self.path);path=parsed.path;query=parse_qs(parsed.query)
        try:
            if self.is_agent():
                if path=='/agent/status':return self.reply(200,self.local_status())
                return self.reply(404,{'error':'监控代理不提供控制台页面'})
            if path=='/':return self.reply(200,(ROOT/'index.html').read_text(encoding='utf-8').replace('__TOKEN__',self.server.ui_token),'text/html')
            if path in ('/app.js','/style.css'):return self.reply(200,(ROOT/path[1:]).read_text(encoding='utf-8'),'application/javascript' if path.endswith('.js') else 'text/css')
            if path=='/api/nodes':
                nodes=self.server.nodes.list()
                if self.server.monitor:nodes.insert(0,dict(id='local',name=self.server.monitor.node['name'],url='本机内置监控'))
                return self.reply(200,dict(nodes=nodes,mode=self.server.mode))
            if path=='/api/status':
                ident=query.get('node',['local'])[0]
                if ident=='local' and self.server.monitor:
                    result=self.local_status();result['accessContext']=dict(viewerLocal=True,accessHost='');return self.reply(200,result)
                result=dict(self.server.nodes.status(ident));node=self.server.nodes.resolve(ident)
                result['accessContext']=dict(viewerLocal=result.get('node',{}).get('id')==read('node.json',{}).get('id'),accessHost=node.get('accessHost',''))
                return self.reply(200,result)
            self.reply(404,{'error':'未找到'})
        except Exception as exc:self.reply(502,{'error':str(exc)})
    def do_POST(self):
        if not self.authorize(True):return self.reply(403,{'error':'请求校验失败，请检查令牌或刷新页面'})
        body={}
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=32768:raise ValueError('请求大小无效')
            body=json.loads(self.rfile.read(length))
            if not isinstance(body,dict):raise ValueError('请求必须为对象')
            parsed=urlsplit(self.path);path=parsed.path.rsplit('/',1)[-1]
            prefix='/agent/' if self.is_agent() else '/api/'
            if not parsed.path.startswith(prefix):return self.reply(404,{'error':'未找到'})
            with self.server.operations:
                if not self.is_agent() and path=='node-add':return self.reply(200,{'id':self.server.nodes.add(body)})
                if not self.is_agent() and path=='node-access':self.server.nodes.set_access(body);return self.reply(200,{'ok':True})
                if not self.is_agent() and path=='node-remove':self.server.nodes.remove(body.get('id'));return self.reply(200,{'ok':True})
                if path not in ROUTES:return self.reply(404,{'error':'未找到'})
                ident=parse_qs(parsed.query).get('node',['local'])[0]
                if self.is_agent() or ident=='local' and self.server.monitor:
                    result=local_operation(self.server.monitor,path,body,self.server.allow_control)
                else:result=self.server.nodes.forward(ident,path,body)
            self.reply(200,result)
        except Exception as exc:
            audit('request:'+urlsplit(self.path).path,body.get('id','') if isinstance(body,dict) else '',outcome='失败：'+type(exc).__name__)
            self.reply(400,{'error':str(exc)})
    def log_message(self,*args):pass

def node_identity(name=None):
    value=read('node.json',{})
    if not value:value=dict(id=secrets.token_hex(8),name=name or socket.gethostname());write('node.json',value)
    if name and name!=value['name']:value['name']=name;write('node.json',value)
    return value

def agent_key():
    path=DATA/'agent-token'
    if not path.exists():
        with path.open('x',encoding='utf-8') as f:f.write(secrets.token_urlsafe(36))
    if os.name!='nt':path.chmod(0o600)
    key=path.read_text(encoding='utf-8').strip()
    if len(key)<24:raise ValueError('代理令牌长度不足，请重新生成 agent-token')
    return key

def monitor_lock():
    """Keep one collector per data directory, including across combined/agent modes."""
    handle=(DATA/'monitor.lock').open('a+b')
    try:
        if os.name=='nt':
            import msvcrt
            if handle.tell()==0:handle.write(b'0');handle.flush()
            handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        handle.close();raise ValueError('该数据目录已有监控实例，请使用 console 模式连接它，或使用独立 LOCAL_SERVICE_DATA')
    return handle

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode',choices=('all','agent','console'),default='all')
    parser.add_argument('--port',type=int);parser.add_argument('--listen',default='127.0.0.1')
    parser.add_argument('--interval',type=int,default=10);parser.add_argument('--node-name')
    parser.add_argument('--allow-control',action='store_true');parser.add_argument('--tls-cert');parser.add_argument('--tls-key')
    parser.add_argument('--local-agent',help='控制台连接同数据目录中的本机代理，例如 http://127.0.0.1:18767')
    args=parser.parse_args()
    if not 5<=args.interval<=300:parser.error('采集间隔应为 5–300 秒')
    try:loopback=ipaddress.ip_address(args.listen).is_loopback
    except ValueError:parser.error('--listen 必须为本机 IP 地址')
    if not loopback and args.mode!='agent':parser.error('控制台只允许监听回环地址')
    if not loopback and not (args.tls_cert and args.tls_key):parser.error('代理监听非回环地址时必须提供 TLS 证书；或使用默认地址配合 SSH 隧道')
    if bool(args.tls_cert)!=bool(args.tls_key):parser.error('TLS 证书和密钥需要同时提供')
    DATA.mkdir(parents=True,exist_ok=True)
    if os.name!='nt':DATA.chmod(0o700)
    server_cls=ThreadingHTTPServer
    if ':' in args.listen:
        class IPv6Server(ThreadingHTTPServer):address_family=socket.AF_INET6
        server_cls=IPv6Server
    server=server_cls((args.listen,args.port or (18767 if args.mode=='agent' else 18765)),Handler)
    server.mode=args.mode;server.ui_token=secrets.token_urlsafe(32);server.nodes=Nodes();server.operations=threading.Lock()
    server.allow_control=args.mode=='all' or args.allow_control
    try:server.monitor_lock=monitor_lock() if args.mode!='console' else None
    except ValueError as exc:server.server_close();parser.error(str(exc))
    server.monitor=Monitor(args.interval,node_identity(args.node_name)) if args.mode!='console' else None
    if args.mode=='agent':server.agent_token=agent_key()
    if args.tls_cert:
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert,args.tls_key);server.socket=context.wrap_socket(server.socket,server_side=True)
    if server.monitor:server.monitor.own_ports.add(server.server_port);server.monitor.start()
    if args.local_agent:
        if args.mode!='console':parser.error('--local-agent 仅适用于 console 模式')
        if not any(n['url']==args.local_agent.rstrip('/') for n in server.nodes.list()):
            server.nodes.add(dict(name=args.node_name or '本机监控',url=args.local_agent,token=agent_key()))
    print(f'Local Services [{args.mode}]: {"https" if args.tls_cert else "http"}://{args.listen}:{server.server_port}',flush=True)
    if args.mode=='agent':print(f'代理令牌文件：{DATA / "agent-token"}（不会在日志或网页回显）',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        if server.monitor:server.monitor.close()
        server.server_close()
        if server.monitor_lock:server.monitor_lock.close()

if __name__=='__main__':main()
