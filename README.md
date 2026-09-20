# AICHAT / cc-chat

cc-chat 是一个运行在本机的持续陪伴服务：Claude Code 负责生成角色与生活，SQLite 负责状态、记忆和计划，cc-connect 负责微信收发。所有管理接口只监听 `127.0.0.1`。

## Quick Start

### 前置要求

| 项目 | 要求 | 说明 |
| --- | --- | --- |
| 系统 | Windows 10/11 + PowerShell | `stop`、`install-startup` 依赖 PowerShell 与计划任务 |
| Python | 3.12 及以上，且**安装路径不含空格** | `requires-python = ">=3.12"`；cc-connect 的 `cli_path` 不支持空格路径 |
| Claude Code | 已安装并完成鉴权 | 默认读取 `%USERPROFILE%\.local\bin\claude.exe`，可用 `claude_binary` 覆盖 |
| Node.js / npm | 用于安装 cc-connect | `npm i -g cc-connect`，默认落在 `%APPDATA%\npm\node_modules\cc-connect\bin\cc-connect.exe` |
| 微信 | 个人微信可扫码登录 | 收发由 cc-connect 负责，cc-chat 只对接本地 bridge |
| 网络 | 首次运行需能访问模型仓库 | 中文向量模型 `BAAI/bge-small-zh-v1.5`，约 92 MB |

另外两项约束来自代码，踩到会直接报错：

- cc-connect 数据目录不能过长：`<cc_data_dir>\run\api.sock` 的 UTF-8 长度必须小于 104 字节，即 `%USERPROFILE%\.cc-connect` 不能是超长用户名下的深层路径。
- 登录自启需要虚拟环境里的 `.venv\Scripts\pythonw.exe`（venv 自带，删掉就无法安装自启）。

### 首次安装

```powershell
cd D:\AICHAT

# 1. 创建虚拟环境并安装（含测试依赖）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 2. 下载中文向量模型（必须先做一次）
.\.venv\Scripts\python.exe -m cc_chat.cli prepare-model
```

`embedding_local_only` 默认是 `true`，记忆检索只读本地模型缓存；`prepare-model` 会临时放开该限制做一次下载，并输出同义句与无关句的余弦值作为自检（前者应明显更高）。网络受限时先设置镜像再执行：

```powershell
$env:HF_ENDPOINT = "https://hf-mirror.com"
```

### 配置本机参数

`config.local.json` 缺失时 `Settings.load()` 使用内置默认值，此时 `transport = "mock"`，消息只写进内存不会真的发出。要接微信，在项目根写这个文件：

```json
{
  "data_dir": "D:\\AICHAT\\data",
  "runtime_dir": "D:\\AICHAT\\runtime",
  "cc_binary": "C:\\Users\\<用户名>\\AppData\\Roaming\\npm\\node_modules\\cc-connect\\bin\\cc-connect.exe",
  "claude_binary": "C:\\Users\\<用户名>\\.local\\bin\\claude.exe"
}
```

`data_dir`、`runtime_dir` 不写则默认取项目下的 `data/` 与 `runtime/`；`transport` 与 `peer_session` 由下面的 `migrate` 写入，不用手填。注意 `migrate` 固定读写项目根的 `config.local.json`，用 `--config` 指向别处时它不会跟着走。

### 绑定微信入口

`migrate` 要求 `%USERPROFILE%\.cc-connect\config.toml` 里已经存在 `name = "default"`、`agent.type = "claudecode"` 且带 weixin 平台的项目，否则拒绝执行（项目名可用 `project` 配置项改）。项目缺失时以 `cc-connect config example` 的输出为模板补一个。先扫码登录，再取得会话标识：

```powershell
cc-connect weixin setup     # 扫码登录个人微信，写入 ~/.cc-connect/config.toml
cc-connect sessions list    # 列出会话；微信标识形如 weixin:dm:<对方标识>@im.wechat
```

