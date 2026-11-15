"""Background telemetry and local alert engine. Rate deltas use monotonic time."""
from collections import Counter, deque
import ipaddress
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import psutil
import backends
from storage import SYSTEM, DATA, read, write, audit_tail
from mailer import Mailer
from catalog import annotate

DEFAULT_RULES=dict(cpu=90,memory=90,disk=90,networkMB=100,diskMB=200,serviceMemoryMB=2048,
                   sustain=30,exposure=True,firewall=True,watched=[])

class Alerts:
    def __init__(self):
        self.rules=dict(DEFAULT_RULES,**read('rules.json',{}))
        self.events=deque(read('alerts.json',[])[-200:],maxlen=200)
        self.active={e['key']:e for e in self.events if not e.get('recovered')}
        self.pending={key:time.monotonic() for key in self.active}
    def configure(self,body):
        values={}
        for key,lo,hi in [('cpu',1,100),('memory',1,100),('disk',1,100),('networkMB',.01,1000000),('diskMB',.01,1000000),('serviceMemoryMB',1,10000000),('sustain',0,3600)]:
            value=body.get(key)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not lo<=value<=hi:raise ValueError(f'{key} 必须在 {lo} 到 {hi} 之间')
            values[key]=value
        for key in ('exposure','firewall'):
            if not isinstance(body.get(key),bool):raise ValueError(f'{key} 必须是布尔值')
            values[key]=body[key]
        watched=body.get('watched',[])
        if not isinstance(watched,list) or len(watched)>500 or not all(isinstance(x,str) and len(x)<300 for x in watched):raise ValueError('服务监控列表无效')
        values['watched']=list(dict.fromkeys(watched))
        self.rules=values;self.pending={key:time.monotonic() for key in self.active};write('rules.json',values)
    def acknowledge(self,ident):
        for event in self.events:
            if event['id']==ident:
                event['acknowledged']=True;write('alerts.json',list(self.events));return
        raise ValueError('告警不存在')
    def evaluate(self,snapshot,now=None,wall=None):
        now=time.monotonic() if now is None else now
        wall=time.time() if wall is None else wall
        rules=self.rules;conditions={};observed=set()
        def condition(key,trigger,title,detail,level='warning',immediate=False,target=''):
            observed.add(key)
            if trigger:conditions[key]=(title,detail,level,0 if immediate else rules['sustain'],target)
        h=snapshot['host']
        condition('cpu',h['cpu']>=rules['cpu'],'CPU 持续高占用',f"主机 CPU {h['cpu']:.1f}% ≥ {rules['cpu']}%")
        condition('memory',h['memoryPercent']>=rules['memory'],'内存持续高占用',f"内存 {h['memoryPercent']:.1f}% ≥ {rules['memory']}%")
        for disk in snapshot['disks']:
            if not disk.get('alertable',True) and 'disk:'+disk['mount'] in self.active:
                event=self.active['disk:'+disk['mount']];event['suppressed']=True;event['detail']='只读系统镜像不适用容量告警；已排除此项，原先的 100% 不是可写磁盘耗尽。'
            condition('disk:'+disk['mount'],disk.get('alertable',True) and disk['percent']>=rules['disk'],'磁盘空间不足',f"{disk['mount']} 使用 {disk['percent']}%",immediate=True,target=disk['mount'])
        net=snapshot['network']
        rate=net['recvRate']+net['sentRate'] if net['recvRate'] is not None else None
        if rate is not None:condition('network',rate>=rules['networkMB']*1048576,'网络流量持续偏高',f"收发合计 {(rate or 0)/1048576:.2f} MB/s")
        io=snapshot['diskIO']
        rate=io['readRate']+io['writeRate'] if io['readRate'] is not None else None
        if rate is not None:condition('io',rate>=rules['diskMB']*1048576,'磁盘 I/O 持续偏高',f"读写合计 {(rate or 0)/1048576:.2f} MB/s")
        for group in snapshot['resources']:
            if group['kind']=='process':continue
            condition('rss:'+group['id'],group['memory']>=rules['serviceMemoryMB']*1048576,'服务内存持续偏高',f"{group.get('displayName',group['name'])} ({group['name']}) · {group['memory']/1048576:.0f} MB",target=group.get('displayName',group['name']))
        byid={s['id']:s for s in snapshot['services']}
        for s in snapshot['services']:
            if s['kind']!='process':observed.add('rss:'+s['id'])
        for key in self.pending:
            if key.startswith('offline:') and key[8:] not in rules['watched']:observed.add(key)
        for ident in rules['watched']:
            service=byid.get(ident)
            # Missing native entries may mean discovery failed, not an offline service.
            if service:
                condition('offline:'+ident,not service['running'],'被监控服务已离线',service.get('displayName',service['name'])+' ('+service['name']+')','critical',target=service.get('displayName',service['name']))
        sensitive=[p for p in snapshot['security']['exposed'] if p['port'] in (21,22,23,445,1433,2375,3306,3389,5432,5900,6379,9200,27017)]
        condition('exposure',rules['exposure'] and bool(sensitive),'敏感端口绑定非回环地址',', '.join(f"{p['address']}:{p['port']} ({p['name']})" for p in sensitive[:8]),immediate=True)
        if not rules['firewall'] or snapshot['security']['firewall']['state']!='unknown':condition('firewall',rules['firewall'] and snapshot['security']['firewall']['state']=='disabled','系统防火墙未启用',snapshot['security']['firewall']['detail'],immediate=True)
        changed=False
        for key,(title,detail,level,delay,target) in conditions.items():
            start=self.pending.setdefault(key,now)
            if key not in self.active and now-start>=delay:
                event=dict(id=f'{time.time_ns()}-{len(self.events)}',key=key,title=title,detail=detail,level=level,target=target,
                    time=wall,recovered=None,acknowledged=False)
                self.events.append(event);self.active[key]=event;changed=True
        for key in list(self.pending):
            if key not in observed and key not in self.active:
                del self.pending[key]
                continue
            if key not in conditions and key in observed:
                del self.pending[key]
                if key in self.active:
                    self.active.pop(key)['recovered']=wall;changed=True
        if changed:write('alerts.json',list(self.events))
        return sorted(self.events,key=lambda x:x['time'],reverse=True)

