"""Read-only smoke test for any installed declarative product profile."""

from __future__ import annotations

from agent.products import backend_for_product, get_product_adapter, get_product_profile


def run_registered_product_tests(client, *, profile: str,
                                 verbose: bool = False) -> tuple[int, int, int, list, bool]:
    backend = backend_for_product(profile)
    product = get_product_profile(profile, backend)
    adapter = get_product_adapter(profile, backend)
    checks: list[tuple[str, bool]] = []
    token = None
    try:
        status = client.call_tool("status", {})
        reported = status.get("backend_kind") or status.get("backend")
        checks.append(("backend", reported in {backend, "uia", "win32"}
                       if backend == "windows_pywinauto" else reported == backend))
        if checks[-1][1]:
            activate = getattr(adapter, "activate", None)
            activated = (activate(lambda name, args: client.call_tool(name, args))
                         if callable(activate) else {"ok": True})
            checks.append(("activation", bool(activated.get("ok"))))
        if checks[-1][1]:
            started = client.call_tool("begin_product_run", {
                "profile_id": profile, "digest": product.digest,
            })
            token = started.get("run_token") if isinstance(started, dict) else None
            checks.append(("product_run", bool(started.get("ok") and token)))
        if token:
            focused = client.call_tool("focus_window_role", {"role": "main", "run_token": token})
            checks.append(("main_window", bool(focused.get("ok"))))
            if focused.get("ok"):
                bundle = client.call_tool("observe_window_bundle", {
                    "run_token": token, "capture_image": False, "include_ocr": False,
                })
                owner = (bundle.get("window_identity") or {}).get("process_name")
                main = adapter.resolve_window_role("main")
                checks.append(("owned_observation", bool(bundle.get("ok") and
                              owner in main.process_names)))
    except Exception:
        checks.append(("product_smoke_exception", False))
    finally:
        if token:
            try:
                ended = client.call_tool("end_product_run", {"run_token": token})
                checks.append(("product_run_cleanup", bool(ended.get("ok"))))
            except Exception:
                checks.append(("product_run_cleanup", False))
            if verbose:
                print(f"  product run cleanup: {checks[-1][1]}")
    return _summary(checks)


def _summary(checks: list[tuple[str, bool]]) -> tuple[int, int, int, list, bool]:
    for name, passed in checks:
        print(f"{'PASS' if passed else 'FAIL'}: {name}")
    failed = [name for name, passed in checks if not passed]
    return (len(checks) - len(failed), len(failed), 0, failed, not failed)
