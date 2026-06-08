# Contributing to edr-wd

## 开发规范

### 隐私信息处理

**严禁**在日志、diff、命令输出、commit message 中包含以下敏感字段：

```
host / ip / user / password / password_env / key_path /
direct_url / tunnel_url / target_root / sshpass -p
```

**禁止在 commit message、PR 描述、测试总结里写真实 IP、用户名、密码、target_root。**

**禁止直接执行以下操作**（会暴露敏感信息）：

```bash
# 不要
cat config/targets.json
cat config/targets.local.json
git diff config/targets.local.json
sshpass -p '<YOUR_PASSWORD>' ssh <TARGET_USER>@<TARGET_IP> ...
```

**调试时使用脱敏输出**：

**唯一允许的查看 local config 方式** — 通过 `scripts/redact_config.py`：

```bash
python scripts/redact_config.py config/targets.local.json
python scripts/redact_config.py config/targets.local.json --strict
# --strict: exits non-zero if any non-secret key contains a
# password-like value (length 6..64, no whitespace, mixed alnum).
```

脚本规则：
- `password / password_env / token / secret / api_key / key_path` 一律 `<REDACTED>`
- `host / user / root / target_root / python_path / *_url` 用 stable SHA1 prefix (`h_xxxxxxxx`)
- 其它结构保留，方便 review schema 正确性

**严禁**使用 `cat / less / head / jq` 直接打开 `config/targets.local.json`、`config/targets.json`、`config/test_machines.json`、`target/config.json`。**严禁**在终端 / commit message / PR 描述里复述这些文件的内容。

CI / pre-commit 应在每次提交前跑：

```bash
git grep -nE "170\\.170\\.11\\.26|whl@123|C:\\\\Users\\\\admin|sshpass -p '[A-Za-z0-9]+'" -- .
git ls-files config/targets.local.json config/test_machines.json target/config.json
```

如果有任何输出，禁止 push。

**SSH 密码认证**：当前内网 target 优先使用 `config/targets.local.json`
里的 `ssh.auth.type=password` + `ssh.auth.password`，由 Paramiko 登录，不使用
密钥，也不要使用 `sshpass -p`。`password_env` / key auth 仅作为兼容路径保留。
TODO: 后续如果脱离可信内网，再做凭据存储加固。

### 本地配置

`config/targets.local.json` 和 `config/targets.json` 不提交到仓库（已列入 `.gitignore`）。

首次配置时从 `config/targets.example.json`（如果存在）复制，或参考 `SKILL.md` 中的配置说明。

### Debugging and Test Guidelines

- 查看配置只使用脱敏输出，不直接 cat/json dump 原文件
- 禁止在日志里展示 ssh 命令含真实凭据
- local config 不提交；gitignore 确保不再 track
- 测试命令格式：

```bash
EDR_WD_TARGET=<target-name> python test_case/run_tests.py -v
```

- 本次调试修复记录不写入 CONTRIBUTING.md，应记录在 issue / debug note / test report