def safe_rate(current,previous,elapsed):
    if current is None or previous is None or elapsed<=0:return None
    return max(0,(current-previous)/elapsed)

def external(address):
    if address in ('*','0.0.0.0','::'):return True
    try:return not ipaddress.ip_address(address.split('%')[0]).is_loopback
    except ValueError:return False

def sockets():
    endpoints=[];states=Counter();warnings=[]
    try:
        connections=psutil.net_connections(kind='inet')
        for conn in connections:
            states[conn.status]+=1
            if conn.laddr and (conn.status==psutil.CONN_LISTEN or conn.type==socket.SOCK_DGRAM):
                endpoints.append(dict(pid=conn.pid,address=conn.laddr.ip,port=conn.laddr.port,protocol='UDP' if conn.type==socket.SOCK_DGRAM else 'TCP'))
        return endpoints,dict(states),True,warnings
    except (psutil.Error,OSError) as exc:
        if SYSTEM!='Darwin':return [],{},False,[f'无法读取连接信息（权限或系统限制）：{exc}']
    try:
        result=subprocess.run(['/usr/sbin/lsof','-nP','-iTCP','-sTCP:LISTEN','-iUDP','-FpnP'],capture_output=True,text=True,timeout=10)
        pid=None;protocol='TCP'
        for line in result.stdout.splitlines():
            if line.startswith('p') and line[1:].isdigit():pid=int(line[1:])
            elif line.startswith('P'):protocol=line[1:]
            elif line.startswith('n') and ':' in line:
                host,port=line[1:].rsplit(':',1)
                if port.isdigit():endpoints.append(dict(pid=pid,address=host.strip('[]'),port=int(port),protocol=protocol))
        if result.returncode not in (0,1):warnings.append('lsof 端口读取不完整：'+result.stderr[-200:])
        warnings.append('macOS 当前权限仅提供可见监听端口；连接总数不可用。')
        return endpoints,{},False,warnings
    except (OSError,subprocess.SubprocessError) as exc:return [],{},False,[f'端口发现不可用：{exc}']

