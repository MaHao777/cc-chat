# cc-connect 静默排队补丁

cc-connect v1.3.4 的 `queueMessageForBusySession` 在启动中或忙碌时无条件发送
“📬 消息已收到，将在当前任务完成后处理。”，不受 thinking/tool 开关影响。
本补丁让 `display.mode = "quiet"` 时跳过成功入队回执，保留队列、顺序、
队列已满与处理失败提示，其他显示模式行为不变。

上游源码：https://github.com/chenhg5/cc-connect/blob/v1.3.4/core/engine.go

构建（需要 Go 1.25+ 和 Git；脚本会验证源码归档 SHA-256）：

```powershell
.\scripts\build-cc-connect.ps1
# 或指定便携 Go
.\scripts\build-cc-connect.ps1 -GoBinary 'D:\AICHAT\.cache\go\bin\go.exe'
```

输出 `runtime/bin/cc-connect.exe`，保留 npm 安装的原版。编译禁用 cc-connect 自带的
Web 前端（源码包不含其构建产物），AICHAT 的管理页不受影响。将 `config.local.json`
的 `cc_binary` 设为这个输出的绝对路径，并将绑定项目的
`~/.cc-connect/config.toml` 中 `[projects.display]` 的 `mode` 设为 `"quiet"`。
`migrate` 也会写入此模式。停止原 cc-connect 后通过 `scripts/start-all.ps1`
启动；该脚本与后台发送程序均读取 `cc_binary`。重新构建前需停止正在使用该文件的实例。

验证：构建脚本运行启动中/忙碌时的静默回执、其他模式、队列顺序、满队列与队列消费测试。
以下协议测试只连接本地测试桥接，不发真实微信：

```powershell
$env:CC_CHAT_PROTOCOL_TEST = '1'
$env:CC_CHAT_TEST_BINARY = 'D:\AICHAT\runtime\bin\cc-connect.exe'
.\.venv\Scripts\python.exe -m pytest tests/test_protocol.py -q
```

升级 cc-connect 时应重新检查上游是否提供原生开关，再移植或移除此补丁，
不要直接让本项目切回未修复的 npm 程序。
