"""Bounded HTTP discovery and private-interface TCP publication. No external URL fetching."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import ipaddress
import os
from pathlib import Path
import select
import socket
import socketserver
import ssl
import threading
import time
import psutil
from urllib.parse import urlsplit
from storage import read,write

PRIVATE=tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))
BLOCKED={21,22,23,25,53,110,135,139,143,389,445,465,587,993,995,1433,2375,2376,3306,3389,5432,5900,6379,9200,9300,11211,27017}
WEB_PORTS={80,443,3000,3001,3080,3081,4000,4173,4310,4321,5000,5173,8000,8001,8080,8081,8443,8888,9000,18000,18001,18080,18086,18787}
DENIED_NAMES={'controlcenter','rapportd','sharingd','uuremoteserver','ssh','adb','clash-verge','cloudflared'}

def private_ipv4(value):
    try:return any(ipaddress.ip_address(value) in net for net in PRIVATE)
    except ValueError:return False

def lan_addresses(interfaces):
    return list(dict.fromkeys(a for i in interfaces if i.get('up') and not i['name'].lower().startswith(('lo','docker','veth','br-','bridge','utun','awdl','llw','vmnet','vbox','virbr','tailscale','tun'))
        for a in i.get('addresses',[]) if private_ipv4(a)))

def url_for(scheme,host,port):
    return f"{scheme}://{'['+host+']' if ':' in host else host}:{port}/"

def validate_url(value):
    if not isinstance(value,str) or len(value)>2048 or any(ord(c)<32 for c in value):raise ValueError('访问地址无效')
    if not value:return ''
    p=urlsplit(value)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:raise ValueError('仅支持不含账号密码的 HTTP/HTTPS 地址')
    try:
        if p.port is not None and not 1<=p.port<=65535:raise ValueError()
    except ValueError:raise ValueError('访问端口无效')
    return value

def web_command(body,lan):
    command=body.get('command')
    if not isinstance(command,list) or not command:return body
    access=body.get('webAccess')
    if access not in (None,'lan','local'):raise ValueError('访问范围无效')
    # Existing generic commands retain their semantics unless a supported preset is explicitly selected.
    if access is None:return body
    flag=None;aliases=()
    if Path(command[0]).name.lower().startswith('python') and '-m' in command and command.index('-m')+1<len(command) and command[command.index('-m')+1]=='http.server':
        flag='--bind';aliases=('--bind','-b')
    elif any(Path(arg).name in ('vite','vite.js') for arg in command[:2]):flag='--host';aliases=('--host',)
    elif any(Path(arg).name=='next' for arg in command[:2]):flag='--hostname';aliases=('--hostname','-H')
    if not flag:raise ValueError('此命令没有已验证的局域网参数，请按程序自己的方式设置监听地址')
    host=lan[0] if access=='lan' and lan else '127.0.0.1'
    output=[];index=0
    while index<len(command):
        arg=command[index]
        if arg in aliases:index+=2 if index+1<len(command) and not command[index+1].startswith('-') else 1;continue
        if any(arg.startswith(a+'=') for a in aliases):index+=1;continue
        output.append(arg);index+=1
    output.extend([flag,host])
    return dict(body,command=output,webAccess=access,webBind=host)

def probe(host,port):
    schemes=('https','http') if port in (443,8443) else ('http','https')
    for scheme in schemes:
        connection=None
        try:
            connection=socket.create_connection((host,port),timeout=.25)
            if scheme=='https':
                # Detection only, no credentials. Real browser navigation retains certificate verification.
                connection=ssl._create_unverified_context().wrap_socket(connection,server_hostname=host)
            deadline=time.monotonic()+.35
            authority=('['+host+']' if ':' in host else host)+':'+str(port)
            connection.sendall(('HEAD / HTTP/1.1\r\nHost: '+authority+'\r\nUser-Agent: LocalServices-WebCheck\r\nConnection: close\r\n\r\n').encode())
            headers=b''
            while b'\r\n\r\n' not in headers and len(headers)<8192:
                remaining=deadline-time.monotonic()
                if remaining<=0:break
                connection.settimeout(remaining);chunk=connection.recv(min(2048,8192-len(headers)))
                if not chunk:break
                headers+=chunk
            if b'x-local-services: monitor' in headers.lower():return None
            first=headers.split(b'\r\n',1)[0].split()
            if len(first)>=2 and first[0].startswith(b'HTTP/1.') and first[1].isdigit() and 100<=int(first[1])<=599:return scheme
        except (OSError,ssl.SSLError):pass
        finally:
            if connection:connection.close()
    return None

class Discovery:
    def __init__(self):self.lock=threading.RLock();self.cache={};self.pending=set();self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='web-check')
    def update(self,endpoints,processes,own_ports):
        results=[];now=time.monotonic();scheduled=0
        with self.lock:
            live=set()
            for endpoint in endpoints:
                if endpoint.get('pid')==os.getpid():continue
                if endpoint['protocol']!='TCP' or endpoint['port'] in BLOCKED|set(own_ports):continue
                name=endpoint.get('name','').lower()
                if name in DENIED_NAMES:continue
                port=endpoint['port'];pid=endpoint.get('pid');created=processes.get(pid,{}).get('created',0)
                if port not in WEB_PORTS and not any(n in name for n in ('node','python','nginx','caddy','httpd','apache','java','dotnet','kuc','deepseek')):continue
                address=endpoint['address'];host='127.0.0.1' if address in ('*','0.0.0.0') else '::1' if address=='::' else address.split('%')[0]
                key=(host,port,pid,created);live.add(key)
                cached=self.cache.get(key)
                if (not cached or now-cached[0]>300) and key not in self.pending and scheduled<4:
                    self.pending.add(key);scheduled+=1
                    self.pool.submit(self.check,key)
                if cached and cached[1]:results.append(dict(endpoint,scheme=cached[1],created=created,probeHost=host))
            self.cache={k:v for k,v in self.cache.items() if k in live}
        return results
    def check(self,key):
        scheme=None;is_monitor=False
        try:
            args=psutil.Process(key[2]).cmdline() if key[2] else []
            is_monitor=any(Path(a).name in ('manager.py','agent.py') and Path(a).parent==Path(__file__).resolve().parent for a in args)
        except psutil.Error:pass
        if not is_monitor:scheme=probe(key[0],key[1])
        with self.lock:self.cache[key]=(time.monotonic(),scheme);self.pending.discard(key)

class RelayHandler(socketserver.BaseRequestHandler):
    def handle(self):
        if not private_ipv4(self.client_address[0]) and self.client_address[0]!='127.0.0.1':return
        try:
            pid,created=self.server.identity
            if abs(psutil.Process(pid).create_time()-created)>.01:return
        except psutil.Error:return
        if not self.server.slots.acquire(blocking=False):return
        upstream=None
        try:
            upstream=socket.create_connection(self.server.target,timeout=2)
            self.request.settimeout(5);upstream.settimeout(5);last=time.monotonic()
            while not self.server.stopping.is_set() and time.monotonic()-last<300:
                readable,_,_=select.select([self.request,upstream],[],[],1)
                for source in readable:
                    chunk=source.recv(32768)
                    if not chunk:return
                    (upstream if source is self.request else self.request).sendall(chunk);last=time.monotonic()
        except OSError:pass
        finally:
            if upstream:upstream.close()
            self.server.slots.release()

class RelayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address=True;daemon_threads=True

class Publications:
    def __init__(self):self.active={};self.errors={};self.lock=threading.RLock();self.slots=threading.BoundedSemaphore(16)
    def reconcile(self,web,lan,own_ports):
        configured=read('web-publications.json',{});bykey={entry['key']:entry for entry in web}
        with self.lock:
            self.errors={k:v for k,v in self.errors.items() if k in configured}
            for key,server in list(self.active.items()):
                item=configured.get(key);entry=bykey.get(key)
                valid=item and entry and lan and server.server_address[0]==lan[0] and item['pid']==entry.get('pid') and item['created']==entry.get('created')
                if not valid:server.stopping.set();server.shutdown();server.server_close();del self.active[key]
            for key,item in configured.items():
                if key in self.active:continue
                entry=bykey.get(key)
                if not lan:self.errors[key]='未发现可用局域网地址';continue
                if not entry or item['pid']!=entry.get('pid') or item['created']!=entry.get('created'):
                    self.errors[key]='目标进程已更换或尚未通过 Web 检测，请重新确认';continue
                try:
                    server=RelayServer((lan[0],item.get('port',0)),RelayHandler)
                    server.target=(entry['probeHost'],entry['port']);server.identity=(entry['pid'],entry['created']);server.stopping=threading.Event();server.slots=self.slots
                    threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.5},daemon=True).start()
                    self.active[key]=server;self.errors.pop(key,None)
                except OSError:self.errors[key]='发布端口被占用或当前网络地址不可用'
            return {key:dict(host=server.server_address[0],port=server.server_address[1]) for key,server in self.active.items()}
    def configure(self,body,web,lan,own_ports):
        key=body.get('key');enabled=body.get('enabled')
        if not isinstance(enabled,bool):raise ValueError('enabled 必须是布尔值')
        config=read('web-publications.json',{})
        if not enabled:config.pop(key,None)
        else:
            entry=next((e for e in web if e['key']==key),None)
            if not entry or not entry.get('pid'):raise ValueError('仅可发布已经确认进程归属的 Web 监听端口')
            if not lan:raise ValueError('未发现可用局域网地址')
            if not ipaddress.ip_address(entry['address']).is_loopback:raise ValueError('该服务已监听网络地址，请直接使用局域网访问链接')
            port=body.get('port',0)
            if isinstance(port,bool) or not isinstance(port,int) or port and not 1024<=port<=65535:raise ValueError('发布端口应为 1024–65535，或 0 自动分配')
            if port in BLOCKED|set(own_ports):raise ValueError('该端口不可用于局域网发布')
            if len(config)>=8 and key not in config:raise ValueError('最多同时发布 8 个 Web 服务')
            if key in self.active:raise ValueError('请先关闭现有发布再修改端口')
            config[key]=dict(pid=entry['pid'],created=entry['created'],port=port)
        write('web-publications.json',config)
        active=self.reconcile(web,lan,own_ports)
        if enabled and key not in active:
            config.pop(key,None);write('web-publications.json',config)
            raise ValueError(self.errors.get(key,'无法创建局域网入口'))
        if enabled and not config[key]['port']:
            config[key]['port']=active[key]['port'];write('web-publications.json',config)
        return active.get(key)
    def close(self):
        with self.lock:
            for server in self.active.values():server.stopping.set();server.shutdown();server.server_close()
            self.active.clear()

def attach(data,discovery,publications,processes,own_ports):
    lan=lan_addresses(data['network']['interfaces']);detected=discovery.update(data['security']['listeners'],processes,own_ports)
    entries=[];seen=set()
    for entry in detected:
        key=f"{entry.get('name','')}:{entry['port']}:{entry['address']}"
        if key in seen:continue
        seen.add(key);entry=dict(entry,key=key);entries.append(entry)
    active=publications.reconcile(entries,lan,own_ports)
    links=read('web-links.json',{});configured=read('web-publications.json',{})
    for service in data['services']:
        ports=set(service['ports']);service['web']=[]
        slots=set()
        for entry in sorted(entries,key=lambda e:(e['key'] not in links,e['key'] not in active,':' in e['probeHost'])):
            if (entry['port'],entry['scheme']) in slots:continue
            if entry['port'] not in ports or service.get('pid') and entry.get('pid')!=service['pid'] and service['kind']=='process':continue
            slots.add((entry['port'],entry['scheme']))
            descriptor=dict(entry,customUrl=links.get(entry['key'],''),publication=active.get(entry['key']),publicationConfigured=entry['key'] in configured,publicationError=publications.errors.get(entry['key']))
            service['web'].append(descriptor)
    data['webAccess']=dict(lanAddresses=lan,confirmed=entries,publications=active,probeInterval=300,defaultWebAccess='lan')
    return data
