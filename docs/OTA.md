# OTA 自升级

## 1. 之前有没有

之前没有。CI 已能在打 v* 标签时把 exe 发到 Release，但设备侧不会自己去拿。现在补上了从 Release 到就地替换的整条链路。

## 2. 怎么工作

agent 的 OTA 线程每 check_min 分钟（默认 60）向 Releases API 要一次最新版本（走镜像池，国内网络用得上），比版本号；发现更新就下载名称里带 asset_pattern（默认 cloudctl-agent）的资产，Windows 下必须是 .exe；下载时校验 sha256（Release 资产自带的 digest 字段，或 Release 说明里写的 sha256 行，两者都找不到就跳过校验）；校验通过后生成一个等待脚本：等当前进程退出、把旧 exe 备份成 .bak、把新 exe 换到位、按需重新拉起，最后删掉自己。

版本号比较是取数字段逐位比，所以 v1.10.0 会正确地被认为比 v1.9.0 新，不会出现字符串比较那种“永远不相等”的毛病。只支持数字化版本号，命名请保持 v1.5.0 这种形式。

## 3. 开关与参数

OTA 默认是关的，规则里加一段 update 即可打开：

```json
{
  "update": {
    "enabled": true,
    "repo": "happy-everyday-everyweek/cloudctl",
    "channel": "latest",
    "tag": "",
    "asset_pattern": "cloudctl-agent",
    "check_min": 60,
    "verify_sha": true,
    "auto_apply": false,
    "keep_backup": true,
    "restart_delay_s": 5
  }
}
```

channel 为 latest 时取最新发布；改成 tag 并在 tag 里写上具体标签，就能把一群实例钉到指定版本（回滚同理，指个旧 tag 即可）。auto_apply 为 false 时只下载不动手，需要你或控制台下 apply。

## 4. 三种触发方式

设备本地：agent --update-check 只看不动；agent --update-apply 直接升（可加 --update-tag v1.5.0 指定版本）。

远程命令：发一条 op 为 agent.update 的命令，args 里 action 取 status、check、download、apply 之一；apply 可以带 path 指定已下好的文件，也可以带 restart 控制是否重启。

规则广播：把 update.enabled 与 update.tag make 进规则下发，所有收到规则的实例下次检查时自动跟随。

## 5. 发布一个新版本

改好代码后先在 agent/cloudctl_agent/main.py 里把 VERSION 提升（例如 1.5.0 改 1.6.0），提交到 main，然后打标签并推送：git tag v1.6.0 加 git push origin v1.6.0。CI 会自动打包单文件 exe、生成 cloudctl-agent.exe.sha256、发 Release。普通用户在 Release 里手动下载，已开 OTA 的实例会在下一轮检查时自己拿。

## 6. 注意

只有 exe 形态（PyInstaller 单文件）支持自我替换；以源码方式运行时 apply 会返回提示并把新版本留在工作目录 updates/ 下。替换失败原 exe 会保留为 .bak，拷回来就能回滚。升级属于高风险动作，建议先把 update.enabled 单机验证一遍再广播。
