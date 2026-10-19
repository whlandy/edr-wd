# Application catalog

All registered applications are managed in one target-owned file:
[`apps.json`](apps.json). `default_app` is `edrclient`; when a target omits
`app_profile`, the platform's EDRClient profile is selected. An explicit
`app_profile` still selects another application. `edr-wd products` shows the
registered apps, platform paths, and complete declared window inventory.

Each `apps[]` entry has an `app_id`, display `name`, and one or more platform
`profiles`. A profile includes:

- `application.path`: absolute installed executable or `.app` path on that
  platform. Optional `entry_path` is the HiSec entry executable; macOS may
  also provide `app_name`/`bundle_id`.
- `windows[]`: every known window role, its human-readable `name`, optional
  exact title `names`, stable role `window_path` (for example
  `main/log_center`), process owner, title pattern, and optional parent role.
  Dynamic-title windows use a descriptive name and role; the runtime still
  verifies process, parent, and native window identity.
- `allowed_action_ids` and optional `intents`: product action policy and
  business intent mappings. Generic intents support one semantic click plus
  explicit result assertions; longer workflows need a reviewed adapter.

To add software, add an `apps[]` entry (or a new platform profile under an
existing app), validate it with the offline tests and `edr-wd products`, then
deploy the tracked target payload. Do not put credentials, shell commands, or
runtime PID/HWND values in the catalog. The target uses only its locally
installed path; an MCP caller cannot supply an arbitrary executable path.
Windows activation checks that the configured executable basename belongs to
the declared window owner before launching or focusing it. EDRClient keeps
its specialized HiSec activation sequence, but its executable and entry paths
come from this same catalog.

The target verifies the digest of profile ID, version, backend, roles, and
allowed actions before a product run. Activation paths and Agent-side intent
templates are not included in that digest. Agent and target should therefore
be deployed from the same catalog revision. A successful offline test does not
prove the app's real window titles or installation paths on a particular
machine; verify those on the target before claiming live coverage.
