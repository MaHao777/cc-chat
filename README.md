# AICHAT / cc-chat

cc-chat 是一个运行在本机的持续陪伴服务：Claude Code 负责生成角色与生活，SQLite 负责状态、记忆和计划，cc-connect 负责微信收发。所有管理接口只监听 `127.0.0.1`。

## 项目介绍

### 它是做什么的

cc-chat 不是问答机器人，也不是把聊天记录塞进提示词的套壳。它跑一个**连续存在的虚构角色**：有自己的身份、作息、正在进行的活动和第一人称日记。角色会主动找你说话，也会判断此刻不适合打扰而把消息延后；它记得你上次说过什么，也记得自己答应过什么，并且会随着时间遗忘不重要的事。

**角色由你定义，没有任何预设。** 首次使用要做的第一件事，是在「角色设定」页写一段设定（例如“在旧书店打工的年轻人，话少，喜欢胶片摄影，和室友合租”），由模型展开成完整角色。不写就不会有角色——服务会一直待机，**绝不会自己发明一个人出来**。生成之后每一项都可以直接改；换角色只需改设定重新生成。

角色是明确虚构的，关系从朋友自然发展。系统不会编造你的真实经历，也不会把模型不知道的事当成事实——上下文里没有的，就是它不知道或已经忘了。

服务以一个后台进程常驻，按秒级循环推进：到点处理对话、生成日程、把已经发生的活动写成经历、在每天 23:30 之后整理日记与记忆。

### 它不是什么

- **不是多用户服务**。绑定一个微信会话，`allow_from` 收窄到该会话，其他来源一律拒绝。
- **不是云端应用**。所有状态在本地 SQLite 与文件系统，不依赖外部数据库，不上传数据；向量模型也在本机 CPU 上跑。
- **不是通用 Agent**。角色不能读写文件、不能联网、不能开工具，只能按给定 Schema 返回结构化 JSON。
- **不是群发工具**。没有每日联系配额，也不会向绑定会话之外的任何人发消息。

### 三个部件

| 部件 | 角色 | 边界 |
| --- | --- | --- |
| Claude Code | 唯一的生成与判断层。六个任务全部要求严格 JSON 输出 | 无头调用，禁工具、禁 MCP、禁会话持久化 |
| SQLite | 唯一的状态源：角色、消息、记忆、图谱、日程、日记、计划、外发信箱、审计 | `data/cc-chat.sqlite3`，WAL 模式 |
| cc-connect | 唯一的微信出入口，负责登录、收发与重连 | 配置文件 `~/.cc-connect/config.toml`，由 `migrate` 改写 |

Python 侧只做三件事：组装上下文、校验模型输出、把决定落库并逐步发送。**任何生成内容都不经过 cc-connect 的会话通道**——适配器对 cc-connect 永远只回 `NO_REPLY`，真正的回复走后台的外发信箱。这样“模型说话”和“平台回执”是两条互不干扰的链路。

### 一条消息的完整路径

1. 微信来消息 → cc-connect 以 `claudecode` 类型的 agent 拉起 `python -m cc_chat.adapter`，用 stream-json 通信。
2. 适配器先校验 `CC_PROJECT` 与 `CC_SESSION_KEY` 是否匹配本机绑定，不匹配直接退出；通过后把消息**原子写入** `data/inbox/*.json`，再带令牌 POST `/internal/incoming`。后台没起来时消息留在投递箱，由后台的 drain 循环每 2 秒捡起，不会丢。
3. `Engine.receive` 校验会话与长度，按 `source_key` 去重，提升全局 `revision`，取消上一轮未完成的计划与待发消息，插入用户消息，再按 `message_wait_seconds`（默认 10 秒）排一个 `turn` 任务——这段时间内的新消息会重置计时并合并进同一轮。
4. `turn` 到期，引擎组装上下文（角色、当前状态、最近消息、检索到的记忆、已发生事件、最近日记、未来日程、旧计划），按字符预算裁剪，调用 Claude Code 的 `decision` 任务。
5. 决定先**整体校验**再落库：延后必须有未来时间、即时回复不能带时间、单条不超长、不得出现 `NO_REPLY`。校验通过后写入记忆、更新状态、记录审计。
6. 要回复时，每条消息写进 `outbox`，由 `flush` 逐条经 cc-connect CLI 发出，碎片之间等 `fragment_delay_seconds`（默认 1.2 秒）。这段时间锁是放开的，新消息可以打断剩余碎片。
7. 发送结果回写 `sent` / `failed` / `unknown`。`unknown` 表示“可能已送达”，此时**不会重发**，并且同一轮的后续碎片一并作废。

