"""Native service adapters. No implicit elevation, no shell execution."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import psutil
from storage import DATA, SYSTEM, read, write

ROOT=Path(__file__).resolve().parent
UNITS=Path.home()/'.config/systemd/user'

def run(args,timeout=25):
    p=subprocess.run(args,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
    if p.returncode:raise ValueError((p.stderr or p.stdout or f'命令失败 {p.returncode}')[-2000:])
    return p.stdout

def configs():return read('services.json',[])

def systemctl(scope,*args):return run(['systemctl']+(['--user'] if scope=='user' else [])+list(args))

def linux_services():
    result=[]; warnings=[]
    for scope in ('user','system'):
        try:
            units=json.loads(systemctl(scope,'list-units','--all','--type=service','--output=json','--no-pager'))
            files=json.loads(systemctl(scope,'list-unit-files','--type=service','--output=json','--no-pager'))
            states={s['unit_file']:s['state'] for s in files}
            names={s['unit'] for s in units}
            text=systemctl(scope,'show',*sorted(names),'-p','Id,MainPID,ActiveState,UnitFileState,Description','--no-pager') if names else ''
            props={}
            for block in text.strip().split('\n\n'):
                prop=dict(line.split('=',1) for line in block.splitlines() if '=' in line)
                if prop.get('Id'):props[prop['Id']]=prop
            for name in sorted(names|set(states)):
                if '@.' in name or states.get(name) in ('alias','masked','masked-runtime'):continue
                prop=props.get(name,{})
                custom=scope=='user' and any(s['label']+'.service'==name for s in configs())
                config=next((s for s in configs() if s['label']+'.service'==name),{}) if custom else {}
                result.append(dict(config,id='custom:'+config['label'] if custom else f'systemd:{scope}:{name}',
                    name=config.get('name',name),kind='custom' if custom else 'systemd',scope=scope,
                    pid=int(prop.get('MainPID','0')) or None,state=prop.get('ActiveState','inactive'),
                    autostart=prop.get('UnitFileState',states.get(name,'')) in ('enabled','enabled-runtime'),
                    canAutostart=states.get(name) not in ('static','indirect','generated','transient'), description=prop.get('Description','')))
        except (ValueError,OSError,subprocess.SubprocessError) as exc:
            warnings.append(f'systemd {scope} 不可用：{exc}')
    return result,warnings

def windows_services():
    result=[];warnings=[]
    try:
        for service in psutil.win_service_iter():
            try:
                s=service.as_dict()
                result.append(dict(id='windows:'+s['name'],name=s['display_name'] or s['name'],label=s['name'],kind='windows',
                    pid=s.get('pid') or None,state=s['status'],autostart=s['start_type']=='automatic',canAutostart=True))
            except psutil.Error:pass
    except (psutil.Error,AttributeError) as exc:warnings.append(str(exc))
    for item in configs():
        state=read(item['label']+'.state.json',{})
        pid=state.get('pid')
        try:
            if not pid or abs(psutil.Process(pid).create_time()-state.get('created',0))>1:pid=None
        except psutil.Error:pid=None
        result.append(dict(item,id='custom:'+item['label'],kind='custom',pid=pid,state='running' if pid else 'stopped',autostart=item.get('autostart',False)))
    return result,warnings

def services():
    if SYSTEM=='Darwin':
        import macos_backend as mac
        try:return mac.snapshot()['services'],[]
        except (OSError,ValueError,subprocess.SubprocessError) as exc:return [],[str(exc)]
    if SYSTEM=='Linux':return linux_services()
    if SYSTEM=='Windows':return windows_services()
    return [],['当前系统不支持服务管理']

def quote_unit(value):
    # systemd command-line syntax, NOT shell quoting. Escape specifiers and expansion.
    return '"'+value.replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$').replace('\n','\\n').replace('\r','\\r')+'"'

def linux_write(item):
    UNITS.mkdir(parents=True,exist_ok=True)
    # A wrapper gives consistent bounded-tail logs on every platform.
    command=[sys.executable,str(ROOT/'service_runner.py'),str(DATA),item['label']]
    unit='[Unit]\nDescription=Local Service Manager '+item['label']+'\n[Service]\nType=simple\nExecStart='+ ' '.join(map(quote_unit,command))+'\nRestart=no\n[Install]\nWantedBy=default.target\n'
    path=UNITS/(item['label']+'.service')
    path.write_text(unit,encoding='utf-8');path.chmod(0o600)
    systemctl('user','daemon-reload')

def windows_write(item,autostart):
    ns='http://schemas.microsoft.com/windows/2004/02/mit/task'
    ET.register_namespace('',ns)
    def child(parent,name,text=None,**attrs):
        x=ET.SubElement(parent,'{'+ns+'}'+name,attrs)
        x.text=text;return x
    root=ET.Element('{'+ns+'}Task',{'version':'1.2'})
    user=psutil.Process().username()
    triggers=child(root,'Triggers')
    if autostart:
        trigger=child(triggers,'LogonTrigger');child(trigger,'UserId',user);child(trigger,'Enabled','true')
    principals=child(root,'Principals');principal=child(principals,'Principal',id='Author')
    child(principal,'UserId',user);child(principal,'LogonType','InteractiveToken');child(principal,'RunLevel','LeastPrivilege')
    settings=child(root,'Settings');child(settings,'MultipleInstancesPolicy','IgnoreNew')
    child(settings,'DisallowStartIfOnBatteries','false');child(settings,'StopIfGoingOnBatteries','false')
    child(settings,'ExecutionTimeLimit','PT0S');child(settings,'AllowStartOnDemand','true')
    actions=child(root,'Actions',Context='Author');execute=child(actions,'Exec')
    child(execute,'Command',sys.executable)
    child(execute,'Arguments',subprocess.list2cmdline([str(ROOT/'service_runner.py'),str(DATA),item['label']]))
    child(execute,'WorkingDirectory',str(ROOT))
    path=DATA/(item['label']+'.xml');ET.ElementTree(root).write(path,encoding='utf-16',xml_declaration=True)
    run(['schtasks.exe','/Create','/TN',item['label'],'/XML',str(path),'/F'])

def create(body):
    if SYSTEM=='Darwin':
        import macos_backend as mac
        return mac.create(body)
    name=str(body.get('name','')).strip();command=body.get('command');cwd=Path(str(body.get('cwd',''))).expanduser()
    if not name or len(name)>80 or not isinstance(command,list) or not command or not all(isinstance(s,str) and s and '\x00' not in s for s in command):raise ValueError('服务名称或命令参数无效')
    if not cwd.is_absolute() or not cwd.is_dir():raise ValueError('工作目录必须是存在的绝对路径')
    if not Path(command[0]).is_absolute() or not os.access(command[0],os.X_OK):raise ValueError('第一项必须是可执行文件的绝对路径')
    import secrets
    item=dict(name=name,command=command,cwd=str(cwd),label='local.svcmanager.'+secrets.token_hex(6),autostart=False)
    items=configs();write('services.json',items+[item])
    try:
        if SYSTEM=='Linux':linux_write(item)
        elif SYSTEM=='Windows':windows_write(item,False)
        else:raise ValueError('不支持的平台')
    except Exception:
        write('services.json',items)
        if SYSTEM=='Linux':(UNITS/(item['label']+'.service')).unlink(missing_ok=True)
        raise

def action(body):
    ident=body.get('id','');op=body.get('action')
    if op not in ('start','stop','restart','autostart','delete'):raise ValueError('未知操作')
    if op=='autostart' and not isinstance(body.get('enabled'),bool):raise ValueError('enabled 必须是布尔值')
    if SYSTEM=='Darwin':
        import macos_backend as mac
        return mac.action(body)
    # Validate IDs against native enumeration before sending privileged commands.
    items,warnings=services()
    service=next((s for s in items if s['id']==ident),None)
    if not service:raise ValueError('服务不存在或没有读取权限')
    custom=service['kind']=='custom'
    item=next((s for s in configs() if 'custom:'+s['label']==ident),None) if custom else None
    if op=='delete' and not custom:raise ValueError('系统服务不支持移除')
    if SYSTEM=='Linux':
        scope='user' if custom else service['scope']
        name=item['label']+'.service' if custom else ident.split(':',2)[2]
        if op=='delete':
            systemctl(scope,'stop',name)
            if service['autostart']:systemctl(scope,'disable',name)
            (UNITS/name).unlink(missing_ok=True);systemctl(scope,'daemon-reload')
        else:systemctl(scope,('enable' if body['enabled'] else 'disable') if op=='autostart' else op,name)
    elif SYSTEM=='Windows':
        if custom:
            name=item['label']
            if op in ('stop','restart','delete'):
                # Terminate the verified process tree before ending the wrapper task.
                state=read(name+'.state.json',{})
                pid=state.get('pid')
                try:
                    p=psutil.Process(pid) if pid else None
                    if p and abs(p.create_time()-state.get('created',0))<1:
                        for c in p.children(recursive=True):c.terminate()
                        p.terminate()
                except psutil.NoSuchProcess:pass
                if service.get('pid'):run(['schtasks.exe','/End','/TN',name])
            if op in ('start','restart'):run(['schtasks.exe','/Run','/TN',name])
            if op=='delete':run(['schtasks.exe','/Delete','/TN',name,'/F'])
            if op=='autostart':
                windows_write(item,body['enabled']);item['autostart']=body['enabled']
                write('services.json',[item if s['label']==name else s for s in configs()])
        else:
            name=service['label']
            if op=='autostart':run(['sc.exe','config',name,'start=','auto' if body['enabled'] else 'demand'])
            else:
                if op in ('stop','restart'):
                    run(['sc.exe','stop',name])
                    if op=='restart':
                        import time
                        for _ in range(40):
                            if psutil.win_service_get(name).status()=='stopped':break
                            time.sleep(.25)
                        else:raise ValueError('服务停止超时，请稍后重试')
                if op in ('start','restart'):run(['sc.exe','start',name])
    if op=='delete':
        write('services.json',[s for s in configs() if s['label']!=item['label']])
        (DATA/(item['label']+'.xml')).unlink(missing_ok=True)
        (DATA/(item['label']+'.state.json')).unlink(missing_ok=True)
