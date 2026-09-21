# 架构与协议

## 1. 设计原则

每台 Windows 实例自己就是一个控制端点。设备上跑一个本地服务（devsrv），本机浏览器打开端口即可管理自己，不依赖中心服务端是否在线。

对外联络有两条平行通道：WebSocket（经过内网穿透）与 GitHub 仓库文件轮询。两者地位对等，没有主备之分，各自独立重连、各自独立暂存，任一条可用就能下发命令与规则、上报事件与结果。两条通道同时可达时，命令按 id 去重，不会重复执行。

权限只能收紧不能放开。规则里的 security 开关与设备本地开关是“与”关系，任一侧关闭则不执行。

用途限定：本系统仅用于你本人拥有或已获得明确授权的 Windows 实例。

## 2. 组件

设备端 agent 是 Python 3.11 + PyInstaller 单文件 exe，包含配置、日志、规则引擎、命令执行、采集、桌面流、本地服务、通道、路由、归档、自启十一块。

中心服务端 server 是 FastAPI 应用，提供设备清单、命令下发、规则下发、审计与一个网页控制台，属于可选组件：不部署也能用，只是少一个集中视角。

deploy 目录装穿透配置，目前以 Cloudflare Tunnel 为主，frp 保留作备选。

GitHub 仓库同时充当规则分发与结果回收的存储，以及素材归档的落点。

## 3. 链路

```
                      ┌─────────────── 设备 A ──────────────┐
浏览器(本机) ──HTTP──▶│ devsrv :8788  控制台 + API + MJPEG  │
                      │ router ── ops / capture / sync       │
浏览器(远端) ──隧道──▶│ channel ── WS 通道 / GitHub 通道      │
                      └──────────────────────────────────┘
        WS 通道 ──▶ 中心服务端 /ws/agent（可选）
        GH 通道 ──▶ ctl/rules.json、ctl/cmd/<device>.json、ctl/outbox/<device>.jsonl
```

三条入口指向同一套路由与权限门禁：本地 HTTP、WS 通道、GitHub 通道。本地入口没有外联依赖，这是“更稳定”的来源；两条外联入口互相独立，这是“不至于单点”的来源。

## 4. 设备本地服务

默认只绑 127.0.0.1:8788，需要局域网或隧道可达时把 devsrv_bind 改成 0.0.0.0。鉴权令牌取 devsrv_token，未设置时回退 server_token，再回退 device_id；比较用 hmac.compare_digest。

只读接口包括 GET /api/dev/status（监听状态、端口、请求数、错误数、鉴权方式）、GET /api/dev/info（等同 agent.info）、GET /api/dev/log?lines=N（尾部日志）、GET /api/dev/frame.jpg?q=&monitor=（单帧 JPEG）、GET /api/dev/stream.mjpg?fps=&q=&monitor=（MJPEG 推流，fps 限 1 至 15，质量限 20 至 90）。

写接口包括 POST /api/dev/cmd（body 为 op、args、timeout，走完整路由与权限门禁）、POST /api/dev/input（桌面输入注入）、POST /api/dev/rules（合并规则并落盘）、POST /api/dev/scan（扫描素材）、POST /api/dev/sync（立即归档）。

登录用 GET /api/dev/login?token=xxx 或 POST 同名路径，成功后写 HttpOnly Cookie，后续请求也可改用 X-Token 请求头。

控制内核还没跑起来时（例如只单跑了本地服务），run_cmd 会降级到内置的四个只读能力：agent.info、sys.info、file.list、scan.now，其余命令返回“未启动控制内核”。

## 5. 两条平行通道

WebSocket 通道：agent 主动拨出 wss://<host>/ws/agent?device_id=..&token=..，服务端通过该连接下发命令、规则，并接收结果与事件。重连退避从 reconnect_min_s 递增到 reconnect_max_s。

GitHub 通道：轮询 raw 地址上的 ctl/rules.json 与 ctl/cmd/<device_id>.json，命令放在 commands 数组里；结果批量追加写入 ctl/outbox/<device_id>.jsonl，单文件超 900KB 时只保留最后 2000 行。轮询间隔 gh_poll_s，默认 30 秒。

离线暂存：两条通道各自维护 spool.ws.jsonl 与 spool.gh.jsonl，重新上线时自动补齐；两条通道都断时，本地控制台照常工作，事件不会丢在内存里。

消息格式统一：命令为 {"type":"cmd","id":"...","op":"...","args":{...}}，结果为 {"type":"result","id":"...","op":"...","ok":true,"data":{...}}，事件为 {"type":"event","event":"...","data":{...}}。

## 6. 命令表

系统与自身：sys.info、agent.info、agent.log、agent.rules、agent.autostart。

终端：shell.exec（一次性）、shell.open、shell.write、shell.read、shell.close（长驻 PowerShell 会话）。

文件：file.list、file.pull（分段读，返回 base64）、file.push（写入或追加）、file.mkdir、file.rename、file.delete（默认关闭）、file.hash。

进程：proc.list、proc.kill。

采集：capture.photo（屏幕或摄像头）、capture.video（分段录制）。

桌面：desktop.start、desktop.stop、desktop.input。

归档：scan.now、sync.now、sync.status。

## 7. 规则文件

rules.json 顶层字段：version、device、group、capture（photo、video 各自 enabled、时机、配额）、desktop（monitors、fps、quality）、scan（roots、interval_min、后缀白名单）、upload（repo、branch、prefix、max_single_mb、concurrency）、security（allow_shell、allow_file_write、allow_delete）。

合并策略是深度合并，版本号小于等于当前值时不覆盖。服务端与本地控制台下发的都是同一种结构。

## 8. 归档与 GitHub 存储上限

单文件超过 100MB 无法经 Contents API 提交，超过 50MB 会告警，单次请求建议不超过 40MB；Release 附件单文件上限 2GB。因此 upload.max_single_mb 默认 90，超限文件标记 too_large 并跳过不重试。大视频要么接受跳过，要么切片，要么改走 Release 附件。

## 9. 完成度与待验证

已落地：agent 全部模块、本地控制台前后端、两条平行通道、中心服务端与控制台、穿透与 CI。

待真机验证：SYSTEM 上下文下抓屏是否拿到登录会话桌面；ShellSession 的 stdin 行为；MJPEG 长连接并发数；schtasks 自启在非管理员安装时的回退路径。
