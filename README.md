# cloudctl：Windows 实例群的集中控制与素材归档

> 用途限定：仅用于你本人拥有或已获得明确授权的 Windows 实例。请勿部署到他人设备上。

## 它是什么

一套自托管的实例管理工具。被管机器上跑一个 agent，agent 自己开一个端口、自带一个网页控制台，本机浏览器直接就能管自己；局域网内多台实例之间可以直接互相通信；对外还有两条平行通道：WebSocket（走内网穿透）与 GitHub 仓库文件轮询。所有入口共用同一套命令路由与权限门禁，任一条断了都不影响其余入口。中心服务端（server/）是可选的集中视角，不装也能用。

## 目录结构

```
cloudctl/
├─ agent/            Windows 客户端（Python + PyInstaller 单文件 exe）
│  ├─ cloudctl_agent/
│  │  ├─ devsrv.py       设备本地服务：控制台 + HTTP API + MJPEG
│  │  ├─ devstatic/      设备控制台前端（index.html、console.js）
│  │  ├─ mesh.py         局域网互联：发现、转发、中继、文件互传
│  │  ├─ channel.py      两条平行通道（WS + GitHub，含镜像与退避容错）
│  │  ├─ router.py       命令路由与权限门禁
│  │  ├─ rules.py        规则引擎与触发判定
│  │  ├─ ops.py          系统信息、终端、文件、进程
│  │  ├─ capture.py      抓屏、摄像头、录制
│  │  ├─ desktop.py      桌面流与输入注入
│  │  ├─ sync.py         素材扫描与归档
│  │  └─ startup.py      开机自启
│  └─ run_agent.py
├─ server/           FastAPI 中心服务端 + 网页控制台
├─ deploy/           内网穿透配置（Cloudflare Tunnel 为主，frp 备选）
├─ ctl/              rules.json 等控制文件
└─ docs/             ARCHITECTURE.md、DEPLOY.md、MESH.md
```

## 快速开始

下载 CI 产物 cloudctl-agent-windows，或本地 pyinstaller 构建。把 config.example.json 改名为 config.json，至少填上 devsrv_token 与 gh_rules_repo。

先只验本地：agent --console，浏览器打开 http://127.0.0.1:8788，登录后可看设备信息、日志、桌面画面、命令面板。

再验外联：agent --run，同时拉起本地控制台、局域网互联、WebSocket 通道与 GitHub 通道。

看局域网邻居：agent --mesh-peers；给邻居发命令：agent --mesh-send <device_id> sys.info。

装自启：agent --install；卸载：agent --uninstall；看状态：agent --status。

## 功能

条件触发的自拍与录屏（前台窗口切换、空闲、锁屏、PPT 放映等时机，配额与冷却可配）、开机自启、接收云控规则、远程桌面（MJPEG 帧流 + 输入注入）、远程终端（长驻 PowerShell 会话）、远程文件管理（列表、分段读、写、改、删、哈希）、素材扫描归档（PPT、文档、图片、音视频按类型落库到指定仓库）、局域网互联（邻居自动发现、点对点命令、中继转发、大文件直传）。

## 断网可用性

三条入口彼此独立：局域网内断公网时，互联与本地控制台照常；GitHub 不可达时自动切镜像、用缓存规则、指数退避，并把上报写进本地暂存等网络恢复后补齐。设备端无任何界面与提示，不引入 UI 依赖。

## 安全默认值

capture.photo、capture.video、upload.enabled 默认全部为 false，security.allow_delete 默认 false，需要时手动打开。本地控制台默认只绑 127.0.0.1。互联接口带共享令牌，对端命令走同一权限门禁。服务端与设备端两侧权限是“与”关系，任一侧关闭则不执行。发到公网前请设长令牌，并在 Cloudflare 侧加 Access 策略。

## GitHub 作为存储后端的上限

单文件超过 100MB 无法经 Contents API 提交，超过 50MB 会告警，单次请求建议不超过 40MB；Release 附件单文件上限 2GB。上传模块里 upload_max_single_mb 默认 90，超限文件标记 too_large 跳过不重试。大视频建议走局域网直传或对象存储。

## 文档

docs/ARCHITECTURE.md 讲设计原则、入口、本地服务接口、两条平行通道、命令表、规则结构与存储上限。

docs/MESH.md 讲局域网互联协议：发现、鉴权、中继、文件互传与配置项。

docs/DEPLOY.md 讲部署形态、Tunnel 映射、GitHub 通道、规则下发、验收清单与常见问题。
