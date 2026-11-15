"""SMTP outbox, background delivery and persisted deduplication. Secrets never leave this module."""
from email.message import EmailMessage
import smtplib
import ssl
import threading
import socket
import time
from storage import read,write
from mail_template import render

def category(key):
    return {'cpu':'CPU','memory':'内存','rss':'服务内存','disk':'磁盘空间','network':'网络流量','io':'磁盘 I/O','offline':'服务离线','exposure':'端口绑定','firewall':'防火墙'}.get(key.split(':',1)[0],'监控')

class Mailer:
    def __init__(self,node=None):
        self.node=node or dict(id=socket.gethostname(),name=socket.gethostname())
        self.lock=threading.RLock();self.wake=threading.Event()
        self.state=read('mail-state.json',dict(sent=[],pending=[],last=None))
        self.state.setdefault('sent',[]);self.state.setdefault('pending',[])
        self.inflight=False;self.last_attempt=0
    def start(self):threading.Thread(target=self.loop,daemon=True).start()
    def public(self):
        cfg=read('email.json',{})
        with self.lock:
            return dict(enabled=bool(cfg.get('enabled')),host=cfg.get('host',''),port=cfg.get('port'),recipient=cfg.get('to',''),
                sender=cfg.get('from',''),encryption=cfg.get('encryption','none'),pending=len(self.state['pending']),
                last=self.state.get('last'),sending=self.inflight)
    def enqueue_events(self,events):
        cfg=read('email.json',{})
        if not cfg.get('enabled'):return
        with self.lock:
            changed=False
            known=set(self.state['sent'])|{x['key'] for x in self.state['pending']}
            for event in events:
                if event['time']<cfg.get('enabledAt',0):continue
                for state,stamp in [('触发',event['time']),('恢复',event.get('recovered'))]:
                    if not stamp:continue
                    key=event['id']+':'+state
                    if key in known:continue
                    self.state['pending'].append(dict(key=key,title=event['title'],detail=('触发时信息（当前条件已解除）：\n' if state=='恢复' else '')+event['detail'],node=event.get('node',self.node),category=category(event.get('key','')),target=event.get('target',''),state=state,time=stamp,level=event['level'],attempts=0,next=0))
                    known.add(key);changed=True
            if not changed:return
            self.state['pending']=self.state['pending'][-200:]
            write('mail-state.json',self.state)
        self.wake.set()
    def test(self):
        cfg=read('email.json',{})
        if not cfg.get('enabled'):raise ValueError('邮箱尚未配置')
        with self.lock:
            if any(e['state']=='测试' for e in self.state['pending']):raise ValueError('测试邮件正在排队或重试，请查看发送状态')
            self.state['pending'].append(dict(key='test:'+str(time.time_ns()),title='本地服务管理器 · 邮箱告警测试',detail='邮箱告警已配置。收到此信表示 SMTP 投递成功；实际入箱仍取决于邮件服务商。',node=dict(self.node),category='邮件通道',state='测试',time=time.time(),level='info',attempts=0,next=0))
            write('mail-state.json',self.state)
        self.wake.set()
    def send(self,cfg,items):
        message=EmailMessage();message['From']=cfg['from'];message['To']=cfg['to']
        first=items[0];node=first.get('node',self.node)
        state=first['state'];level='正常' if state=='恢复' else '严重' if first['level']=='critical' else '注意' if first['level']=='warning' else '信息'
        clean=lambda value:str(value).replace('\r',' ').replace('\n',' ')[:90]
        message['Subject']=f"[{clean(state)}][{level}][{clean(node['name'])}][{clean(first.get('category','监控'))}] {clean((first.get('target','')+' · ' if first.get('target') else '')+first['title'])}"+(f" · {len(items)} 条" if len(items)>1 else '')
        html,plain=render(items,hostname=node['name'],zone=cfg.get('timezone','Asia/Shanghai'))
        message.set_content(plain)
        message.add_alternative(html,subtype='html')
        smtp_cls=smtplib.SMTP_SSL if cfg.get('encryption')=='ssl' else smtplib.SMTP
        kwargs=dict(host=cfg['host'],port=int(cfg['port']),local_hostname=cfg.get('hello') or None,timeout=12)
        if cfg.get('encryption')=='ssl':kwargs['context']=ssl.create_default_context()
        with smtp_cls(**kwargs) as smtp:
            smtp.ehlo()
            if cfg.get('encryption')=='starttls':smtp.starttls(context=ssl.create_default_context());smtp.ehlo()
            if cfg.get('username'):smtp.login(cfg['username'],cfg['password'])
            smtp.send_message(message)
    def deliver(self):
        cfg=read('email.json',{})
        if not cfg.get('enabled'):return
        now=time.time()
        with self.lock:
            # Batch all ready incidents; at most one automatic delivery per minute.
            ready=[dict(x) for x in self.state['pending'] if x['next']<=now]
            ready.sort(key=lambda x:(0 if x['state']=='测试' else 1 if x['level']=='critical' else 2,x['time']))
            def group(x):return (x.get('node',self.node)['id'],x['state'],x.get('category','监控'),x['level'])
            items=[x for x in ready if group(x)==group(ready[0])] if ready else []
            if not items or self.inflight or (now-self.last_attempt<60 and not any(x['state']=='测试' for x in items)):return
            self.inflight=True;self.last_attempt=now
        error=None
        try:self.send(cfg,items)
        except Exception as exc:
            # Do not persist raw SMTP server text or exception args containing credentials.
            if isinstance(exc,smtplib.SMTPAuthenticationError):error='认证失败，请检查 SMTP 授权码及服务商设置'
            elif isinstance(exc,(TimeoutError,OSError)):error='连接超时或网络不可达，请检查端口、防火墙与 SMTP 服务器'
            elif isinstance(exc,smtplib.SMTPException):error='SMTP 投递失败：'+type(exc).__name__
            else:error='邮件发送失败：'+type(exc).__name__
        with self.lock:
            keys={x['key'] for x in items}
            if error:
                for item in self.state['pending']:
                    if item['key'] in keys:
                        item['attempts']+=1;item['next']=time.time()+min(3600,60*2**min(item['attempts']-1,6))
            else:
                self.state['sent']=(self.state['sent']+list(keys))[-2000:]
                self.state['pending']=[x for x in self.state['pending'] if x['key'] not in keys]
            self.state['last']=dict(time=time.time(),ok=not bool(error),detail=error or f'SMTP 服务器已接受 {len(items)} 条通知',count=len(items))
            self.inflight=False;write('mail-state.json',self.state)
    def loop(self):
        while True:
            self.wake.wait(15);self.wake.clear()
            try:self.deliver()
            except Exception:pass
