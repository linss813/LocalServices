"""Console-side proxy. Keys stay on the console; no redirects or plaintext remote keys."""
import gzip
import ipaddress
import json
import secrets
import re
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request,build_opener,HTTPRedirectHandler,ProxyHandler
from urllib.error import HTTPError,URLError
from storage import read,write

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

class NodeError(ValueError):pass

def validate_url(url):
    parts=urlsplit(url)
    if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ('','/'):
        raise ValueError('节点地址应为 http(s)://主机:端口，不得包含账号、路径或查询参数')
    try:
        loopback=ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:loopback=parts.hostname=='localhost'
    if parts.scheme=='http' and not loopback:raise ValueError('非回环节点必须使用 HTTPS；也可通过 SSH 隧道连接本机转发端口')
    try:port=parts.port
    except ValueError:raise ValueError('端口无效')
    if port is not None and not 1<=port<=65535:raise ValueError('端口无效')
    return url.rstrip('/')

def access_host(value):
    if not isinstance(value,str):raise ValueError('访问主机无效')
    value=value.strip().strip('[]')
    if not value:return ''
    try:ipaddress.ip_address(value);return value
    except ValueError:pass
    if len(value)>253 or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?',value):raise ValueError('访问主机仅填写 IP 或域名，不包含协议或端口')
    return value

class Nodes:
    def __init__(self):self.cache={};self.node_locks={};self.lock=threading.RLock();self.opener=build_opener(NoRedirect,ProxyHandler({}))
    def list(self):return [dict(id=n['id'],name=n['name'],url=n['url'],accessHost=n.get('accessHost','')) for n in read('nodes.json',[])]
    def add(self,body):
        name=body.get('name','');token=body.get('token','');url=body.get('url','')
        if not isinstance(name,str) or not name.strip() or len(name)>80 or any(c in name for c in '\r\n'):raise ValueError('节点名称应为 1–80 字')
        if not isinstance(token,str) or not 24<=len(token)<=512 or any(c.isspace() for c in token):raise ValueError('请填写代理生成的有效令牌（至少 24 字符）')
        host=access_host(body.get('accessHost',''))
        node=dict(accessHost=host,id=secrets.token_hex(8),name=name.strip(),url=validate_url(url),token=token)
        self.request(node,'status') # Verify identity/permissions before saving.
        with self.lock:
            nodes=read('nodes.json',[])
            if len(nodes)>=32:raise ValueError('最多支持 32 个节点')
            if any(n['url']==node['url'] for n in nodes):raise ValueError('该节点地址已添加')
            write('nodes.json',nodes+[node])
        return node['id']
    def set_access(self,body):
        host=access_host(body.get('accessHost',''))
        with self.lock:
            items=read('nodes.json',[])
            item=next((n for n in items if n['id']==body.get('id')),None)
            if not item:raise NodeError('节点不存在')
            item['accessHost']=host;write('nodes.json',items)
    def remove(self,ident):
        with self.lock:
            write('nodes.json',[n for n in read('nodes.json',[]) if n['id']!=ident]);self.cache.pop(ident,None)
    def resolve(self,ident):
        node=next((n for n in read('nodes.json',[]) if n['id']==ident),None)
        if not node:raise NodeError('请选择有效监控节点')
        return node
    def request(self,node,path,body=None):
        req=Request(node['url']+'/agent/'+path,data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization':'Bearer '+node['token'],'Content-Type':'application/json','Accept-Encoding':'gzip'})
        try:
            with self.opener.open(req,timeout=8) as response:
                raw=response.read(8*1024*1024+1)
                if len(raw)>8*1024*1024:raise NodeError('节点响应过大')
                if response.headers.get('Content-Encoding')=='gzip':
                    import io
                    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:raw=gz.read(16*1024*1024+1)
                    if len(raw)>16*1024*1024:raise NodeError('节点响应过大')
                result=json.loads(raw)
                if not isinstance(result,dict):raise NodeError('节点响应格式无效')
                return result
        except HTTPError as exc:
            if exc.code in (401,403):raise NodeError('节点认证失败或当前节点未开放此能力')
            # Only propagate structured agent errors, never raw redirects/authorization headers.
            try:detail=json.loads(exc.read(4096)).get('error','请求被节点拒绝')
            except Exception:detail='请求被节点拒绝'
            raise NodeError(str(detail)[:500])
        except (URLError,OSError,ValueError) as exc:
            if isinstance(exc,NodeError):raise
            raise NodeError('无法连接监控节点，请检查服务、隧道和证书；未展示过期指标')
    def status(self,ident):
        with self.lock:lock=self.node_locks.setdefault(ident,threading.Lock())
        with lock:
            with self.lock:cached=self.cache.get(ident)
            if cached and time.monotonic()-cached[0]<2:
                return dict(cached[1],age=round(cached[1].get('age',0)+time.monotonic()-cached[0],1))
            result=self.request(self.resolve(ident),'status')
            with self.lock:self.cache[ident]=(time.monotonic(),result)
            return result
    def forward(self,ident,path,body):
        result=self.request(self.resolve(ident),path,body)
        with self.lock:self.cache.pop(ident,None)
        return result
