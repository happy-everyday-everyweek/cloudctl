# 局域网互联协议（cloudctl-mesh v1）

## 1. 定位

同一局域网里的多台实例彼此直接通信，不经过中心服务端，也不依赖 GitHub。设备端是纯被控端：没有窗口、没有托盘、没有提示，不引入任何 UI 库，只用标准库的 socket、http.server 与 urllib，内存与 CPU 开销保持在低位。

互联与两条外联通道相互独立。局域网内能通、公网全断的情况下，你仍然可以在任意一台实例上用命令行把命令发给邻座实例。

## 2. 发现

发现走 UDP，端口 mesh_discovery_port，默认 8791。节点每 mesh_announce_s 秒（默认 20）广播一次自己的身份，收发同时使用组播地址 239.255.42.99 与受限广播地址 255.255.255.255，这样在禁用组播的交换机上也能靠广播互相看见。

身份包字段为协议名、版本、mesh 组名、device_id、设备名、业务组、互联端口、本地控制台地址、程序版本、caps、时间戳。收到同组身份包就把对方写进邻居表，mesh_ttl_s 秒（默认 90）没再收到就删掉。不同 mesh 组之间互不扰动。

## 3. 通信

业务通信走局域网 HTTP，端口 mesh_port，默认 8792。每个请求带 X-Mesh-Token 头，与 mesh_token 比对，慢比较防时序猜测；mesh_token 留空时取本地控制台令牌，因此不会出现“无鉴权”状态。请求体里的 mesh 字段与本地组名不符时直接 403。

提供的端点有：GET /mesh/hello 返回身份与邻居表，GET /mesh/peers 返回邻居表与运行统计，GET /mesh/pull 按 offset 与 length 分段读取文件，POST /mesh/cmd 执行一条命令，POST /mesh/relay 转发一条命令，POST /mesh/blob 接收一个文件。

信封字段统一为 v、proto、kind、mesh、from、from_name、to、id、ts、hops。id 用于去重，同一 id 的请求只执行一次，避免两条外联通道加上互联通道同时送达时重复动作。

## 4. 安全边界

对端命令进入与本机控制台完全相同的 Router 权限门禁，shell、写文件、删除等开关一项都不会因为“来自局域网”而被放宽。mesh_accept_cmd 为 false 时彻底拒绝执行任何对端命令，只保留发现与文件接收。

传输是明文的局域网 HTTP，适合可信内网。若局域网不可信，把 mesh_token 设为一个长随机串，并把互联端口用防火墙限制在本网段。

## 5. 中继

目标不在直连邻居表里时，节点会把请求交给已知邻居代传，hops 每过一跳加一，超过 mesh_max_hops（默认 2）就丢弃，防止环路与广播风暴。mesh_relay 为 false 的节点不代传。中继只做转发，不查看命令内容，也不缓存。

## 6. 文件互传

小文件用 POST /mesh/blob 一次发送，单次上限 32MB，带 sha256 校验，落盘到工作目录下的 peers/<对端 device_id>/ 并保留原文件名。大文件用 GET /mesh/pull 分段拉取，每次默认 256KB，适合几 GB 的素材在局域网内搬运而不碰 GitHub 的体积限制。

## 7. 配置项

mesh_enabled 开关互联；mesh_group 指定组名，留空则用 group；mesh_bind 默认 0.0.0.0；mesh_port 与 mesh_discovery_port 分别是业务端口与发现端口；mesh_token 是共享令牌；mesh_announce_s 与 mesh_ttl_s 控制宣告频率与邻居存活期；mesh_accept_cmd、mesh_relay、mesh_max_hops 控制接不接命令、代不代传、最多几跳。

## 8. 命令行

agent --mesh-peers 启动监听若干秒后打印邻居表，默认等 8 秒，可用 --mesh-wait 调整。agent --mesh-send <device_id> <op> [JSON参数] 向指定邻居发一条命令并打印结果，例如 agent --mesh-send a1b2c3d4e5f60718 sys.info。这两个子命令不会常驻，探完即退。

## 9. 与两条外联通道的关系

WebSocket 通道解决跨公网长连接，GitHub 通道解决无公网时的命令与结果交换，互联解决同一局域网内的横向通信与大批量文件搬运。三者共享同一套命令表与权限门禁，任何一条断了都不影响其余两条。
