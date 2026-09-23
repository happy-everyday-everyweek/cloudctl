# 构建与配置内嵌

## 配置的四个来源（优先级从高到低）

环境变量（`CLOUDCTL_<键名大写>`）> exe 旁的 `config.json` > 内嵌 `build_config.json` > 内置默认值。
顺序说明：内嵌只当默认值用，任何人拿同一个 exe，在本机放一份 `config.json` 就能盖掉内嵌内容。
源码方式运行时，外部配置文件是 `agent/config.json`；单文件 exe 运行时是 exe 同目录的 `config.json`
（打包后 `__file__` 指向临时解包目录，不能拿它推路径）。

## 做“装上去就能连”的发行包

先在交互式向导里填好凭证，并加 `--embed` 把同一份内容写给构建用：

```
python scripts/setup_config.py --embed
powershell -ExecutionPolicy Bypass -File scripts/build_windows.ps1
```

`build_windows.ps1` 发现 `agent/cloudctl_agent/build_config.json` 时会自动加 `--add-data` 把它打进 exe。
也可以走 CI：在仓库加一个 secret `CLOUDCTL_BUILD_CONFIG`（内容是 JSON），
`.github/workflows/build-agent.yml` 会在打包前写盘并内嵌，没有这个 secret 就跳过。

## 注意

内嵌意味着谁拿到这个 exe，谁就能读到里面的令牌。公开 Release 里放带令牌的 exe，
等于把控制权和仓库写权限一起公开。更稳的分法：内嵌只放 `server_url`、`gh_rules_repo` 这类公开信息，
令牌留在每台机器自己的 `config.json` 里（OTA 升级只换 exe，`config.json` 不会被覆盖）。

`build_config.json` 与 `config.json` 都已在 `.gitignore` 里，不要提交。
