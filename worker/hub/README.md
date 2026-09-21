# cloudctl Hub（Cloudflare Workers 版核心服务端）

## 1. 为什么有这个

原来 server/ 是 Python FastAPI，得有一台常驻机器。Workers 版把同一套协议搬到了 Cloudflare 免费额度上：没有服务器、没有账单、不用维护，agent 与控制台照样能连。

## 2. 免费额度够不够

Workers 免费版：每天 10 万次请求，单请求 CPU 时间 10 毫秒。WebSocket 升级计入请求数，升级之后的长连接不再按请求计费。

Durable Objects 免费档：包含每日请求数与持续时长额度，用 SQLite 后端存储；本 Hub 用 hibernation 接受 WebSocket，空闲连接不占用持续时长，所以几十台设备挂着也基本碰不到配额。

审计与设备快照存在 Durable Objects 的 storage 里，每条几百字节，免费档的存储额度（数 GB 级）完全不可能满。以上数字以 Cloudflare 官方与仪表盘显示为准，规则偶尔会变。

## 3. 不适合用它做的事

文件存储：不要拿它当素材仓库，素材依旧走 GitHub 或局域网直传。图片/视频不落地到 CF。

重计算：CPU 上限 10 毫秒是硬约束，所以 Hub 只做转发与登记，不做转码、不做哈希、不做代理下载。

替代本地控制台：设备自带的 8788 控制台与局域网互联不受影响，Hub 只是把“远程可达”这件事变成零成本。

## 4. 部署

本地已装 wrangler（4.133.0）。在仓库根目录执行：

```bash
cd worker/hub
wrangler deploy
wrangler secret put HUB_TOKEN      # 贴一个长随机串，agent 与控制台都用它
```

想要固定域名（例如 hub.lumenbay.online）：到 Cloudflare 仪表盘该 Worker 的 Settings → Domains & Routes 里添加，或取消 wrangler.toml 里 routes 那两行的注释再部署一次。lumenbay.online 已经在你的 Cloudflare 里激活，不需要再改 DNS。

想走 CI 部署：在仓库 Secrets 里加 CLOUDFLARE_API_TOKEN 与 CLOUDFLARE_ACCOUNT_ID，然后手动触发 deploy-hub 工作流（或推 worker/ 目录下的改动）。

## 5. 验证

```bash
curl https://hub.lumenbay.online/health
curl -H "x-token: <HUB_TOKEN>" https://hub.lumenbay.online/api/devices
```

设备侧把 config.json 的 server_url 改成 https://hub.lumenbay.online，agent 会自己拨 wss://hub.lumenbay.online/ws/agent。

## 6. 接口

WebSocket：/ws/agent?device_id=..&token=..（agent 拨入），/ws/console?token=..&device=..（控制台拨入，带 device 就直接收该设备的帧与事件）。

REST：GET /api/devices 设备清单（在线与离线都列出），POST /api/devices/{id}/cmd 下发命令并等结果（body 为 op、args、timeout），POST /api/devices/{id}/rules 下发规则，GET /api/audit 看最近审计，GET /health 看在线数。

鉴权：全部请求带 HUB_TOKEN（查询串 token= 或请求头 x-token）。不设 HUB_TOKEN 时不做鉴权，仅建议本地调试。

## 7. 与 Python 版 server/ 的关系

协议兼容，agent 代码零修改，切 server_url 即可换后端。差别在于：Python 版能挂静态控制台页面、能做更重的处理；Workers 版胜在免费、零维护、自带全球边缘。两者可以同时存在，按分组分别指向。

## 8. 其他免费选项

Cloudflare 这边：Workers + Durable Objects 免费档、D1 免费档、KV 免费档、Tunnel 免费（把你自己机器的 FastAPI 服务暴露出去，代码零改动）。Containers 是付费的，没免费额度。

CF 之外：Oracle Cloud Always Free 的 ARM 实例（四核二十四 G，永久免费）跑 Python 版最省事；Google Cloud Run、Render、Koyeb、Hugging Face Spaces 都有免费档，但会有休眠或额度限制，WebSocket 长连接容易被掐。