### 技术栈

| 层次 | 选型 | 说明 |
| --- | --- | --- |
| 语言 | Python ≥ 3.12 | 单一 pip 包，入口 `cc-chat` 与 `cc-chat-adapter` |
| Web | FastAPI + Uvicorn + Jinja2 | 仅监听 `127.0.0.1`，无 CDN、无外部脚本 |
| 校验 | Pydantic v2 | 所有模型输出走 `extra="forbid"` 的严格模型 |
| 存储 | sqlite3（标准库） | 单文件，WAL，无 ORM |
| 向量 | sentence-transformers + `BAAI/bge-small-zh-v1.5` | CPU、离线、本地缓存，约 92 MB |
| 推理 | Claude Code CLI | `-p --output-format json --json-schema`，结构化输出 |
| 通道 | cc-connect CLI | 微信登录与收发，子进程调用 |

## 特点

### 1. 角色有自己的生活，不等你开口

- **每日日程**：每天首次 tick 时按 `Asia/Shanghai` 生成当天的活动骨架（课程、吃饭、自习、回宿舍），校验 `at < until`、不重叠、不回填过去，写入 `events` 表。
- **活动变成经历**：活动到点后，模型把它写成“正在发生的具体经历”和当时的状态，记忆以 `source_type=virtual` 落库。**活动只成为角色的回忆，绝不会变成发给你的消息**——错过的时间在唤醒后按生活史补齐，而不是补发通知。
- **第一人称日记**：每天 23:30 之后，把当天已发生的事件和真实聊天整理成日记。已有日记时只写补记，且只喂入尚未被覆盖的新素材。
- **主动联系**：模型在每轮决定里可以给出 `next_plan`（时间、意图、成立条件、理由），到点后**重新判断**是否真的说话，而不是播放预写好的文案。
- **延迟回复**：`defer` 把回复推迟到未来某个时刻，保证“现在不方便，过会儿回你”这件事真的会发生。
- **硬暂停**：管理页的“暂停主动联系”只拦截主动消息，你对角色说的话照常回复。

### 2. 模型只负责推理，状态全部落在 SQLite

- 六个任务各有独立提示词：`persona`（按你的设定展开角色）、`decision`（此刻说不说、说什么）、`schedule`（排日程）、`experience`（写经历）、`diary`（写日记）、`compress`（压缩琐事）。
- 每次调用都带 `--json-schema`，返回后用 Pydantic 严格模型校验；字段类型、取值范围、时间必须含时区，不合规直接判为失败。
- 子进程环境**被主动裁剪**：过滤掉 `CC_*` 与 `CLAUDECODE`，显式关闭自动记忆与插件执行；禁工具、禁 MCP、禁会话持久化、cwd 指向 `runtime/`。角色读不到项目文件，也读不到你的其他会话。
- 上下文由引擎拼装，并受**总量预算**约束（默认 18000 字符）：先按节裁剪日记/事件/日程/旧计划，再裁最近消息，再裁记忆，最后才截断角色设定里的长字符串——保证裁剪后结构不变、字段不丢。
- 模型失败不会污染状态：错误写入 `last_error` 与审计，冷却按 `10 × 2^n` 秒退避（上限 300 秒）；任务最多重试 3 次，且**只有在还没产生任何外发副作用**时才重试。

