# 架构与协议

## 1. 整体链路

```
控制台(浏览器) ─┐
                ├─ HTTPS/WSS ─→ server (FastAPI) ─→ WebSocket ─→ agent(实例1..N)
自动化脚本 ─────┘                                  └─ GitHub 仓库文件轮询（备用通道）
```

agent 启动后主动向 server 拨出 WebSocket 长连接（设备端不需要开端口，天然穿 NAT）。server 只负责转发命令、存储规则、汇总上报，不主动链接设备。

备用通道用于 server 不可达的情况：agent 周期性拉取规则文件，并把命令执行结果写回仓库。两条通道共享同一套消息格式。

## 2. 主通道消息格式

### agent → server

```json
{"type":"hello","device_id":"pc-lab-01","name":"LAB-PC-01","ver":"1.0.0","os":"Windows-10-19045","caps":["desktop","shell","files","capture","git"]}
{"type":"result","id":"<命令id>","ok":true,"data":{...},"err":null}
{"type":"event","event":"capture.done","data":{"kind":"photo","path":"..."}}
{"type":"frame","stream":"desktop","seq":12,"w":1920,"h":1080,"jpeg":"<base64>"}
{"type":"stream.end","stream":"desktop"}
{"type":"pong","t":1690000000}
```

### server → agent

```json
{"type":"cmd","id":"<uuid>","op":"shell.exec","args":{"cmd":"ipconfig"}}
{"type":"rules","version":7,"rules":{...}}
{"type":"desktop.start","id":"...","args":{"fps":8,"quality":55,"scale":1.0,"monitor":0}}
{"type":"desktop.input","events":[{"k":"move","x":100,"y":200},{"k":"down","btn":"left"}]}
{"type":"desktop.stop"}
{"type":"ping","t":1690000000}
```

## 3. 命令表（op）

| op | args | 返回 |
| --- | --- | --- |
| `sys.info` | 无 | 主机名、用户、CPU、内存、磁盘、网卡、运行时长 |
| `shell.exec` | `cmd`、`timeout_s` | `stdout`、`stderr`、`code` |
| `shell.open` / `shell.write` / `shell.close` | `cmd`、`session` | 长驻交互式会话 |
| `file.list` | `path` | 名称、大小、mtime、is_dir |
| `file.pull` | `path` | 分块 base64 或直传 server 中转 |
| `file.push` | `path`、`data_b64` | 写入结果 |
| `file.mkdir` / `file.rename` / `file.delete` | `path`、`new_path` | 结果 |
| `proc.list` / `proc.kill` | `pid` | 进程列表 / 结果 |
| `capture.photo` | `device`（screen/cam0）、`path` | 保存路径 |
| `capture.video` | `duration_s`、`fps`、`monitor` | 保存路径 |
| `scan.now` | 无 | 扫描统计 |
| `sync.now` | 无 | 上传统计 |
| `agent.update` | `rules` 或 `url` | 应用结果 |

## 4. 规则文件（rules.json）

规则是整个系统的“策略来源”，由服务端下发，agent 缓存到 `rules.json` 并在断网时继续按缓存执行。

```json
{
  "version": 7,
  "identity": {"display_name": "LAB-PC-01", "tags": ["lab"]},
  "capture": {
    "photo": {
      "enabled": true,
      "device": "cam0",
      "min_interval_s": 600,
      "max_per_day": 50,
      "active_hours": ["08:00", "22:00"],
      "when": {
        "only_if_idle_s": 120,
        "on_session_unlock": true,
        "on_foreground_change": true,
        "min_gap_same_window_s": 300
      }
    },
    "video": {
      "enabled": true,
      "segment_s": 60,
      "fps": 5,
      "quality": 60,
      "max_mb_per_day": 500,
      "active_hours": ["08:00", "22:00"],
      "when": {
        "on_foreground_change": true,
        "on_slideshow": true,
        "idle_skip": true,
        "exclude_titles": ["*隐私*", "*Password*"]
      }
    },
    "slideshow": {
      "enabled": true,
      "titles": ["PowerPoint 幻灯片放映", "Presentation"],
      "on_slide_change": "dhash",
      "threshold": 12,
      "cooldown_s": 3
    }
  },
  "scan": {
    "roots": ["C:/Users/Public/Documents", "D:/course"],
    "interval_min": 30,
    "follow_links": false,
    "types": {
      "doc":   ["ppt", "pptx", "doc", "docx", "pdf", "xls", "xlsx", "md", "txt"],
      "image": ["jpg", "jpeg", "png", "webp", "gif", "bmp", "heic"],
      "audio": ["mp3", "wav", "flac", "m4a", "aac", "ogg"],
      "video": ["mp4", "mov", "mkv", "avi", "webm"]
    },
    "exclude_globs": ["*/node_modules/*", "*/.git/*", "*/AppData/Local/Temp/*", "*/Windows/*"]
  },
  "upload": {
    "enabled": true,
    "repo": "happy-everyday-everyweek/knowledge-base",
    "branch": "main",
    "path_prefix": "kb",
    "layout": "{prefix}/{type}/{yyyy}/{mm}/{name}",
    "dedup": "sha256",
    "max_single_mb": 90,
    "chunk_video": true,
    "chunk_seconds": 600,
    "concurrency": 2,
    "rate_limit_rpm": 60
  },
  "desktop": {"max_fps": 10, "allow_input": true, "monitors": 1},
  "security": {"allow_shell": true, "allow_file_write": true, "allow_delete": false}
}
```

字段说明：

- `when.only_if_idle_s`：仅在用户无输入持续 N 秒后触发，用来避免录下无关操作。
- `when.on_foreground_change`：前台窗口切换后延迟 `settle_s` 再触发。
- `slideshow.on_slide_change`：用 dHash 感知哈希比较连续帧，变化超过 `threshold` 判定换页，这是“合适时机”的核心判定。
- `upload.layout`：决定仓库内目录结构，按类型与年月分桶，避免单目录文件过多。
- `security.allow_delete`：服务端与 agent 同时为 true 才允许删除，默认关闭。

## 5. 去重与断点

agent 维护本地 SQLite（`state.db`），记录已上传文件的 `sha256`、远端路径、状态。重复内容直接跳过；上传中断的记录保留 `pending` 状态，下次启动优先重试。同一份文件在多台实例上扫描到时，后到的记录会以 `existing` 标记跳过，减少重复提交。

## 6. 服务端存储

SQLite 三张表：`devices`（设备与最后心跳）、`rules`（按设备或分组保存的规则版本）、`audit`（命令下发与结果，用于追溯）。控制台是单页应用，通过 `/api/*` 与 `/ws/console` 通信。
