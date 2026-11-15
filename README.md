# Local Services

轻量本地与远程服务监控工具。一个控制台查看多个节点，独立代理负责采集和邮件告警，控制台关闭后代理继续运行。

Python 3.9+；唯一第三方运行依赖是 psutil。支持 macOS、Linux、Windows。

## 功能

- CPU、内存环图，资源 TOP 排行、历史趋势；每个 PID 只归属一个资源组。
- 内存、CPU、磁盘读写、运行时长、PID、端口、名称排序，支持升降序和列头排序。
- 服务与端口、全部进程资源组两种视图，名称/用途/端口/PID 搜索。
- 常见组件中文名称与用途，保留原始标识；支持节点独立的自定义名称和说明。
- 网卡速率、连接状态、监听地址；磁盘容量、I/O、平台支持时的设备繁忙度。
- 防火墙可读状态、非回环端口、敏感端口、登录会话、操作审计。
- 可配置阈值、持续超限判断、服务离线、恢复记录；HTML/纯文本邮件、去重与退避重试。
- 暖灰、浅色、柔和深色主题及字号调节，减少动画设置支持，适配手机。

## 快速启动

```sh
git clone https://github.com/linss813/LocalServices.git
cd LocalServices
./start.sh
```

打开 http://127.0.0.1:18765 。首次运行创建 `.venv` 并安装依赖。Windows 使用 `start.bat` 或 `start.ps1`。

默认是单机合并模式。分离部署：

```sh
# 服务器：默认只读代理，端口 18767，无网页
./start.sh --mode agent --node-name server-hk --interval 10

# 管理电脑：纯控制台，端口 18765
./start.sh --mode console

# 同一电脑分离运行时，先启动代理，再让控制台读取同数据目录的代理令牌
./start.sh --mode console --local-agent http://127.0.0.1:18767
```

控制台“管理节点”中填写节点名称、地址和代理 `agent-token` 文件的内容。令牌不回显、不发送给浏览器。只拉取当前选中节点，不同时轮询所有节点。

远程连接推荐 SSH 隧道：

```sh
ssh -N -L 18768:127.0.0.1:18767 user@server
```

将该节点地址填为 `http://127.0.0.1:18768`。多个节点使用不同本地转发端口。也支持受信任证书的 HTTPS；禁止非回环明文 HTTP，验证证书并拒绝重定向。

[独立代理部署说明](AGENT.md)包含无前端安装包、TLS、Linux systemd 常驻示例与数据路径。构建代理包：

```sh
python3 build_agent.py --output dist/local-service-agent.zip
```

代理默认不提供启停、注册、移除或日志访问；需要时显式添加 `--allow-control`，操作权限仍由系统决定，不自动提权。

## 平台服务管理

| 平台 | 服务 | 自启 |
|---|---|---|
| macOS | 当前用户 Homebrew、launchd 自定义服务 | 用户登录后 |
| Linux | systemd 用户/系统服务、自定义用户 unit | 按用户或系统范围 |
| Windows | Windows SCM、自定义计划任务 | 系统服务随系统；自定义任务随登录 |

Linux 建议 systemd 245+ 及可用用户会话总线。没有 systemd 时仍能监控资源，服务管理明确提示不可用。Windows 修改系统服务通常需要管理员权限。发现的普通进程只读；共享宿主 PID 的服务标注资源共享。

注册自定义服务时，命令采用 JSON 参数数组，不经 Shell；第一项为可执行文件的绝对路径。示例：

```json
["/usr/bin/python3", "-u", "-m", "http.server", "8000", "--bind", "127.0.0.1"]
```

工作目录填写现有绝对路径。Windows 替换为实际 Python 路径。不要在参数中存放密码。macOS 保护目录可能被 launchd 拒绝，可查看日志确认。

## 邮件告警

在每个代理所在节点单独配置，授权码隐藏输入，不经控制台分发：

```sh
.venv/bin/python configure_email.py --host smtp.163.com --port 465 --encryption ssl --sender sender@163.com --to recipient@163.com
```

