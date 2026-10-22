# npa-mcp

嵌入式开发 MCP 服务器(Claude Code / FastMCP)。在一个 Windows 进程里提供:

- **远程代码服务器**:SSH 执行命令、SFTP 读写文件、上传下载、编译产物拉回本机。
- **开发板操作**:串口 / Telnet / SSH 三种方式,`device_exec` 自动路由;网页终端实时可看。
- **固件更新**:本机 TFTP / FTP / HTTP 文件服务,让开发板用 `tftp` / `wget` / `ftpget` / `scp` 主动拉取,默认做 MD5 校验。
- **电源管理**:Telnet / SSH 控制 PDU,给开发板上电、断电、查状态。
- **定时自主工作**:Windows 任务计划在指定时间段启动无头 Claude,窗口结束自动收工。
- **HTML 总结报告**:独立单文件,零外链,可直接归档或分享。
- **网页配置**:所有凭据在本地配置页填写,工具层没有写凭据的入口。

> 设计取向:所有远程会话都有对应的网页终端,用户能实时看到 AI 在做什么;所有失败都返回中文 `error` + `hint`,不抛裸 traceback。

---

## 1. 安装

### 1.1 环境要求

| 项目 | 要求 |
|---|---|
| 系统 | Windows 10/11 |
| Python | 3.10+(推荐 3.13;`py -3.13` 可用) |
| Claude Code | 已安装并在 PATH 中(`claude` 命令可用) |
| 网络 | 本机能访问代码服务器 / 开发板 / 电源设备;开发板能访问本机 |

### 1.2 一键安装(推荐)

