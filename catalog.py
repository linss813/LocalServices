"""Small offline catalog. Keep original identifiers; unknown roles are never guessed."""
from storage import read,write

KNOWN={
 'com.apple.WebKit.WebContent':('网页内容进程','WebKit 网页渲染进程，常见于 Safari 及使用 WebKit 的应用。'),
 'com.apple.WebKit.GPU':('网页图形加速','WebKit 的图形与合成辅助进程。'),
 'com.apple.WebKit.Networking':('网页网络进程','WebKit 的网络请求辅助进程。'),
 'rapportd':('Apple 设备协作','Apple 设备间发现与协作相关进程，具体用途受系统版本影响。'),
 'sharingd':('Apple 共享服务','与 AirDrop 等系统共享功能相关。'),
 'ControlCenter':('控制中心','macOS 控制中心组件。'),
 'WindowServer':('窗口与显示服务','macOS 窗口合成与显示管理组件。'),
 'OrbStack Helper':('OrbStack 虚拟化助手','OrbStack 容器或虚拟机的辅助进程；占用可能包含其中的工作负载。'),
 'cloudflared':('Cloudflare 隧道','Cloudflare Tunnel 客户端；实际用途以本机配置为准。'),
 'node':('Node.js 运行时','运行 JavaScript 服务或开发工具，需结合端口、PID 判断具体项目。'),
 'Electron':('Electron 应用进程','Electron 桌面应用的宿主进程；仅凭进程名无法判断具体应用。'),
 'postgres':('PostgreSQL 数据库','PostgreSQL 服务端或其工作进程。'),
 'mysqld':('MySQL 数据库','MySQL 兼容数据库服务端，具体发行版需进一步确认。'),
 'redis-server':('Redis 服务','Redis 服务端进程。'),
 'nginx':('Nginx 服务','Web 服务或反向代理，具体站点由本机配置决定。'),
 'sshd':('SSH 服务','提供远程登录或文件传输的 SSH 服务端。'),
}

def describe(name,ident=None,aliases=None):
    aliases=read('aliases.json',{}) if aliases is None else aliases
    alias=aliases.get(ident,{}) if ident else {}
    known=KNOWN.get(name)
    if known:display,description=known
    elif name.startswith('python'):
        display,description=name,'Python 运行时。仅凭进程名无法判断具体脚本，请结合端口与 PID 查看。'
    elif name.startswith('com.apple.'):
        display,description=name,'Apple 命名空间中的组件标识；当前离线目录尚无精确用途说明。'
    elif name.startswith(('com.','org.','net.','io.')):
        display,description=name,'反向域名形式的组件标识；尚未确认具体用途，可添加自己的名称与说明。'
    else:display,description=name,'尚无内置用途说明，可添加自己的名称与说明；原始标识会保留。'
    return dict(displayName=alias.get('name') or display,description=alias.get('description') or description,
                nameSource='自定义' if alias else '内置说明' if known else '原始标识')

def annotate(data):
    aliases=read('aliases.json',{})
    for group in data.get('resources',[]):group.update(describe(group['name'],group['id'],aliases))
    for service in data.get('services',[]):
        service['aliasId']=service['id'] if service['kind']!='process' else 'process-name:'+service['name']
        info=describe(service['name'],service['aliasId'],aliases)
        if info['nameSource']=='原始标识' and service.get('systemDescription'):
            info['description']=service['systemDescription'];info['nameSource']='系统说明'
            if service.get('kind')=='systemd':info['displayName']=service['systemDescription'][:80]
        service.update(info)
    return data

def set_alias(body,valid):
    ident=body.get('id');name=body.get('name','');description=body.get('description','')
    if ident not in valid:raise ValueError('资源组或服务不存在')
    if not isinstance(name,str) or not isinstance(description,str) or len(name)>80 or len(description)>600 or any(c in name for c in '\r\n\x00'):raise ValueError('名称最多 80 字、说明最多 600 字')
    aliases=read('aliases.json',{})
    if not name.strip() and not description.strip():aliases.pop(ident,None)
    else:aliases[ident]=dict(name=name.strip(),description=description.strip())
    write('aliases.json',aliases)