会话标识需要先用微信给这个号发一条消息才会出现。拿到后先停止 cc-connect（`migrate` 会原地重写 `config.toml`），再执行迁移：

```powershell
.\.venv\Scripts\python.exe -m cc_chat.cli migrate --session "weixin:dm:<对方标识>@im.wechat"
```

`migrate` 会把该项目的 `work_dir` 指向 `runtime/`、`cli_path` 指向 `python -m cc_chat.adapter`、`allow_from` 收窄为这个会话，并把原始 `config.toml` 与 `config.local.json` 备份到 `backups/<timestamp>/` 后输出 manifest。之后重新启动 cc-connect 生效。

### 启动与验证

```powershell
.\.venv\Scripts\python.exe -m cc_chat.cli start
.\.venv\Scripts\python.exe -m cc_chat.cli status
Start-Process http://127.0.0.1:9881
```

`status` 返回 `transport`、`persona`、`paused` 与最近一次错误。管理页首次启动时生成 `data/access.token`，页面本身只监听 `127.0.0.1`，`/api/` 与 `/internal/` 还需带上该令牌。日常启停、登录自启与回滚见下文。

## 当前运行状态

当前微信入口已经绑定到 cc-chat 的默认项目，原 cc-connect 配置备份在迁移命令输出的 `backups/<timestamp>` 目录。原工作目录不会被移动或删除。

```powershell
cd D:\AICHAT
.\.venv\Scripts\python.exe -m cc_chat.cli status
Start-Process http://127.0.0.1:9881
```

管理页包含状态与下一次联系、角色设定、生活与日记、记忆抽屉四个入口。页面里的“暂停主动联系”是硬暂停；恢复后到期计划会重新由 AI 判断。

收到用户消息后，后台默认等待 10 秒再让 AI 回答；这段时间内的新消息会重置计时并合并进同一轮上下文。普通回应最多发送两条短消息，只有模型判断确实有值得分享的事情时才会拆成三到五条，并默认间隔 1.2 秒逐条发送。两个参数可在 `config.local.json` 中调整：`message_wait_seconds` 与 `fragment_delay_seconds`。

## 启停

```powershell
# 启动 cc-chat（后台）
.\.venv\Scripts\python.exe -m cc_chat.cli start

# 停止 cc-chat
.\.venv\Scripts\python.exe -m cc_chat.cli stop

# 安装当前用户登录时自动启动 cc-chat 和 cc-connect
.\.venv\Scripts\python.exe -m cc_chat.cli install-startup
```

cc-connect 使用当前用户的 `~/.cc-connect/config.toml`，运行时日志可在 `data\cc-connect.stdout.log` 与 `data\cc-connect.stderr.log` 查看。微信网络或代理暂时不可用时，cc-connect 会保持进程存活并自动重连，cc-chat 不会补发不确定的消息。

## 回滚微信入口

迁移前的配置是可校验的原始副本。停止 cc-connect 后执行：

```powershell
.\.venv\Scripts\python.exe -m cc_chat.cli restore D:\AICHAT\backups\<timestamp>
```

命令会在发现备份后配置已被其他程序修改时拒绝覆盖；只有明确加 `--force` 才会强制恢复。恢复后重新启动 cc-connect 即可回到原项目。

## 开发与测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check cc_chat tests
```

协议测试默认不触碰真实微信，设置 `CC_CHAT_PROTOCOL_TEST=1` 后会用短临时目录启动已安装的 cc-connect bridge 做完整收发验证。真实 Claude Code 的结构化调用测试设置 `CC_CHAT_LIVE_TEST=1`，测试数据是临时合成内容。

## 数据目录

- `data/cc-chat.sqlite3`：角色、生活、记忆、消息、计划和审计记录。
- `data/access.token`：仅本机管理接口令牌，不要提交或分享。
- `data/inbox/`：cc-connect 适配器无法访问后台时的原子消息投递箱。
- `runtime/`：角色的 Claude Code 工作目录，不包含用户原笔记。
- `backups/`：cc-connect 迁移前的配置副本。
