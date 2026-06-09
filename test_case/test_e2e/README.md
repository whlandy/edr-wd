# E2E Test Layout

This directory contains pytest E2E tests split by target platform.

- `test_edr_full_workflow.py`: Windows-only HiSec EDR workflow. It requires the
  `windows_pywinauto` backend and is skipped automatically on macOS targets.
- `test_macos_hisec_workflow.py`: macOS-only HiSecEndpoint workflow. It requires
  the `macos_accessibility` backend and verifies the native
  `HiSecEndpointAgent` and `EDRClient` windows are visible.

Run the macOS HiSec E2E directly:

```bash
EDR_WD_TARGET=mac-dev python -m pytest test_case/test_e2e/test_macos_hisec_workflow.py -v
```

Run all E2E tests for the active target:

```bash
EDR_WD_TARGET=mac-dev python -m pytest test_case/test_e2e -v
EDR_WD_TARGET=win-dev python -m pytest test_case/test_e2e -v
```

Platform-specific tests guard themselves by backend, so the wrong-platform file
should skip instead of failing.
