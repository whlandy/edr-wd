# PyInstaller Packaging

Put all PyInstaller-specific files for `mcp.exe` in this directory.

Planned files:

```text
packaging/pyinstaller/
  README.md
  mcp.spec
  build.ps1
  build.sh
  smoke.ps1
  smoke.sh
```

Do not place PyInstaller specs or build scripts at the repository root.

The desktop prototype's root `mcp-edr-wd.spec` should not be copied as-is. When
implementation starts, migrate it into `mcp.spec` and fix these issues first:

- output executable name must be `mcp.exe`.
- use portable paths instead of Windows-only source path literals.
- include only required runtime resources and FastMCP metadata.
- exclude tests, local config, screenshots, logs, caches, and generated reports.
- import the current root-level source of truth instead of bundling duplicate
  long-lived `agent/` or `target/` implementations.
