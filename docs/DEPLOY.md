# 部署与联调

## 1. 装什么在哪台机器上

每台要管理的 Windows 实例上放一份 cloudctl-agent.exe 和一份 config.json，config.json 放 exe 同目录，或放到 %ProgramData%\\cloudctl\\config.json。运行 agent --console 只启本地控制台，适合先验证；运行 agent --run 则同时拉起本地控制台与两条外联通道。

中心服务端是可选件，跑在任何一台你能访问的 Linux 主机上，uvicorn server.app.main:app --host 127.0.0.1 --port 8787 即可。不部署它，你依然能用每台设备自己的控制台管理它。

## 2. 设备本地控制台

启动后浏览器打开 http://127.0.0.1:8788，输入令牌。令牌优先级：config.json 里的 devsrv_token，没填则用 server_token，都没有就用 device_id（在 agent --status 里能看到）。默认只绑 127.0.0.1，意味着只有本机能访问，这是最安全的默认值。

要局域网内其它机器访问，把 devsrv_bind 改成 0.0.0.0，同时必须设一个 devsrv_token，并在 Windows 防火墙上放行该端口。

## 3. 内网穿透（Cloudflare Tunnel）

设备自己有端口了，穿透的作用变为把这个端口安全地露到外网。你已有的资产：cloudflared 2026.9.1 已装，固定隧道 phone-tunnel（id dc1ed1ca-8793-461c-9634-e75139c9757d）已建且 healthy，域名 lumenbay.online 已在 Cloudflare 激活（zone id 31abbdbc67231502b0127d6be0768c95），凭证在 /root/cloudflare/creds.env，隧道密钥在 /root/cloudflare/tunnel.env。

推荐做法是每台设备自己跑一个 cloudflared，用 token 模式接入，这样隧道进程和设备在同一台机器上，127.0.0.1 直达：cloudflared service install <token>，然后 cloudflared service start。

如果想把多台设备挂在同一条隧道下，就在隧道所在机器上写 config.yml，用 ingress 把不同 hostname 映射到不同地址：

```yaml
tunnel: dc1ed1ca-8793-461c-9634-e75139c9757d
credentials-file: /root/.cloudflared/dc1ed1ca-8793-461c-9634-e75139c9757d.json
ingress:
  - hostname: ctl.lumenbay.online
    service: http://127.0.0.1:8787
  - hostname: dev-a.lumenbay.online
    service: http://127.0.0.1:8788
  - service: http_status:404
```

DNS 记录用 cloudflared tunnel route dns phone-tunnel dev-a.lumenbay.online 创建，或在仪表盘手加 CNAME 指向 <tunnel-id>.cfargotunnel.com。

关键点：只有同机器上的 127.0.0.1 才能被本机 cloudflared 访问。远程机器的 8788 不能由你服务器的 cloudflared 直接转发，除非它走内网地址（如 http://192.168.1.20:8788）。

暴露到公网前必做两件事：设置一个足够长的 devsrv_token；在 Cloudflare 侧给这个 hostname 加 Access 策略（仅允许你的邮箱登录）。否则任何人拿到地址就能试令牌。

WebSocket 通道走的是同一个道理：把 server_url 写成 https://ctl.lumenbay.online，agent 会自己拨 wss://ctl.lumenbay.online/ws/agent。Tunnel 原生支持 WebSocket，不需要额外配置。

## 4. GitHub 通道

在 config.json 里填 gh_rules_repo（如 happy-everyday-everyweek/cloudctl）、gh_token（细粒度 PAT，只给这个仓库的 Contents 读写）。agent 会每 gh_poll_s 秒拉一次 ctl/rules.json 与 ctl/cmd/<device_id>.json，并把结果批量追加到 ctl/outbox/<device_id>.jsonl。

下发一条命令的做法：在 ctl/cmd/<device_id>.json 里写 {"commands":[{"type":"cmd","id":"c1","op":"sys.info","args":{}}]}，等一个轮询周期后看 outbox 文件。两条通道同时配齐时，同一 id 的命令只会被执行一次。

## 5. 素材归档

upload_repo 指向归档目标仓库，upload_prefix 是仓库内的子目录前缀。upload.enabled 默认为 false，需要在规则里手动打开。csv、pdf、docx、pptx、图片、音频归小文件路径，视频容易撞上 GitHub 的单文件 100MB 硬限制，超限会被标 too_large 跳过。

## 6. 下发规则

三种下发途径效果一致：本地控制台的规则区（POST /api/dev/rules）、中心服务端的 /api/devices/{id}/rules、或直接改仓库里的 ctl/rules.json。规则只收紧权限：capture.photo、capture.video、upload.enabled 默认全关，security.allow_delete 默认 false。

## 7. 验收清单

取一台 Windows 实例，先跑 agent --selftest 看依赖是否齐全（会附带报告 devstatic 是否存在），再跑 agent --status 确认 device_id、自启状态、控制台端口与两条通道的配置位。然后跑 agent --console，浏览器登录 http://127.0.0.1:8788，依次点 agent.info、sys.info、拉取日志、扫描素材。确认本地链路通了，再开 agent --run 验证外联。

## 8. 常见问题

打开页面显示 device console missing：exe 没带上 devstatic 静态资源，用最新 CI 产物（workflow 已加 --add-data）重新构建。

接口返回 401 需要令牌：请求头没带 X-Token，Cookie 也没登录。用登录接口拿一次 Cookie，或在控制台里重新输入令牌。

远程桌面黑屏：先确认目标机器已登录到桌面会话。agent 跑在 schtasks 的 SYSTEM 上下文里时可能只能截到锁屏界面，把自启改成“用户登录时启动”即可。

8788 端口被占：改 devsrv_port，同时改隧道 ingress 里对应的端口。

WebSocket 一直连不上：看 agent.log 里的“WebSocket 通道断开”原因；Tunnel 的 hostname 未解析、或 server 端未监听 8787 都会导致这个现象。此时 GitHub 通道与本地控制台不应受影响，这正是两条通道平行的意义。
