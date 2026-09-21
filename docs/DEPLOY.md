# 部署步骤

## 1. 准备工作

- 一台有公网地址的机器（VPS），用来同时跑 frps 和云控服务端
- 每个受控实例装 Windows 10/11，且你有管理员权限
- 一个用于知识库的 GitHub 仓库，建议私有，不要用公开仓库放个人素材
- 一个 fine-grained PAT，权限：知识库仓库的 Contents 读写

## 2. 启动服务端

```bash
git clone https://github.com/happy-everyday-everyweek/cloudctl.git
cd cloudctl/server
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
chmod 600 ~/.env && cat > ~/.env <<EOF
export CLOUDCTL_AGENT_TOKEN='换成随机串A'
export CLOUDCTL_CONSOLE_TOKEN='换成随机串B'
export CLOUDCTL_DB=/opt/cloudctl/cloudctl.db
EOF
. ~/.env
python -m app.main --host 127.0.0.1 --port 8787
```

两个令牌务必不同。控制台令牌就是浏览器登录口令，agent 令牌给客户端用。
服务端默认只监 127.0.0.1，外部访问统一走 frp，不要在公网直接暴露端口。

## 3. 打通内网穿透

在 VPS 上跑 frps：`./frps -c frps.toml`。在服务端所在机器上跑 frpc（本仓库 deploy/frpc.toml），把本地 8787 映射到 VPS 的 8787。

然后把 frps.toml 的 7000、8787、7500 在 VPS 安全组放行。若用域名和证书，走 frpc 里的 https vhost，并把 customDomains 换成你的域名。

验证：浏览器打开 `http://VPS_IP:8787/`，能看到登录页即通。

## 4. 得到 agent 的 exe

推一个 tag 就会自动构建并发布：

```bash
git tag v1.0.0 && git push origin v1.0.0
```

产物在 Actions 的 artifact 或 Release 里，文件名 cloudctl-agent.exe。也可以本地装 pyinstaller 后跑 workflow 里同样的命令。

## 5. 实例端安装

把 exe 和 config.json 放到同一个目录，config.json 至少填四项：server_url、server_token、device_id、device_name。

```powershell
.\cloudctl-agent.exe --selftest     # 看依赖是否齐
.\cloudctl-agent.exe --status       # 看设备号与自启状态
.\cloudctl-agent.exe --install      # 装开机自启（管理员跑，会建 SYSTEM 计划任务）
.\cloudctl-agent.exe --run          # 前台跑一次，确认能连上
```

自启装好后重启机器，控制台左侧应该出现这台实例。卸载用 `--uninstall`。

## 6. 下发规则

控制台里选实例，进“规则”页，按需打开字段后保存。几个要点：

- capture.photo.enabled 与 capture.video.enabled 默认关闭，需要才开
- scan.roots 填你要归档的目录，不要填整个 C:/，扫描会非常慢
- upload.enabled 为 true 且 upload.repo 填对后，才会真的往仓库推
- security.allow_delete 默认 false，控制台删文件会被拒。确实需要再开

保存后版本号自增，在线实例立刻生效，离线实例上线时自动拉取。

服务端不可达时，agent 会退到备用通道：每 gh_poll_s 秒拉一次 ctl/rules.json，命令放在 ctl/cmd/设备号.json，结果写回 ctl/outbox/设备号.jsonl。

## 7. 知识库仓库的准备

先在知识库仓库里建好目录骨架（可选）：

```
kb/doc/2026/09/
kb/image/2026/09/
kb/audio/2026/09/
kb/video/2026/09/
```

上传按 `{prefix}/{type}/{yyyy}/{mm}/{name}` 生成路径，文件名会带上 sha256 前八位，避免同名覆盖。

关键限制，动手前先算好：单文件超过 100 MB 无法通过 API 提交，超过 50 MB 告警；建议单文件压在 40 MB 以下。视频类素材很容易超标，所以规则里的 max_single_mb 默认 90，超过的直接标记 too_large 跳过，不会反复重试。真的要大文件，走 Release 附件（单个上限 2 GB）或自建对象存储。

## 8. 常见问题

控制台一直显示离线：先看 agent.log，确认 server_url 协议对不对（wss 对应 https，ws 对应 http），以及 server_token 是否与服务端一致。

远程桌面黑屏：多半是会话问题。计划任务跑在 SYSTEM 下，登录会话里的画面才抓得到；未登录时 gdigrab 拿到的是锁屏界面。

录像文件巨大：调高 crf 或降低 fps、缩短 segment_s。video.max_mb_per_day 到顶后会自动停止录制直到第二天。

上传 403：PAT 缺 Contents 写权限，或者 upload.repo 写的仓库不在这个 PAT 的授权范围内。

上传 429：触发 GitHub 限流，本轮会提前结束，下轮自动继续。