def firewall():
    try:
        if SYSTEM=='Darwin':
            out=backends.run(['/usr/libexec/ApplicationFirewall/socketfilterfw','--getglobalstate'],timeout=5)
            state='disabled' if 'disabled' in out.lower() else 'enabled' if 'enabled' in out.lower() else 'unknown'
            return dict(state=state,detail=out.strip(),scope='macOS 应用防火墙')
        if SYSTEM=='Windows':
            out=backends.run(['powershell.exe','-NoProfile','-NonInteractive','-Command','Get-NetFirewallProfile | Select-Object Name,Enabled | ConvertTo-Json -Compress'],timeout=8)
            profiles=json.loads(out);profiles=profiles if isinstance(profiles,list) else [profiles]
            disabled=[p['Name'] for p in profiles if p['Enabled'] in (False,0,'False','false')]
            known=all(p['Enabled'] in (True,1,'True','true',False,0,'False','false') for p in profiles)
            return dict(state='disabled' if disabled else 'enabled' if known and profiles else 'unknown',detail='关闭的配置：'+', '.join(disabled) if disabled else '所有配置文件已启用' if known and profiles else '存在无法确认的配置',scope='Windows Defender 防火墙配置')
        if SYSTEM=='Linux':
            if shutil_which('ufw'):
                out=backends.run(['ufw','status'],timeout=5)
                return dict(state='disabled' if 'inactive' in out else 'enabled' if 'Status: active' in out else 'unknown',detail=out[:400],scope='UFW（不代表全部过滤规则）')
            if shutil_which('firewall-cmd'):
                out=backends.run(['firewall-cmd','--state'],timeout=5)
                return dict(state='enabled' if out.strip()=='running' else 'unknown',detail=out.strip(),scope='firewalld')
        return dict(state='unknown',detail='未发现可读取的防火墙管理器；未审计底层过滤规则。',scope='系统防火墙')
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        return dict(state='unknown',detail='读取失败或权限不足：'+str(exc)[:250],scope='系统防火墙')

def shutil_which(name):
    import shutil
    return shutil.which(name)

