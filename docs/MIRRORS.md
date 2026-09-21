# 镜像池（GitHub 不可达时的容错基石）

## 1. 清单从哪里来

内置清单取自你自己的项目 happy-everyday-everyweek/gitlink（app/src/main/java/com/ghlink/app/core/Mirror.kt），共 33 条镜像加 1 条直连。前缀语义与 GitLink 一致：最终链接 = prefix + 原始 URL。清单里按当时测速结果排了序，cxkpro、gh-proxy、ghfast、ghproxy.net 这些常用域名都在。

## 2. 运行时候选怎么排

每次要取一个文件，都从镜像池里取前 gh_mirror_top 个候选（默认 4），按健康分从高到低依次尝试，首个 200 就 stops。健康分算法是成功次数乘二、减失败次数、再减连续失败次数的三倍，并按时延微调，因此偶发抽风的镜像会自然沉底，好用的会浮上来。

用 github.com 前缀去拼 raw.githubusercontent.com 或 api.github.com 地址是无效组合，这类候选会自动跳过。

## 3. 健康记忆存哪里

存在工作目录下的 mirrors.json，结构为 {"list":[镜像定义], "health":{镜像 id: 成功、失败、平均时延、连续失败次数、时间戳}}。它可以通过环境变量 CLOUDCTL_HOME 或 config.json 里的 home 改位置。删除这个文件就回到出厂排序。

## 4. 主动探测

gh_mirror_probe 默认开启，每 gh_mirror_probe_s 秒（默认 1800）探测一次前 12 个镜像，每个给 8 秒超时，探活目标就是你的 ctl/rules.json，测一遍恢复与延迟。探测在后台线程中跑，不阻塞轮询。

## 5. 导入你自己的清单

GitLink 导出的是 {"list":[{"id":..,"name":..,"prefix":..,"note":..,"builtin":..}]}，这个格式与镜像池完全兼容。三种导入方式：把文件内容写进工作目录的 mirrors.json 的 list 字段；把路径填到 config.json 的 gh_mirror_pool，启动时会自动合并到已有清单；或在控制台里让助手执行导入。

## 6. 配置项

gh_mirror_pool 是自备清单文件路径；gh_mirror_top 控制单次请求最多试多少个候选，网络很差时可以调到 8；gh_mirror_probe 开关主动探测；gh_mirror_probe_s 是探测间隔；gh_proxy 是指定优先镜像，填了就排在最前面。

## 7. 连不上的时候会发生什么

轮询先试候选镜像，全失败则指数退避（2 的幂，上限 gh_backoff_max_s，默认 900 秒，带随机抖动），第 1、5、10、20 次会写明白的警告日志；期间规则用上次落盘的 rules.remote.json；上报先写 spool.gh.jsonl，恢复后自动批量补写；设备本地控制台与局域网互联完全不受影响。后台探测一旦发现哪条恢复，候选顺序会立即好转。

## 8. 与 GitLink 的关系

两边共用同一套镜像认知：GitLink 管 GitHub 资产下载，cloudctl 管规则与结果的拉推。在 GitLink 里测出好用的域名，直接导出清单丢进 mirrors.json，cloudctl 下次跑探测就会用上。
