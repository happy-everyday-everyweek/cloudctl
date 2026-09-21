# cloudctl

Windows 实例群的集中控制与素材归档系统。客户端（agent）+ 云控服务端（server）+ 内网穿透通道。

> 用途限定：仅用于你本人拥有或已获得明确授权的 Windows 实例。

## 组件

| 目录 | 内容 | 运行环境 |
| --- | --- | --- |
| `agent/` | Windows 客户端，PyInstaller 打包为单文件 exe | Windows 10/11 |
| `server/` | 云控服务端，FastAPI + WebSocket + 网页控制台 | Win/Linux，需可被访问 |
| `deploy/` | frp 内网穿透配置与安装脚本 | 服务端 + 各实例 |
| `.github/workflows/` | 在 windows-latest 上自动打包 agent exe | GitHub 托管 |

## 功能

- 定时/条件触发的屏幕录制与摄像头拍照，触发条件（前台窗口切换、空闲状态、PPT 放映）由云控规则决定
- 开机自启（Windows 计划任务，SYSTEM 上下文，无需登录即可运行）
- 接收云控规则：WebSocket 主通道 + GitHub 仓库文件轮询备用通道
- 远程桌面：JPEG 帧推流 + 鼠标键盘事件回注
- 远程终端：长驻 PowerShell 会话，支持交互式命令
- 远程文件管理：列目录、上传、下载、删除、重命名、新建目录
- 素材自动归档：扫描 PPT/文档/图片/音频/视频，去重后按类型上传到指定 GitHub 仓库

## 快速开始

1. 服务端：`cd server && pip install -r requirements.txt && python -m app.main --host 0.0.0.0 --port 8787`
2. 打通通道：`deploy/frpc.toml` 内指向你的 frps 地址，或用服务端所在机器的公网地址
3. 客户端：`agent/config.json` 填 `server_url`、`server_token`、`device_id`
4. 访问服务端网页控制台，设备上线后即可下发规则与命令

详细协议见 `docs/ARCHITECTURE.md`，部署步骤见 `docs/DEPLOY.md`。

## GitHub 作为存储后端的上限

上传到 GitHub 仓库有几条硬限制，规划素材归档时必须先算好：单文件超过 100 MB 无法通过 API 提交，超过 50 MB 会告警；单次 Contents API 请求建议不超过 40 MB；仓库总体积 GitHub 有软性建议上限，长期堆积大量视频会被限制。因此视频类素材默认按 `segment_s` 切片，或改用 release 附件与对象存储。