Windows 使用 `.venv\Scripts\python.exe`。支持 `ssl`、`starttls`、`none`。配置保存至本机受限目录；不要把授权码提交到仓库。告警中心提供测试投递状态。

主题示例：

```text
[触发][注意][server-hk][CPU] CPU 持续高占用
[恢复][正常][server-hk][CPU] CPU 持续高占用
[触发][严重][server-hk][服务离线] 知识库 API · 被监控服务已离线
```

同一节点、状态、级别、类别才合并。服务和磁盘告警带具体目标名称。恢复正文注明原始触发信息，避免把旧指标误认为当前指标。自动投递最多每分钟一次，失败从 1 分钟起退避至每小时重试，队列最多 200 条。SMTP 接受不保证最终入箱，异常中断时不保证严格一次投递。

默认 CPU/内存/磁盘 90%，网络合计 100 MiB/s、磁盘合计 200 MiB/s、服务 RSS 2048 MiB，持续 30 秒。磁盘容量与安全状态立即判断。离线告警在服务列表逐项开启。确认知悉不等于解除告警。

## 资源与边界

默认采集 10 秒，端口 30 秒，服务/网卡元数据/挂载点缓存 60 秒，防火墙 300 秒。`--interval 30` 可进一步降低采样频率。历史仅在内存保留 120 点。快照可 gzip 压缩，代理无浏览器和 Web 框架依赖；同一数据目录只允许一个监控实例。

控制台显示代理自身 CPU、RSS、采集耗时。性能随进程数、操作系统和权限变化，不承诺固定资源上限。

- CPU 为多核累计，可超过 100%；环图是可见进程之间的份额。
- RSS 可能重复计算共享内存。网络和磁盘合计可能含虚拟设备重复计数。
- Snap squashfs、ISO、EROFS 与只读挂载仍可查看，但不参与容量不足告警，避免只读镜像恒定 100% 的误报。
- 不提供单进程网络流量归因、硬盘 SMART 检测或漏洞扫描。
- 无权限的指标显示不可用。macOS 可回退 lsof，无法读取完整连接表时明确提示。
- 代理或整台服务器停止时不能给自己发离线邮件；当前无外部心跳接收端。
- 控制台面向本机可信用户，只监听回环地址，不适合直接反代到公网。

## 本地数据

- macOS：`~/Library/Application Support/LocalServiceManager/`
- Linux：`${XDG_DATA_HOME:-~/.local/share}/local-service-manager/`
- Windows：`%LOCALAPPDATA%\LocalServiceManager\`

可用 `LOCAL_SERVICE_DATA` 指定独立目录。`email.json` 含邮箱授权码、`agent-token` 和 `nodes.json` 含节点凭据，均不得提交。Unix 目录权限 700，敏感文件 600；Windows 继承当前用户目录 ACL。每个节点使用独立数据目录与身份。

日志查看只读末尾 32 KB。Linux/Windows 包装程序在启动时轮换超过 10 MB 的日志，macOS 日志由 launchd 追加；持续大日志需另配轮转。审计超过 2 MB 保留一份轮换。

## 验证

```sh
.venv/bin/python -m unittest discover -s . -p test_monitor.py -v
.venv/bin/python -m unittest discover -s . -p test_architecture.py -v
```

基础测试 23 项、分离架构测试 11 项，不操作已有服务、不发送真实邮件；架构测试使用独立临时节点，验证认证、压缩、只读控制、别名、重复实例、关闭控制台后持续采样及邮件主题分类。

完整服务生命周期测试只创建自身临时 HTTP 服务并清理，需要启动控制台且选中节点明确开启控制：

```sh
.venv/bin/python test_manager.py
# 可指定地址与节点
TEST_MANAGER_URL=http://127.0.0.1:18765 TEST_MANAGER_NODE=NODE_ID .venv/bin/python test_manager.py
```

macOS 实际服务生命周期与桌面/手机交互、独立 Linux 容器代理已验证。完整 Linux systemd 主机及 Windows 实机的服务管理仍待端到端验收；对应适配器有模拟契约测试。
