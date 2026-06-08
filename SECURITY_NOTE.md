# Security Note

## History Sanitization (June 2026)

Sensitive configuration files containing real credentials were previously present in git history and have since been removed:

- `config/targets.json`
- `config/targets.local.json`
- `config/test_machines.json`
- `target/config.json`
- `target/config/test_machines.json`

All remote branches (`main`, `hermes`, `mcp_auto_v1.0`, `mcp_manual`) were force-rewritten using `git-filter-repo` to remove these files from full history.

## Required Actions for All Collaborators

1. **Re-clone** or `git reset --hard origin/hermes` to get a clean history.
2. **Recreate** local target configs from `config/targets.example.json`.
3. **Rotate credentials** that were exposed before cleanup — treat them as potentially compromised.

## Usage Rules

- Never `cat` or `git diff` real config files in issues, PRs, or chat.
- Never use `sshpass -p` with real passwords.
- Current intranet targets may store credentials directly in `config/targets.local.json` (already in `.gitignore`); `password_env` remains a compatibility option.
- TODO: move credentials out of local JSON if the workflow leaves the trusted intranet setup.
- All redacted output only — never paste real IPs, usernames, passwords, or full paths.