### 3. 记忆是可追溯、可更正的原子事实

- **原子化**：每条记忆带 `subject`、`kind`（fact / preference / experience / promise / feeling / inference）、`entities`、`topics`、`importance`、`confidence`、`occurred_at`。不是聊天记录的切片。
- **四路加权检索**：语义（本地向量余弦）、连接（记忆图谱一跳扩散）、关键词（中英混合，中文按 bigram）、时间（指数衰减，事实与偏好半衰期 180 天，其余 14 天）。默认权重 `0.45 / 0.25 / 0.20 / 0.10`，可在管理页实时调整并立即生效，无需重启。
- **来源必须可验证**：模型给出的 `source_ids` 必须落在本轮上下文真实存在的 ID 里；声明 `user` 来源时对应消息必须真的是用户发的；声明 `virtual` 来源时对应事件必须已经是 `occurred`。**来源决定可信度，模型的自述不算数。**
- **去重**：以“内容 + 来源集合”的 SHA-256 作为主键，同一件事不会因为重复抽取而堆积。
- **更正**：只有明确标记 `explicit_correction` 的记忆才能顶替旧条目；用户来源的事实不能被模型自己的推测覆盖。
- **遗忘是级联的**：压缩、遗忘、删除时，会一并隐藏源消息、由它派生的回复、相关日记与尚未执行的计划——被忘掉的事不会从别处漏回来。彻底删除会留下不含内容的墓碑记录，便于审计。
- **自然衰减**：长期未被使用且重要度低于阈值的记忆，先由模型压缩成“不含姓名、日期、数字和可反推细节”的模糊概括，再彻底遗忘（`compress_days=30`、`forget_days=90`）。

### 4. 不重复、不丢失、不谎报

- **幂等接收**：`source_key` 唯一约束，同一消息重复投递只返回原记录。
- **乐观并发**：每轮对话提升 `revision`，过期的决定、过期的待发消息一律作废；新消息会顺带取消上一轮尚未发出的碎片。
- **三态外发**：`pending → sending → sent`，发送失败或超时记 `unknown`（可能已送达），此时**不自动重发**，并停止本轮后续碎片。
- **崩溃恢复**：启动时把中断在 `sending` 的记录统一降级为 `unknown`，残留的 `pending` 碎片**不重放**，而是重新交给模型判断。
- **决定先校验后落库**：校验失败不会产生任何半成品效果。

### 5. 说人话的节奏

- 收到消息后先等 10 秒再回答，连发几条会被合并成一轮，而不是逐条回复。
- 普通回复**最多两条短消息**，且要求先回应最重要的一点，避免总结、复述和连续铺陈；只有模型明确判定“确实值得分享”时才用 `share` 模式拆成三到五条短消息，模拟忍不住想讲一件事的样子。
- 碎片之间逐条发送并留出间隔；发送间隔期间释放锁，你中途发消息可以把它打断。
- 提示词明确禁止客服口吻，禁止把规划、评分、JSON 或后台逻辑暴露给聊天对象。

### 6. 单用户、只在本机

- 服务与管理页只监听 `127.0.0.1`，并叠加 `TrustedHostMiddleware`。
- `/api/` 与 `/internal/` 需要 `data/access.token`（Bearer 或 `X-CC-Chat-Token`），用常量时间比较；令牌首次启动自动生成。
- 非 GET 请求拒绝跨站 `Origin`；所有响应带 `no-store`、`nosniff`、`X-Frame-Options: DENY` 与严格 CSP，前端不使用任何外部资源。
- 微信侧 `allow_from` 收窄到绑定会话，适配器再校验一次 `project` 与 `session`。
- 真实微信模式下管理页不提供“冒充用户发消息”的入口——本地测试只能在 `transport = "mock"` 时进行。
- 模型子进程的 stderr **不落盘**，避免把供应商凭据写进日志。