```powershell
cd <项目目录>
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

脚本会:

1. 用 `py -3.13`(找不到则用 `python`)创建虚拟环境,默认放在
   `%LOCALAPPDATA%\npa-mcp\venv`(不放进 OneDrive 同步目录);
2. 安装本项目与全部依赖(`pip install -e .`);
3. 检查 `src/npa_mcp/web/static/` 下的 xterm.js 是否齐全,缺失时从
   jsDelivr 下载固定版本(运行期不依赖 CDN);
4. 打印可复制到 `.mcp.json` 的配置片段。

常用参数:

```powershell
.\scripts\install.ps1 -VenvPath D:\venvs\npa-mcp -WithDev
```

### 1.3 手动安装

```powershell
py -3.13 -m venv "$env:LOCALAPPDATA\npa-mcp\venv"
& "$env:LOCALAPPDATA\npa-mcp\venv\Scripts\python.exe" -m pip install -U pip
& "$env:LOCALAPPDATA\npa-mcp\venv\Scripts\python.exe" -m pip install -e .
```

---

## 2. 接入 Claude Code

把 `.mcp.example.json` 复制为项目根目录的 `.mcp.json`(或合并进已有配置),
把路径改成自己的实际路径:

```json
{
  "mcpServers": {
    "npa_mcp": {
      "command": "C:/Users/<你>/AppData/Local/npa-mcp/venv/Scripts/python.exe",
      "args": ["-m", "npa_mcp"],
      "env": {
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

重启 Claude Code,确认 `npa_mcp` 已连接。此时可以让 Claude 调用:

```
list_targets          # 看环境状态与哪些目标还没配置
open_config_page      # 打开配置页填地址和凭据
```

MCP 进程启动时会同时启动本地网页服务(默认 `http://127.0.0.1:8765`),
并在默认浏览器自动打开配置页(`web.auto_open`,默认开启;计划任务等无头场景
因 `NPA_MCP_WINDOW` 环境变量自动跳过,也可设 `NPA_MCP_NO_BROWSER=1` 关闭;
多个 Claude 会话同时启动时按 `web.auto_open_interval_sec` 去重)。
配置页与终端页只监听本机回环地址;`web.host` 改成 `0.0.0.0` 前请确认网络环境。

### 2.1 网页页面

| 地址 | 用途 |
|---|---|
| `/config` | 填写代码服务器、开发板、电源、文件服务、工作窗口等配置 |
| `/terminals` | 每个远程会话一个 xterm.js 终端,实时显示 AI 的操作 |
| `/reports/` | 浏览已生成的 HTML 报告 |
| `/images/` | HTTP 固件下载(可加 token,供开发板 `wget`) |

配置保存后立即生效(按文件 mtime 热加载),不用重启 MCP。

### 2.2 独立终端窗口(默认)

串口、telnet、SSH 以及代码服务器镜像会话创建时,npa-mcp 会自动用
`CREATE_NEW_CONSOLE` 弹出一个类似 cmd 的原生控制台窗口(`python -m npa_mcp.term`),
窗口内可实时观看输出并直接敲键盘输入(Enter、方向键、Ctrl+C 原样转发),
**Ctrl+]** 关闭窗口但保留底层会话。配置页取消勾选“会话自动开独立终端窗口”
(`web.native_terminal=false`)可关闭;夜间计划任务因 `NPA_MCP_WINDOW` 自动跳过。
浏览器 `/terminals` 页面保留作备用视图。

---

## 3. 首次配置

打开配置页后按分组填写。配置文件位于 `~/.npa-mcp/config.json`
(可用环境变量 `NPA_MCP_HOME` 改根目录)。

### 3.1 代码服务器 `code_server`

| 字段 | 说明 |
|---|---|
| `host` / `port` | SSH 地址,默认 22 |
| `username` | 登录用户 |
| `auth.type` | `password` 或 `key` |
| `auth.password` | 密码(`auth.type=password`) |
| `auth.key_file` / `passphrase` | 私钥路径与口令(`auth.type=key`) |
| `workdir` | 默认工作目录,例如 `/home/dev/project` |
| `shell_prompt` | 预留字段,SSH exec 不依赖提示符 |

### 3.2 开发板 `device`

`prefer` 决定 `device_exec(kind=auto)` 的优先方式;`ssh` / `telnet` / `serial`
三组各自有 `enabled` 开关,只有启用的才会被尝试。

- **SSH**:与代码服务器相同的 `auth` 结构,另有 `shell_prompt`。
- **Telnet**:`host`、`port`、`username`、`password`、`login_prompt`、
  `password_prompt`、`shell_prompt`、`echo_strip`、`line_ending`。
- **串口**:`port`(如 `COM3`;COM10 以上自动加 `\\.\` 前缀)、`baudrate`、
  `bytesize`、`parity`、`stopbits`、`encoding`、`line_ending`、
  `login_required`、`login_prompt`、`password_prompt`、`shell_prompt`。

> `shell_prompt` 必须与设备真实提示符一致(例如 `# `、`$ `、`/ # `)。
> 不匹配会导致 `device_exec` 超时;此时输出仍会原样返回,便于排查。

### 3.3 电源管理 `power`

| 字段 | 说明 |
|---|---|
| `transport` | `telnet` 或 `ssh` |
| `host` / `port` | 设备地址 |
| `username` / `password` | 登录凭据(PDU 常见用户名 `admin`) |
| `login_prompt` / `password_prompt` / `prompt` | 登录与命令提示符 |
| `outlet` | 默认插座号 |
| `commands` | `on` / `off` / `status` 三条命令模板,`{outlet}` 会被替换 |
| `expect.ok` / `expect.fail` | 判定成功/失败的关键字(先匹配到者生效) |
| `session_reuse` | 复用 Telnet 会话(默认 true,空闲 120s 后重建) |

`power_status` 会从输出中推断 `state=on/off/unknown`;无法判定时
`ok=false`,不会假装成功。

### 3.4 文件服务 `firmware_server`

| 服务 | 默认 | 说明 |
|---|---|---|
| TFTP | `0.0.0.0:69/UDP` | 服务 `paths.image_dir`,设备端 `tftp -g` 拉取 |
| FTP | `0.0.0.0:2121/TCP` | 被动端口 `30000-30100`,用户名/密码可配 |
| HTTP | 网页同一端口 | `GET /images/<文件名>`,可配置 `token` |

`host` 留空时自动探测本机局域网 IP:优先按开发板/电源地址(`device.*.host`、
`power.host`)反查路由源地址;都未配置时枚举本机网卡并跳过虚拟网卡常见的 `.1`
地址。若本机装有 Sing-box/WSL/VMware 等虚拟网卡,建议在配置页把
`firmware_server.host` 固定为真实网卡地址(如 `192.168.71.55`)。
双实例(用户会话 + 夜间任务)同时运行时,
TFTP/FTP 第二个实例会因端口占用降级,但不影响另一个实例继续分发;网页端口会
自动 +1 并写入 `~/.npa-mcp/web_port.txt`。

### 3.5 固件拉取模板 `firmware`

```json
{
  "device_pull_dir": "/tmp",
  "pull_templates": {
    "tftp":   "tftp -g -r {file} {host}",
    "wget":   "wget http://{host}:{http_port}/images/{file}?token={token} -O {remote_path}",
    "ftpget": "ftpget -P {port} -u {user} -p {pass} {host} {file} {remote_path}",
    "scp":    "scp {user}@{host}:{file} {remote_path}"
  },
  "verify": {
    "cmd": "ls -l {remote_path} && md5sum {remote_path}",
    "expect_ok": ""
  }
}
```

占位符:`{file}`、`{host}`、`{http_port}`、`{port}`、`{user}`、`{pass}`、
`{remote_path}`、`{token}`。`{http_port}` 跟随本实例实际网页端口,端口自增后
`wget` 模板自动正确。

`verify.cmd` 为空表示不校验;`expect_ok` 非空时,输出包含该子串即视为通过;
否则从输出提取 32 位 MD5 与本地文件比对。

### 3.6 定时工作 `work_windows`

```json
{
  "name": "nightly",
  "days": [1, 2, 3, 4, 5, 6, 0],
  "start": "23:00",
  "end": "06:00",
  "job": {
    "project_dir": "C:/work/my-project",
    "task_file": "C:/Users/me/.npa-mcp/task.md",
    "resume_session_id": "",
    "model": "",
    "max_turns": 100,
    "allowed_tools": "mcp__npa_mcp__*,Bash,Read,Edit,Write,Glob,Grep"
  }
}
```

- `days`:Python 星期,`0=周一 … 6=周日`。
- 支持跨午夜窗口(如 `23:00–06:00`)。
- `task_file` 写本窗口要完成的工作;无头 Claude 会读取它。
- `allowed_tools` 是**白名单**:无头模式默认拒绝一切工具授权,必须显式列出。
  绝不使用 `--dangerously-skip-permissions`。
- `resume_session_id` 为空时,自动复用同窗口上一次的会话(`runs/last_session.json`),
  实现跨夜"续做"。

---

## 4. MCP 工具

所有工具返回 `{"ok": bool, ...}`;失败附中文 `error` 与 `hint`。

### 4.1 状态

| 工具 | 说明 |
|---|---|
| `list_targets` | 环境总览:配置是否完整、文件服务状态、会话、工作窗口。凭据掩码 |
| `open_config_page` | 在浏览器打开配置页 |

### 4.2 代码服务器

| 工具 | 说明 |
|---|---|
| `ssh_exec(command, timeout, cwd)` | 远程执行命令,返回 stdout/stderr/exit_code |
| `remote_read_file(remote_path, max_bytes)` | SFTP 读文本文件(默认 2MB) |
| `remote_write_file(remote_path, content, mode)` | 原子写入(临时文件 + rename,保留 `.bak`) |
| `remote_list_dir(remote_dir)` | 列目录(目录优先,最多 500 条) |
| `ssh_upload(local_path, remote_path)` | 本机 → 远程 |
| `ssh_download(remote_path, local_path)` | 远程 → 本机(默认 `image_dir`),返回 MD5 |

远端路径支持 `~` 和相对 `workdir` 的相对路径。

### 4.3 开发板

| 工具 | 说明 |
|---|---|
| `device_exec(command, timeout, kind)` | `auto` 按 prefer 依次尝试 SSH/Telnet/串口 |
| `device_pull_firmware(filename, remote_path, method, token, verify, timeout)` | 设备主动拉固件,默认 MD5 校验 |
| `device_push_firmware(filename, remote_path, verify)` | 设备有 sshd 时 SFTP 直推 |

`device_pull_firmware` 的 `method`:

- `auto`:按正在运行的服务依次选 `tftp` → `wget` → `ftpget`;
- 显式指定时必须对应服务在运行,否则报错并说明原因;
- `scp` 需显式指定(从代码服务器拉,而非本机文件服务)。

### 4.4 交互会话

| 工具 | 说明 |
|---|---|
| `session_open(kind, label)` | 打开/复用会话,返回 `session_id` + `terminal_url` |
| `session_send(session_id, data, newline)` | 发送原始字符(交互菜单、uboot) |
| `session_read(session_id, since_seq, lines, wait_pattern, timeout)` | 读增量 / 等正则匹配 |
| `session_list()` | 列出所有会话 |
| `session_close(session_id, force)` | `force=false` 只隐藏视图;`true` 断开底层 |

`session_read(wait_pattern=...)` 是等待编译/烧录结果的标准用法:
匹配成功返回 `matched=true`;超时返回已收到的全部文本,不会丢日志。

### 4.5 电源

| 工具 | 说明 |
|---|---|
| `power_on(outlet, settle_sec)` | 上电;`settle_sec` 给设备启动留时间 |
| `power_off(outlet, settle_sec)` | 断电 |
| `power_status(outlet)` | 查询,返回 `state=on/off/unknown` |

### 4.6 定时与报告

| 工具 | 说明 |
|---|---|
| `schedule_register(window)` | 注册 Windows 任务计划;空参数注册全部窗口 |
| `schedule_status()` | 注册状态、下次/上次运行、最近一次无头结果 |
| `schedule_remove(task_name, window)` | 删除任务(只允许 `npa-mcp-` 前缀) |
| `work_window_check()` | AI 开工前自查:当前是否在窗口内、剩余秒数 |
| `generate_report(...)` | 生成中文 HTML 报告,可选附会话日志 |

---

## 5. 典型工作流

### 5.1 改代码 → 编译 → 更新设备

```
ssh_exec("git pull")
remote_read_file("src/main.c")
remote_write_file("src/main.c", "...")
ssh_exec("make -j8")
ssh_download("build/firmware.bin")          # 落到 image_dir,返回 MD5
device_pull_firmware(filename="firmware.bin")  # 设备 tftp 拉取 + md5 校验
device_exec("reboot")
```

### 5.2 串口看启动日志

```
session_open(kind="serial")
session_read(session_id, wait_pattern="login:", timeout=60)
session_send(session_id, "root")
session_send(session_id, "密码")
device_exec("ifconfig")
```

### 5.3 断电重启

```
power_off()
power_on(settle_sec=5)
device_exec("uptime", kind="telnet")
```

### 5.4 定时自主开发

1. 在配置页添加 `work_windows`,写好 `task_file`;
2. 调用 `schedule_register`;
3. 到点后 Windows 任务计划启动 `pythonw -m npa_mcp.sched_run --window <name>`;
4. 无头 Claude 在窗口内工作,窗口结束被 `taskkill /T /F` 收掉;
5. 结果写入 `~/.npa-mcp/logs/runs/last_run.json`,Claude 在会话内调用
   `generate_report` 生成报告。

---

## 6. 防火墙(开发板需要访问本机时)

以管理员身份运行 PowerShell:

```powershell
netsh advfirewall firewall add rule name="npa-mcp TFTP" dir=in action=allow protocol=UDP localport=69
netsh advfirewall firewall add rule name="npa-mcp FTP" dir=in action=allow protocol=TCP localport=2121
netsh advfirewall firewall add rule name="npa-mcp FTP-Passive" dir=in action=allow protocol=TCP localport=30000-30100
netsh advfirewall firewall add rule name="npa-mcp HTTP" dir=in action=allow protocol=TCP localport=8765
```

删除:

```powershell
netsh advfirewall firewall delete rule name="npa-mcp TFTP"
netsh advfirewall firewall delete rule name="npa-mcp FTP"
netsh advfirewall firewall delete rule name="npa-mcp FTP-Passive"
netsh advfirewall firewall delete rule name="npa-mcp HTTP"
```

> TFTP 使用 69/UDP,Windows 上没有内置 tftp 客户端;自测可用
> `dev/tftp_get.py`。

---

## 7. 目录与运行数据

```
npa-mcp/
├── src/npa_mcp/            # 源码
│   ├── server.py           # FastMCP 工具与进程生命周期
│   ├── config.py           # 配置模型 / 热加载 / 掩码
│   ├── transports.py       # SSH 连接池与 SFTP
│   ├── sessions.py         # 串口/Telnet/SSH 会话与镜像终端
│   ├── power.py            # PDU 客户端
│   ├── fileservers.py      # TFTP / FTP / HTTP
│   ├── webapp.py           # FastAPI 路由与 WebSocket
│   ├── scheduler.py        # schtasks XML / 注册 / 查询
│   ├── sched_run.py        # 无头 Claude 窗口入口
│   ├── report.py           # HTML 报告
│   └── web/                # 模板、xterm.js(本地 vendored)
├── scripts/install.ps1     # 安装脚本
├── scripts/sched_run.cmd   # 手动测试任务入口
├── dev/                    # 假设备 / TFTP 客户端(冒烟测试用)
└── tests/                  # pytest
```

运行期数据(不放进项目目录):

```
~/.npa-mcp/
├── config.json             # 配置(含明文凭据)
├── task.md                 # 默认任务文件示例(需自行创建)
├── web_port.txt            # 最近一次实际网页端口
├── images/                 # 固件目录(TFTP/FTP/HTTP 根)
├── reports/                # HTML 报告
└── logs/
    ├── npa-mcp.log         # 主日志(轮转)
    └── runs/               # 任务计划 XML、无头 Claude 日志、last_run.json
```

---

## 8. 安全说明

- 配置页、终端页、WebSocket、管理 API 默认只监听 `127.0.0.1`。
- `list_targets` 只返回掩码后的凭据(`***` / 未配置)。
- **工具层没有写凭据的入口**,凭据只能由用户在网页里填写。
- 无头任务显式使用 `--allowedTools` 白名单,不使用 `--dangerously-skip-permissions`。
- v1 将凭据明文保存在 `config.json`。建议限制文件权限:

```powershell
icacls "$env:USERPROFILE\.npa-mcp\config.json" /inheritance:r /grant:r "$env:USERNAME:(R,W)"
```

- HTTP 镜像服务可能暴露到局域网,建议设置 `firmware_server.http.token`,
  并只在可信网络启用。
- `remote_write_file` 与 `ssh_download` 都限制在允许的目录内;相对路径不能越界。

---

## 9. 常见问题

**Q:工具返回"代码服务器未配置"**
打开 `open_config_page`,填写 `code_server` 的 host / username / 密码或私钥。
私钥路径必须是 Windows 本机可读的绝对路径。

**Q:串口打不开,提示拒绝访问**
串口被其他程序占用。关闭 PuTTY / MobaXterm / 串口助手后重试;COM10 以上
本项目会自动加 `\\.\` 前缀。

**Q:`device_exec` 总是超时**
检查配置页的 `shell_prompt` 是否与设备提示符完全一致(含空格)。超时返回里
有原始输出,可据此判断命令是否执行。也可以改用 `session_send` + `session_read`。

**Q:设备 tftp 拉取超时**
1) 确认开发板能 ping 通本机 `firmware_server.host`;2) 放行防火墙 69/UDP;
3) 某些 busybox tftp 不支持 `-r`,改用 `tftp -g -l <本地文件> <host>`,在
`pull_templates` 中修改;4) 默认 `verify=true`,失败会明确报错。

**Q:FTP 登录成功但列目录/传输卡住**
被动端口 `30000-30100` 未放行,或 `masquerade_address` 不是开发板可达的
局域网 IP。放行端口,或在配置页把 `firmware_server.host` 固定为本机 LAN IP。

**Q:网页打不开**
查看 `~/.npa-mcp/logs/npa-mcp.log` 与 `list_targets.web.error`。端口被占用时
会自动 +1,实际端口见 `list_targets.web.port` 或 `web_port.txt`。用
`--no-web` 启动时不会提供网页。

**Q:注册任务计划失败**
需要当前用户有创建任务的权限;把任务名改成 ASCII(`npa-mcp-<slug>` 已自动
处理)。可在"任务计划程序"中手动查看 `npa-mcp-*` 任务。

**Q:定时任务到点没跑**
在任务计划程序中查看"上次运行结果";再看
`~/.npa-mcp/logs/runs/last_run.json`。若 `skipped=outside_window`,说明任务
在窗口外补跑,已被窗口守卫主动跳过(这是预期行为,避免白天消耗 token)。

**Q:无头 Claude 提示找不到命令**
设置环境变量 `NPA_CLAUDE_CMD` 指向 `claude.cmd` 的完整路径,然后重新注册任务。

**Q:配置文件损坏**
MCP 启动时会备份为 `config.json.corrupt-<时间戳>` 并生成默认配置;从备份
恢复需要的字段后保存即可。

---

## 10. 开发与测试

```powershell
# 安装开发依赖
.\scripts\install.ps1 -WithDev

# 单元测试
& "$env:LOCALAPPDATA\npa-mcp\venv\Scripts\python.exe" -m pytest -q

# 只启动网页(不接 MCP)
& "$env:LOCALAPPDATA\npa-mcp\venv\Scripts\python.exe" -m npa_mcp --no-web
```

本地假设备(无需真实硬件):

```powershell
# 假开发板(telnet) / 假 PDU / 假串口 shell
python dev\mock_telnet_device.py --port 2323
python dev\mock_telnet_pdu.py --port 2324
python dev\mock_serial_shell.py --port COM5

# 没有 tftp.exe 时,用 Python 客户端验证 TFTP 服务
python dev\tftp_get.py 127.0.0.1 firmware.bin
```

---

## 11. 已知边界

- Telnet/串口回显剥离基于"下一行等于命令"的启发式,失配时放弃剥离,
  可能出现一行重复;不影响命令执行。
- TFTP 偶发丢包/超时,因此 `device_pull_firmware` 默认开启校验。
- 同一时间只支持一个 Telnet/串口常驻会话;SSH 可并发。
- 双实例运行时,第二个实例的 TFTP/FTP 会因端口占用降级(HTTP 仍可用)。
- 凭据明文存储(v1);后续可选 Windows DPAPI 加密。
- HTTP 镜像服务面向局域网,建议启用 token。
