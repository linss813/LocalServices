# 独立监控代理

此包只有 Python 监控后台，不含控制台 HTML、CSS、JavaScript，也不含节点令牌、邮箱授权码或任何本机数据。

## 安装与运行

需要 Python 3.9+，Windows 建议安装带 Python Launcher 的发行版。解压后进入 `local-service-agent`：

macOS / Linux：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python agent.py --node-name server-shanghai --interval 10
```

Windows：

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe agent.py --node-name server-shanghai --interval 10
```

默认监听 `127.0.0.1:18767`，每 10 秒采样，默认不开放服务启停和日志。仅当确实需要从控制台管理服务时，显式添加 `--allow-control`，不自动提权。自定义服务配置、系统服务权限要求与完整工具一致。

终端会给出 `agent-token` 文件路径，不会打印其内容。请在服务器本机读取令牌，在控制台“管理节点”中粘贴；不要放入 URL、命令参数或截图。独立节点首次产生自己的身份与令牌，不复制其他节点的数据目录。可用 `LOCAL_SERVICE_DATA` 指定每个实例独立的数据目录。

## 连接控制台

推荐经 SSH 转发（在控制台所在电脑执行，替换实际用户名与服务器）：

```sh
ssh -N -L 18768:127.0.0.1:18767 user@server
```

在控制台添加地址 `http://127.0.0.1:18768` 及该服务器的代理令牌。多个服务器使用不同本地转发端口。只拉取当前选中的节点，不周期轮询全部节点。

也可直接监听服务器网络地址，但必须提供受信任证书：

```sh
.venv/bin/python agent.py --listen 0.0.0.0 --port 18767 --tls-cert /path/server-fullchain.pem --tls-key /path/server-key.pem --node-name server-shanghai
```

控制台使用 `https://实际证书域名:18767` 连接，验证证书与域名；不提供“忽略证书”开关。仅给需要连接的地址开放网络访问。HTTP 非回环监听会被拒绝，控制台也不会向非回环 HTTP 地址发送令牌。没有内置 CORS，令牌由控制台后端使用，不发送给浏览器。

## 服务器开机运行（Linux systemd 示例）

将程序放到固定目录，例如 `/opt/local-service-agent`，使用专用普通用户 `service-monitor`（该用户与目录需自行创建）。创建 `/etc/systemd/system/local-service-agent.service`：

```ini
[Unit]
Description=Local Services Monitor Agent
After=network.target

[Service]
Type=simple
User=service-monitor
WorkingDirectory=/opt/local-service-agent
Environment=LOCAL_SERVICE_DATA=/var/lib/local-service-agent
ExecStart=/opt/local-service-agent/.venv/bin/python /opt/local-service-agent/agent.py --node-name server-shanghai --interval 10
Restart=on-failure
RestartSec=10
StateDirectory=local-service-agent
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now local-service-agent
sudo systemctl status local-service-agent
```

该示例不会由工具自动安装或提权执行。普通用户的指标可见范围受权限影响，不必为了监控改为 root。若安装路径不同，修改 WorkingDirectory 与 ExecStart。Windows 可通过系统计划任务、macOS 可通过 LaunchAgent 托管 `agent.py`；不要配置重复实例。

## 独立告警

在代理所在服务器执行 `configure_email.py`，授权码隐藏输入：

```sh
.venv/bin/python configure_email.py --host smtp.163.com --port 465 --encryption ssl --sender sender@163.com --to recipient@163.com
```

Windows 替换为 `.venv\Scripts\python.exe`。代理自行判断阈值并发送邮件；关掉控制台或断开 SSH 隧道，不影响代理采集和投递，但无法通过离线控制台操作该节点。若代理进程或服务器本身停止，代理无法给自己发送离线邮件；当前未实现独立心跳接收端。

邮箱配置必须分别在各节点设置，控制台不会自动分发本机授权码。邮件主题包含 `[触发/恢复/测试][严重/注意/正常/信息][节点名][CPU/内存/服务离线/安全类别]`，不同状态、节点、类型不混在同一邮件中。

## 资源与频率

默认资源采样 10 秒，端口 30 秒，服务/网卡地址/挂载点缓存 60 秒，防火墙 300 秒。`--interval 30` 可进一步减少采集频率（支持 5–300 秒）。趋势只在内存保留最近 120 个点。按需压缩快照，控制台短期缓存；代理无浏览器渲染与 Web 框架依赖，唯一第三方依赖是 psutil。面板显示代理 CPU、RSS 与采集耗时；这些是实测样本，不是资源上限或所有主机的性能保证。

受权限限制时会明确显示不可用。磁盘 I/O、网络总量可能包含虚拟设备重复计数，不提供单进程网络流量归因。数据路径与完整工具相同；Unix 目录 700、敏感文件 600，Windows 继承用户目录 ACL。不要复制 `email.json`、`agent-token`、`nodes.json` 到公开仓库。

磁盘容量按去重后的可写文件系统告警，排除只读镜像。磁盘默认超限 120 秒才触发，恢复需稳定 60 秒并回落 5 个百分点；普通邮件默认间隔 300 秒，可在控制台阈值中修改。持续告警不重复发信，未投递即恢复的短暂事件不补发邮件。

存储支持系统盘、全部、外接和单个存储池范围。
macOS 使用 APFS 容器容量并缓存 30 秒，硬件拓扑缓存 300 秒；
Linux 总 I/O 不重复累加物理盘、其分区以及 loop/映射设备。
控制台容量使用 GB/TB，内存及速率使用 GiB/MiB。
