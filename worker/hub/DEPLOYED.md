# 部署记录（2026-09-21）

## 已完成

Worker 名：cloudctl-hub，已成功部署到 Cloudflare Workers 免费档。

脚本状态（CF API 实查）：handlers 为 fetch，named_handlers 含 Hub 类，migration_tag 为 v1，说明 Durable Objects（SQLite 后端）在免费档已可用。

默认地址：https://cloudctl-hub.lumenbay.workers.dev （国内网络访问不到，workers.dev 被墙）。

密钥：HUB_TOKEN 已通过 wrangler secret put 写入（当前值 hub_f8516558655de12e59eba222bfc3c50b82065aba，如要更换重跑 wrangler secret put 即可）。

DNS：已在 zone 31abbdbc67231502b0127d6be0768c95 下创建 AAAA 记录 hub.lumenbay.online → 100::，proxied 已开（记录 id 6fe331e47ae2cbf75c970a5da2292261）。

## 卡住的一步

把 Worker 挂到 hub.lumenbay.online 需要“绑定自定义域名”，这一步被我手上的两个令牌都拒了（账户令牌调 /accounts/{id}/workers/domains 报错，域名令牌调 /zones/{id}/workers/routes 也报错），判断是令牌里缺 Workers Scripts 与 Workers Routes 的编辑权限。

在仪表盘点一下就行：Workers 与 Pages → cloudctl-hub → Settings → Domains & Routes → Add → Custom Domain → 填 hub.lumenbay.online（DNS 记录已经存在，不会再让你建）。

想要全自动化，就在 API 令牌里补勾 Account 下的 Workers Scripts:Edit 与 Workers Routes:Edit，之后重跑一次 wrangler deploy 即可自动绑定。

## 部署后怎么验证

```bash
curl https://hub.lumenbay.online/health
curl -H "x-token: <HUB_TOKEN>" https://hub.lumenbay.online/api/devices
```

## 设备侧配置

config.json 改两行即可，agent 代码不用动：

```json
{ "server_url": "https://hub.lumenbay.online", "server_token": "<HUB_TOKEN>" }
```

## 免费额度现状

Workers 免费档：10 万请求/天、CPU 10ms/请求；Durable Objects 免费档含每日请求与时长额度；本 Hub 用 hibernation 接 WebSocket，空闲连接不占时长。当前账号下已跑两个 Worker（cloudctl-hub 与 lumenbay-mail-relay），均在免费档内。
