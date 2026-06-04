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
sshpass -p '真实密码' ssh user@host ...
```

**调试时使用脱敏输出**：

```python
import json

SENSITIVE_KEYS = {
    "host", "ip", "user", "password", "password_env",
    "key_path", "direct_url", "tunnel_url", "target_root"
}

def redact(obj):
    if isinstance(obj, dict):
        return {k: ("<REDACTED>" if k in SENSITIVE_KEYS else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj

print(json.dumps(redact(data), indent=2, ensure_ascii=False))
```

**SSH 密码认证**：使用 `sshpass -e`（从 `SSHPASS` 环境变量读取），不要在命令中明文写密码。

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
