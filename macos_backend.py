#!/usr/bin/env python3
"""Loopback-only macOS service manager."""
import json
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parent
from storage import DATA
AGENTS = Path.home() / 'Library/LaunchAgents'
CACHE = {'time': 0, 'items': []}

def run(args, timeout=25):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise ValueError((p.stderr or p.stdout or f'命令失败 ({p.returncode})')[-2000:])
    return p.stdout

def configs():
    path = DATA / 'services.json'
    return json.loads(path.read_text()) if path.exists() else []

def save(items):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DATA / 'services.tmp'
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    tmp.chmod(0o600)
    tmp.replace(DATA / 'services.json')

def brew_services():
    if time.monotonic() - CACHE['time'] < 12:
        return CACHE['items']
    brew = shutil.which('brew')
    if not brew:
        return []
    try:
        items = json.loads(run([brew, 'services', 'list', '--json']))
        CACHE.update(time=time.monotonic(), items=items)
        return items
    except (ValueError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return CACHE['items']

def launch_jobs():
    jobs = {}
    for line in run(['launchctl', 'list']).splitlines()[1:]:
        fields = line.split(None, 2)
        if len(fields) == 3:
            jobs[fields[2]] = dict(pid=int(fields[0]) if fields[0].isdigit() else None, exitCode=fields[1])
    return jobs

def snapshot():
    jobs = launch_jobs()
    services = []
    for item in configs():
        label = item['label']
        job = jobs.get(label, {})
        services.append(dict(item, id='custom:'+label, kind='custom',
            autostart=(AGENTS/(label+'.plist')).exists(), pid=job.get('pid'),
            state='running' if job.get('pid') else 'stopped', exitCode=job.get('exitCode')))
    for item in brew_services():
        name = item['name']
        label = item.get('service_name') or 'homebrew.mxcl.'+name
        pid = jobs.get(label, {}).get('pid')
        services.append(dict(id='brew:'+name, name=name, kind='brew', label=label,
            autostart=(AGENTS/(label+'.plist')).exists(), pid=pid,
            state='running' if pid else 'stopped'))
    known = {s['label'] for s in services}
    disabled = disabled_jobs()
    for label, path in native_plists().items():
        if label in known:
            continue
        job = jobs.get(label, {})
        services.append(dict(id='launchd:'+label, name=label, label=label,
            kind='launchd', pid=job.get('pid'),
            autostart=label not in disabled,
            state='running' if job.get('pid') else 'stopped',
            exitCode=job.get('exitCode'), description='用户 LaunchAgent · '+str(path)))
    return {'services':services}

def native_plists():
    result = {}
    for path in AGENTS.glob('*.plist'):
        try:
            doc = plistlib.loads(path.read_bytes())
            label = doc.get('Label', '')
            if label and not label.startswith(('com.apple.', 'homebrew.mxcl.', 'local.svcmanager.')):
                result[label] = path
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
    return result

def disabled_jobs():
    import re
    try:
        output = run(['launchctl', 'print-disabled', f'gui/{os.getuid()}'])
        return set(re.findall(r'"([^"\n]+)"\s*=>\s*true', output))
    except (ValueError, subprocess.TimeoutExpired):
        return set()

def plist_path(item):
    auto = AGENTS / (item['label']+'.plist')
    return auto if auto.exists() else DATA / (item['label']+'.plist')

def write_plist(item, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = dict(Label=item['label'], ProgramArguments=item['command'], WorkingDirectory=item['cwd'],
        RunAtLoad=True, KeepAlive=False, EnvironmentVariables={'PATH':os.environ.get('PATH', '/usr/bin:/bin')},
        StandardOutPath=str(DATA/(item['label']+'.log')), StandardErrorPath=str(DATA/(item['label']+'.log')))
    path.write_bytes(plistlib.dumps(doc))
    path.chmod(0o600)

def action(body):
    ident, op = body.get('id',''), body.get('action')
    if op not in ('start','stop','restart','autostart','delete'):
        raise ValueError('未知操作')
    if ident.startswith('launchd:'):
        label = ident[len('launchd:'):]
        path = native_plists().get(label)
        if not path:
            raise ValueError('未找到用户 LaunchAgent')
        target = f'gui/{os.getuid()}/'+label
        if op == 'delete':
            raise ValueError('已有 LaunchAgent 请在原应用中卸载')
        if op == 'autostart':
            if not isinstance(body.get('enabled'), bool):
                raise ValueError('enabled 必须是布尔值')
            run(['launchctl', 'enable' if body['enabled'] else 'disable', target])
            return
        loaded = label in launch_jobs()
        if op == 'stop':
            if loaded:
                run(['launchctl', 'bootout', target])
            return
        if label in disabled_jobs():
            raise ValueError('该服务已禁用，请先启用自启后启动')
        if not loaded:
            run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)])
        run(['launchctl', 'kickstart']+(['-k'] if op=='restart' and loaded else [])+[target])
        return
    if ident.startswith('brew:'):
        name = ident[5:]
        if name not in [s['name'] for s in brew_services()]:
            raise ValueError('未找到 Homebrew 服务')
        if op == 'delete':
            raise ValueError('请使用 Homebrew 卸载服务')
        brew = shutil.which('brew')
        service = next(s for s in snapshot()['services'] if s['id']==ident)
        if op == 'autostart':
            if not isinstance(body.get('enabled'), bool):
                raise ValueError('enabled 必须是布尔值')
            run([brew, 'services', 'stop', name])
            if body['enabled'] or service['pid']:
                run([brew, 'services', 'start' if body['enabled'] else 'run', name])
        else:
            if op in ('stop','restart'):
                # brew stop removes the login plist; preserve the user's autostart choice.
                login_plist = AGENTS/(service['label']+'.plist')
                saved_plist = login_plist.read_bytes() if service['autostart'] else None
                run([brew, 'services', 'stop', name])
                if op == 'stop' and saved_plist is not None:
                    login_plist.parent.mkdir(parents=True, exist_ok=True)
                    login_plist.write_bytes(saved_plist)
                    login_plist.chmod(0o600)
            if op in ('start','restart'):
                run([brew, 'services', 'start' if service['autostart'] else 'run', name])
        CACHE['time'] = 0
        return
    items = configs()
    item = next((x for x in items if 'custom:'+x['label']==ident), None)
    if not item:
        raise ValueError('仅支持操作已注册的服务')
    target = f'gui/{os.getuid()}/'+item['label']
    loaded = item['label'] in launch_jobs()
    if op == 'autostart':
        if not isinstance(body.get('enabled'), bool):
            raise ValueError('enabled 必须是布尔值')
        old = plist_path(item)
        path = (AGENTS if body['enabled'] else DATA)/(item['label']+'.plist')
        write_plist(item, path)
        if old != path:
            old.unlink(missing_ok=True)
    elif op == 'delete':
        if loaded:
            run(['launchctl','bootout',target])
        plist_path(item).unlink(missing_ok=True)
        save([x for x in items if x!=item])
    elif op == 'stop':
        if loaded:
            run(['launchctl','bootout',target])
    else:
        if not loaded:
            run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(plist_path(item))])
        else:
            run(['launchctl','kickstart']+(['-k'] if op=='restart' else [])+[target])

def create(body):
    name = str(body.get('name','')).strip()
    command = body.get('command')
    cwd = Path(str(body.get('cwd',''))).expanduser()
    if not name or len(name)>80 or not isinstance(command,list) or not command or not all(isinstance(x,str) and x and '\x00' not in x for x in command):
        raise ValueError('请填写服务名称及有效的命令参数数组')
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ValueError('工作目录必须是存在的绝对路径')
    if not Path(command[0]).is_absolute() or not os.access(command[0],os.X_OK):
        raise ValueError('命令第一项必须是可执行文件的绝对路径')
    item = dict(name=name, command=command, cwd=str(cwd), label='local.svcmanager.'+secrets.token_hex(6))
    items = configs()
    write_plist(item, DATA/(item['label']+'.plist'))
    save(items+[item])

