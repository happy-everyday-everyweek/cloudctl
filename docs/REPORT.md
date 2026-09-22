# 上报：存活心跳与关机上报

## 1. 为什么有这条能力

Hub 需要能回答两个问题：哪些设备现在活着，以及某台设备是什么时候不见的。
所以上报分三个时机：启动、按间隔定时（证明设备活着）、退出或关机（留下最后一条记录）。

上报不是新协议，就是一条普通事件：`event = agent.report`，带着设备、版本、运行时长与通道状态，
走现有的 WebSocket 与 GitHub 两条平行通道。任何一条通就能送达。

## 2. 三个时机

启动上报在 `agent.online` 之后立刻发一条，`kind=start`。定时上报由独立线程负责，
默认每 300 秒一条，`kind=alive`。退出或关机时同步发一条，`kind` 分别可能是
`shutdown`（正常退出）、`shutdown_signal`（收到系统关机/注销事件）、`exit`（主循环结束退栈）、
`manual`（人工或云端要求立即上报）。

关机上报是同步的：不复用已有的事件循环，而是新开一条短连接把队列里的东西一次性发完，
发完即走，最长等 `report_shutdown_wait_s`（默认 8 秒）。两条通道都发不出去就写到
`spool.ws.jsonl` / `spool.gh.jsonl`，下次连上补发。

## 3. 配置与规则

`config.json` 里的六个键：`report_enabled`（总开关，默认关不掉就不发）、`report_interval_s`
（间隔，最小 30 秒）、`report_on_start`、`report_on_shutdown`、`report_shutdown_wait_s`、
`report_include_metrics`（是否带 CPU / 内存 / 磁盘占用）。

规则里的 `report` 段同名同义，可由云端下发整段覆盖。与权限类开关不同的是，上报是功能开关，
两边任一关闭即不上报；但规则侧不能把已经关掉的上报重新打开（配置总开关优先）。

## 4. 怎么触发与怎么查

命令行 `agent --report` 立即发一次，适合验证通道是否通。
本地控制台 `GET/POST /api/dev/report` 触发一次，返回值里带 `ws` / `gh` 哪个通了。
云端或邻居下发命令 `report.now` 触发一次，`report.status` 查当前上报配置与运行时长。

## 5. 已知边界

关机钩子靠 Windows 控制台事件（关闭窗口、注销、关机）。以计划任务方式运行且不带控制台时，
系统不一定送事件，这种情况下由信号与退出退栈补发；日志里会写明钩子是否挂上。
Linux 侧没有等价钩子，靠 SIGTERM 走 `Agent.stop()` 里的同一条同步上报路径。

上报载荷里的 CPU 占用需要 psutil，没有 psutil 时该项为空，不影响其它字段。
