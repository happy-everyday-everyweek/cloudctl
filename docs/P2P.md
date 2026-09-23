# 设备间直连（P2P）

## 它解决什么

前两条通道（WebSocket、GitHub）都要经过“外面”：WS 要过隧道或域名，GitHub 在国内可能几天不通。
直连层让设备之间自己说话：能打通就打点对点，打不通才让“两边都通”的邻居转一手，
中心（Hub、包仓库）只负责告知彼此在哪。

## 怎么工作

UDP 打洞：双方各自向对方候选端点发 PUNCH，收到 PONG 即置为 direct 并保活（默认 20 秒一次）。
信封鉴权：每包带 nonce 与签名（token+nonce+from+to 的 sha256 前 16 位），错签丢弃。
分片传输：逻辑分片 256KB，每个 UDP 包有效负载 1000 字节，避开 64KB 上限与 MTU 碎片；
接收端按偏移写盘，乱序也不怕；FIN 里带 size 与 sha256，缺片就反向请求补发（ACK）。
转发兜底：直连超时就交给网格里对端可达的邻居（现有 mesh 中继），不引入任何付费服务。

## 配置

`p2p_enabled`、`p2p_bind`（默认 0.0.0.0）、`p2p_port`（默认 8793）、`p2p_token`（默认复用 mesh 令牌）、
`p2p_public_host` / `p2p_public_port`（手工声明公网端点）、`p2p_keepalive_s`、`p2p_inbox_max_mb`（收取目录上限）。
规则里可下发 `p2p` 段覆盖开关与保活间隔。

## 怎么验

`agent --p2p-status` 看候选端点与已建路径；`agent --p2p-ping ID=IP:PORT` 主动打洞；
`agent --p2p-push ID=IP:PORT FILE` 推一个文件并校验 sha256。上报载荷里的 `p2p` 段会带直连数、
中继次数与收发字节，远端能看出哪两台设备其实直连着。

## 实测

本机两个实例（回环，端口 8793/8794）互打：双方 1.5 秒内变 direct，1KB 消息送达，
3MB 文件分 13 片传完、sha256 校验通过、零丢弃。丢包场景靠 ACK 补发兜底。