### 7. 可观察、可调、可回滚

- 四个管理页入口：`/` 此刻与联系、`/persona` 角色设定、`/life` 生活与日记、`/memories` 记忆抽屉。可以看到状态、下一次联系、待发计划、外发消息、最近消息、审计记录、记忆与日记计数。
- **检索记录**保留每次检索的查询与命中结果（含四路得分），可以回看“角色当时为什么想起这件事”。
- 审计表记录角色更新、每次决定、发送结果、运行错误等关键事件。
- 唯一可在页面上改的是记忆参数；其余参数改配置文件后 `stop` 再 `start`（设置在启动时读取一次）。
- `migrate` 会先备份 `config.toml` 与 `config.local.json` 到 `backups/<timestamp>/`，并输出带 SHA-256 的 manifest；`restore` 校验“安装后是否被其他程序改过”，不匹配就拒绝覆盖，除非显式 `--force`。

### 8. 部署与运维

- 一个 pip 包搞定，无需外部数据库或消息队列。
- `install-startup` 优先注册计划任务，被权限策略拒绝时自动回落到当前用户的“启动”文件夹，两者作用域一致。
- `runtime.lock` 文件锁保证同一份数据只有一个后台实例；`stop` 会先核对进程命令行再结束进程，避免误杀。
- cc-connect 断网或代理不可用时会保持进程存活并自动重连；这期间 cc-chat 不会补发任何结果不确定的消息。

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

### 第一个角色

服务起来之后**还没有角色**：`status` 的 `persona` 是 `null`，后台保持待机，不会生成任何内容，也不会主动联系。到 <http://127.0.0.1:9881/persona> 写一段设定，点「生成角色」：

> 在旧书店打工的年轻人，话少，喜欢胶片摄影，和室友合租。

模型据此展开成完整角色（姓名、年龄、身份、性格、说话习惯、兴趣、日常安排、身边的人），身份、年龄与生活场景以你写的那段为准。生成后每一项都能直接改，点「保存新版本」重新安排未来的生活。想换角色就改写设定再点一次「按这段设定重新生成」——**旧的设定版本全部留在「过去的设定版本」里**。

设定会写进 `config.local.json` 的 `persona_brief`，所以删掉数据库重建时，同一个角色会被原样重建出来。想直接跳过页面，也可以先把这个字段填好再启动。

## 查看与修改设置

管理页在 <http://127.0.0.1:9881>，服务没运行时打不开（先用 `start` 拉起）：

| 地址 | 内容 |
| --- | --- |
| `/` 此刻与联系 | 状态、暂停开关、下一次联系、待发计划、外发消息、最近消息、审计记录、记忆与日记计数 |
| `/persona` 角色设定 | 写下设定生成角色，或逐项修改当前角色 |
| `/life` 生活与日记 | 已发生事件与日记 |
| `/memories` 记忆抽屉 | 记忆列表、检索记录，以及界面上唯一能改的一组设置 |

页面上能写回 `config.local.json` 的只有两处：记忆参数（记忆抽屉页的四类权重 语义/连接/关键词/时间、`compress_days`、`forget_days`、`low_importance`），以及角色设定页的 `persona_brief`。两者保存后立即生效，不需要重启。

其余参数都在 `config.local.json` 里手改，改完必须 `stop` 再 `start`：设置在服务启动时读取一次，运行中不会重载。cc-connect 那一侧（微信 token、`allow_from`、`cli_path`、`work_dir`、显示开关）在 `%USERPROFILE%\.cc-connect\config.toml`。

命令行没有列出设置的子命令，`cc-chat status` 只输出 `transport`、`persona`、`paused`、最近一次错误与地址。消息、记忆、计划、审计的逐条明细在 `data/cc-chat.sqlite3`。

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