class Monitor:
    def __init__(self,interval=10,node=None):
        self.interval=interval;self.node=node or dict(id=socket.gethostname(),name=socket.gethostname())
        self.detail_cache={};self.detail_times={}
        self.lock=threading.RLock();self.data=None;self.error=None;self.history=deque(maxlen=120)
        self.previous={};self.proc_previous={};self.previous_time=None;self.native_time=0;self.native=[];self.native_warnings=[]
        self.alerts=Alerts();self.mailer=Mailer(self.node);self.firewall_time=0;self.firewall_data={};self.stop_event=threading.Event();self.ready=threading.Event()
    def invalidate(self):self.native_time=0
    def start(self):
        self.mailer.start()
        self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()
    def loop(self):
        while not self.stop_event.is_set():
            started=time.monotonic()
            try:
                data=self.collect()
                with self.lock:
                    data['node']=dict(self.node)
                    annotate(data)
                    data['alerts']=self.alerts.evaluate(data)
                    for event in data['alerts']:
                        event.setdefault('node',dict(self.node))
                    self.mailer.enqueue_events(data['alerts'])
                    data['rules']=dict(self.alerts.rules)
                    self.data=data;self.error=None
                self.ready.set()
            except Exception as exc:
                self.error=str(exc)
            self.stop_event.wait(max(1,self.interval-(time.monotonic()-started)))
    def get(self):
        with self.lock:
            if not self.data:return dict(loading=True,error=self.error)
            # Copy top-level metadata so requests do not mutate the collector state.
            return dict(self.data,history=list(self.history),rules=dict(self.alerts.rules),alerts=list(self.alerts.events)[::-1],
                audit=audit_tail(),email=self.mailer.public(),error=self.error,age=round(time.time()-self.data['sampledAt'],1))
    def cached(self,key,seconds,loader):
        now=time.monotonic()
        if key not in self.detail_cache or now-self.detail_times.get(key,0)>=seconds:
            self.detail_cache[key]=loader();self.detail_times[key]=now
        return self.detail_cache[key]
    def counters(self,key,current,elapsed):
        previous=self.previous.get(key)
        self.previous[key]=current
        return safe_rate(current,previous,elapsed)
    def collect(self):
        now=time.monotonic();warnings=[]
        if now-self.native_time>60 or self.native_time==0:
            self.native,self.native_warnings=backends.services();self.native_time=now
        warnings.extend(self.native_warnings)
        endpoints,connections,connection_complete,notice=self.cached('sockets',max(30,self.interval),sockets);
        endpoints=[dict(e) for e in endpoints];warnings.extend(notice)
        sampled=time.monotonic();elapsed=sampled-self.previous_time if self.previous_time is not None else 0
        self.previous_time=sampled
        procs={};prev={};denied=0
        for p in psutil.process_iter(['pid','ppid','name','create_time','memory_info','cpu_times','username','status'],ad_value=None):
            try:
                info=p.info
                if info['create_time'] is None:denied+=1;continue
                key=(p.pid,info['create_time']);cpu_times=info['cpu_times'];cpu_total=(cpu_times.user+cpu_times.system) if cpu_times else None
                before=self.proc_previous.get(key,{})
                cpu=safe_rate(cpu_total,before.get('cpu'),elapsed)
                entry=dict(pid=p.pid,ppid=info['ppid'],name=info['name'] or str(p.pid),cpu=round(cpu*100,2) if cpu is not None else None,
                    memory=info['memory_info'].rss if info['memory_info'] else 0,created=info['create_time'],username=info['username'],status=info['status'])
                try:
                    io=p.io_counters();read_bytes=io.read_bytes;write_bytes=io.write_bytes
                except (psutil.Error,AttributeError,NotImplementedError):read_bytes=write_bytes=None
                entry['readRate']=safe_rate(read_bytes,before.get('read'),elapsed)
                entry['writeRate']=safe_rate(write_bytes,before.get('write'),elapsed)
                prev[key]=dict(cpu=cpu_total,read=read_bytes,write=write_bytes)
                procs[p.pid]=entry
            except (psutil.Error,OSError):denied+=1
        self.proc_previous=prev
        if denied:warnings.append(f'{denied} 个进程的部分指标无权限读取；资源图只统计可见进程。')
        # A PID belongs to exactly one resource group (nearest managed ancestor).
        roots={};native=[dict(s) for s in self.native]
        for s in native:
            if s.get('pid') and s['pid'] in procs:roots.setdefault(s['pid'],[]).append(s)
        groups={};owner={}
        for pid,p in procs.items():
            ancestor=pid;seen=set();managed=None
            while ancestor and ancestor not in seen:
                seen.add(ancestor)
                if ancestor in roots:managed=roots[ancestor];break
                ancestor=procs.get(ancestor,{}).get('ppid')
            ident=managed[0]['id'] if managed else 'process-name:'+p['name']
            owner[pid]=ident
            g=groups.setdefault(ident,dict(id=ident,name=' / '.join(s['name'] for s in managed) if managed else p['name'],
                kind=managed[0]['kind'] if managed else 'process',cpu=None,memory=0,readRate=None,writeRate=None,pids=[]))
            g['pids'].append(pid);g['memory']+=p['memory']
            for metric in ('cpu','readRate','writeRate'):
                if p[metric] is not None:g[metric]=(g[metric] or 0)+p[metric]
        bypid={}
        unique={}
        for endpoint in endpoints:
            unique[(endpoint['pid'],endpoint['address'],endpoint['port'],endpoint['protocol'])]=endpoint
        endpoints=list(unique.values())
        for endpoint in endpoints:
            endpoint['name']=procs.get(endpoint['pid'],{}).get('name','未知进程')
            endpoint['exposed']=external(endpoint['address'])
            if endpoint['pid']:bypid.setdefault(endpoint['pid'],[]).append(endpoint['port'])
        services=[];covered=set()
        for s in native:
            s['systemDescription']=s.get('description','')
            p=procs.get(s.get('pid'));g=groups.get(owner.get(s.get('pid')), {}) if p else {}
            pids=g.get('pids',[]);covered.update(pids)
            s.update(cpu=g.get('cpu'),memory=g.get('memory',0),readRate=g.get('readRate'),writeRate=g.get('writeRate'),
                ports=sorted({port for pid in pids for port in bypid.get(pid,[])}),uptime=max(0,int(time.time()-p['created'])) if p else None,
                running=bool(p) or (not s.get('pid') and s.get('state') in ('active','running')),shared=len(roots.get(s.get('pid'),[]))>1,
                resourceId=g.get('id'),controllable=True)
            if not p:s['pid']=None
            s.pop('command',None);s.pop('cwd',None)
            services.append(s)
        for pid,ports in bypid.items():
            if pid in covered or pid not in procs:continue
            p=procs[pid]
            services.append(dict(id=f'process:{pid}',name=p['name'],kind='process',pid=pid,ports=sorted(set(ports)),autostart=None,
                cpu=p['cpu'],memory=p['memory'],readRate=p['readRate'],writeRate=p['writeRate'],uptime=max(0,int(time.time()-p['created'])),
                running=True,controllable=False,resourceId=owner.get(pid)))
        groups=sorted(groups.values(),key=lambda x:x['memory'],reverse=True)
        vm=psutil.virtual_memory();swap=psutil.swap_memory();disks=[];mounts=set()
        for part in self.cached('mounts',60,lambda:psutil.disk_partitions(all=False)):
            if part.mountpoint in mounts:continue
            mounts.add(part.mountpoint)
            try:
                usage=psutil.disk_usage(part.mountpoint)
                disks.append(dict(device=part.device,mount=part.mountpoint,fstype=part.fstype,total=usage.total,used=usage.used,free=usage.free,percent=usage.percent,readOnly='ro' in part.opts.split(','),alertable='ro' not in part.opts.split(',') and part.fstype.lower() not in ('squashfs','iso9660','erofs')))
            except (PermissionError,OSError):pass
        net_items=[];stats=self.cached('interfaceStats',60,psutil.net_if_stats);addresses=self.cached('addresses',60,psutil.net_if_addrs)
        for name,io in psutil.net_io_counters(pernic=True).items():
            stat=stats.get(name)
            net_items.append(dict(name=name,recv=io.bytes_recv,sent=io.bytes_sent,
                recvRate=self.counters('net:'+name+':recv',io.bytes_recv,elapsed),sentRate=self.counters('net:'+name+':sent',io.bytes_sent,elapsed),
                errors=io.errin+io.errout,drops=io.dropin+io.dropout,up=bool(stat and stat.isup),speed=stat.speed if stat else 0,
                addresses=[a.address for a in addresses.get(name,[]) if a.family in (socket.AF_INET,socket.AF_INET6)]))
        def sum_rate(items,key):
            values=[i[key] for i in items if i[key] is not None]
            return sum(values) if values else None
        network=dict(interfaces=net_items,recvRate=sum_rate(net_items,'recvRate'),sentRate=sum_rate(net_items,'sentRate'),
            recv=sum(i['recv'] for i in net_items),sent=sum(i['sent'] for i in net_items),connections=connections,complete=connection_complete)
        disk_devices=[]
        try:device_counters=psutil.disk_io_counters(perdisk=True) or {}
        except (OSError,RuntimeError):device_counters={};warnings.append('磁盘 I/O 计数不可用')
        for name,io in device_counters.items():
            busy_rate=self.counters('disk:'+name+':busy',io.busy_time,elapsed) if hasattr(io,'busy_time') else None
            disk_devices.append(dict(name=name,readRate=self.counters('disk:'+name+':read',io.read_bytes,elapsed),
                writeRate=self.counters('disk:'+name+':write',io.write_bytes,elapsed),reads=io.read_count,writes=io.write_count,
                busy=min(100,busy_rate/10) if busy_rate is not None else None))
        diskIO=dict(devices=disk_devices,readRate=sum_rate(disk_devices,'readRate'),writeRate=sum_rate(disk_devices,'writeRate'))
        if now-self.firewall_time>300 or not self.firewall_data:
            self.firewall_data=firewall();self.firewall_time=now
        try:users=[dict(name=u.name,host=u.host,started=u.started) for u in self.cached('users',60,psutil.users)]
        except (psutil.Error,OSError):users=[]
        try:elevated=psutil.Process().username().lower() in ('root','nt authority\\system') or (SYSTEM=='Windows' and __import__('ctypes').windll.shell32.IsUserAnAdmin()!=0)
        except (psutil.Error,AttributeError):elevated=False
        cpu=psutil.cpu_percent(interval=.1 if not elapsed else None);host=dict(cpu=cpu,memory=vm.total-vm.available,total=vm.total,memoryPercent=vm.percent,
            available=vm.available,swap=swap.used,swapTotal=swap.total,cores=psutil.cpu_count(),
            load=list(os.getloadavg()) if hasattr(os,'getloadavg') else None,boot=psutil.boot_time(),processCount=len(procs))
        row=dict(time=time.time(),cpu=cpu,memory=vm.percent,recv=network['recvRate'],sent=network['sentRate'],read=diskIO['readRate'],write=diskIO['writeRate'])
        with self.lock:self.history.append(row)
        own=procs.get(os.getpid(),{})
        overhead=dict(pid=os.getpid(),cpu=own.get('cpu'),memory=own.get('memory'),collectMs=round((time.monotonic()-now)*1000,1),sampleInterval=self.interval,portsInterval=max(30,self.interval),servicesInterval=60,firewallInterval=300)
        return dict(monitor=overhead,sampledAt=time.time(),platform=SYSTEM,services=services,resources=groups,host=host,network=network,disks=disks,diskIO=diskIO,
            security=dict(exposed=[e for e in endpoints if e['exposed']],listeners=endpoints,firewall=self.firewall_data,elevated=elevated,
                users=users,rootProcesses=sum(1 for p in procs.values() if p['username']=='root'),visibility='完整连接表' if connection_complete else '部分可见'),
            warnings=warnings,interval=elapsed,capabilities=dict(native={'Darwin':'launchd / Homebrew','Linux':'systemd 用户及系统服务','Windows':'Windows SCM / 计划任务'}.get(SYSTEM,'只读'),
                processIO=any(p['readRate'] is not None for p in procs.values()),processNetwork=False))
