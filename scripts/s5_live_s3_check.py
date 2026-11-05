#!/usr/bin/env python3
"""S3/S4 实机验证驱动(11.26):真实租约/binding/focus/门禁链。

2112 复验修订:
- 阶段判定精确化:negative-gate-pass / positive-flow-pass /
  blocked-unsupported / not-run / fail 分别计数;正向必要项缺失或失败
  不返回成功退出码;capability 门拒绝只能计入负例(unsupported);
- 负例锁定精确错误码(不接受任何 ok=false 都算预期);
- focus-only 使用协议契约 action_id=None(不是空字符串;MCP JSON 层
  省略可选参数即为 focus-only);
- 凭据递归脱敏在**源头**执行:任何进入 debug/报告的值先过
  redact_credentials(嵌套 dict/list 全覆盖),run_token 等绝不落盘;
- 阶段模型:V1a/V2/V7(负例门)、V1b/V3/V4(正向门);V4 capability
  拒绝 → blocked-unsupported,V5/V6/V-semantic 记 not-run。
"""
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "http://127.0.0.1:18765/mcp"
OUT = Path(__file__).resolve().parents[1] / "result-report" / \
    "s5-windows-acceptance" / "live"
OUT.mkdir(parents=True, exist_ok=True)
_session = {"id": None}

CREDENTIAL_KEYS = {"run_token", "binding_token", "token", "lease_token",
                   "digest", "password", "secret"}


def redact_credentials(value):
    """递归凭据脱敏(源头;dict/list 全深度)。"""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key).lower() in CREDENTIAL_KEYS and isinstance(item, str):
                out[key] = "<redacted>"
            else:
                out[key] = redact_credentials(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_credentials(item) for item in value]
    return value


def rpc(method, params=None, notification=False):
    payload = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    if not notification:
        payload["id"] = rpc._next
        rpc._next += 1
    req = urllib.request.Request(
        BASE, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 **({"mcp-session-id": _session["id"]}
                    if _session["id"] else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            sid = resp.headers.get("mcp-session-id")
            if sid:
                _session["id"] = sid
            body = resp.read().decode()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return {"raw": body[:400]}


rpc._next = 1


def call_tool(name, arguments):
    response = rpc("tools/call", {"name": name, "arguments": arguments})
    result = response.get("result") or {}
    if result.get("isError"):
        return {"ok": False,
                "code": "tool_execution_error",
                "raw": json.dumps(result)[:300]}
    for item in result.get("content") or []:
        if item.get("type") == "text":
            try:
                parsed = json.loads(item["text"])
            except json.JSONDecodeError:
                return {"raw": item["text"]}
            if isinstance(parsed, dict) and isinstance(
                    parsed.get("result"), str):
                try:
                    inner = json.loads(parsed["result"])
                    if isinstance(inner, dict):
                        return inner
                except json.JSONDecodeError:
                    pass
            return parsed
    return response


def main() -> int:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from target.product_profiles import get_capability
    digest = get_capability("windows_hisec", "windows_pywinauto").digest

    rpc("initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "s5-live-s3", "version": "0.1"}})
    rpc("notifications/initialized", notification=True)

    # 阶段桶(2112 修订):
    stages = {"negative_gate_pass": [], "positive_flow_pass": [],
              "blocked_unsupported": [], "not_run": [], "fail": []}
    checks = {}
    debug = {}
    exit_code = 0

    def record(name, bucket, **fields):
        checks[name] = {"bucket": bucket, **fields}
        stages[bucket].append(name)
        print(f"{name} [{bucket}]: {json.dumps(fields, ensure_ascii=False)}")

    def capture(name, value):
        # 源头递归脱敏(2112 缺口2):凭据/原生身份不进任何落盘结构。
        debug[name] = redact_credentials(value)

    # ---- V1a: 错误 digest 必须以精确码拒绝 ----
    bad = call_tool("begin_product_run", {
        "profile_id": "windows_hisec", "digest": "0" * 64})
    capture("V1a-raw", bad)
    if bad.get("ok") is False and bad.get("code") == \
            "product profile digest mismatch":
        record("V1a_bad_digest_refused", "negative_gate_pass",
               code=bad.get("code"))
    else:
        record("V1a_bad_digest_refused", "fail",
               expected="product profile digest mismatch",
               actual=bad.get("code"))
        exit_code = 2

    # ---- V1b: 正确 digest 获取生产租约(正向必要项) ----
    run = call_tool("begin_product_run", {
        "profile_id": "windows_hisec", "digest": digest,
        "lease_seconds": 300})
    capture("V1b-raw", run)
    if run.get("ok") is True and run.get("run_token"):
        record("V1b_begin_product_run", "positive_flow_pass",
               lease_expires_in=run.get("expires_in"))
    else:
        record("V1b_begin_product_run", "fail", code=run.get("code"))
        (OUT / "s3-live-checks.json").write_text(json.dumps(
            {"at": datetime.now(timezone.utc).isoformat(),
             "target": "11.26-desktop-s917l7s-win10",
             "stages": stages, "checks": checks, "debug": debug},
            ensure_ascii=False, indent=1), encoding="utf-8")
        return 2
    token = run["run_token"]
    binding_token = None

    try:
        # ---- V2: 未知 binding token → window_binding_not_found(负例锁码) ----
        state_missing = call_tool("window_binding_state", {
            "window_id": "main", "binding_token": "WB-none",
            "run_token": token})
        if state_missing.get("ok") is False and \
                state_missing.get("code") == "window_binding_not_found":
            record("V2_state_unknown_token_refused", "negative_gate_pass",
                   code=state_missing.get("code"))
        else:
            record("V2_state_unknown_token_refused", "fail",
                   expected="window_binding_not_found",
                   actual=state_missing.get("code"))
            exit_code = 2

        # ---- V3: golden W 声明注册(正向必要项) ----
        declared = call_tool("register_window_declaration", {
            "declarations_json": json.dumps(
                {"WG": {"processName": "EDRClient.exe",
                        "titleRegex": "^logo1$"}}),
            "run_token": token})
        if declared.get("ok") is True and "WG" in (
                declared.get("declared") or []):
            record("V3_register_golden_declaration", "positive_flow_pass",
                   declared=declared.get("declared"))
        else:
            record("V3_register_golden_declaration", "fail",
                   code=declared.get("code"))
            exit_code = 2

        # ---- V4: 绑定(正向必要项;capability 拒绝 → blocked) ----
        bound = call_tool("bind_entry_window", {
            "window_id": "WG",
            "identity": {"processName": "EDRClient.exe",
                         "titleRegex": "^logo1$"},
            "run_token": token})
        capture("V4-raw", bound)
        if bound.get("binding_token"):
            record("V4_bind_golden_window", "positive_flow_pass")
            binding_token = bound["binding_token"]
        elif bound.get("code") == "window_sequence_capability_unavailable":
            # 生产 backend 未声明强实例能力:如实 blocked-unsupported,
            # 不伪造 ready;正向链后续阶段 not-run。
            record("V4_bind_golden_window", "blocked_unsupported",
                   code=bound.get("code"),
                   note="fail closed 正确;正向链在此前置阻塞")
            for pending in ("V5_focus_and_observe", "V6_reverify_bound",
                            "V-semantic-gui-click"):
                record(pending, "not_run",
                       reason="blocked by capability gate")
        else:
            record("V4_bind_golden_window", "fail",
                   code=bound.get("code"))
            exit_code = 2

        if binding_token:
            # ---- V5: focus-only(协议契约 action_id=None,不是空串) ----
            prepared = call_tool("focus_bound_window", {
                "window_id": "WG", "binding_token": binding_token,
                "run_token": token, "action_id": None, "observe": True})
            capture("V5-raw", prepared)
            if prepared.get("ok") is True:
                record("V5_focus_and_observe", "positive_flow_pass")
            else:
                record("V5_focus_and_observe", "fail",
                       code=prepared.get("code"))
                exit_code = 2
            # ---- V6: 只读重验(负例前置:LOST 状态拒绝已单测) ----
            reverified = call_tool("reverify_bound_window", {
                "window_id": "WG", "binding_token": binding_token,
                "run_token": token})
            capture("V6-raw", reverified)
            # 成功契约:state=BOUND(无 ok 键);拒绝形态为 ok:false+code。
            if reverified.get("state") == "BOUND" and \
                    reverified.get("ok") is not False:
                record("V6_reverify_bound", "positive_flow_pass",
                       state="BOUND")
            else:
                record("V6_reverify_bound", "fail",
                       code=reverified.get("code"))
                exit_code = 2

        # ---- V7: 未声明 W 拒绝(负例锁码) ----
        undeclared = call_tool("bind_entry_window", {
            "window_id": "W-NOT-DECLARED",
            "identity": {"processName": "EDRClient.exe"},
            "run_token": token})
        if undeclared.get("ok") is False and \
                undeclared.get("code") == "window_binding_not_declared":
            record("V7_undeclared_window_refused", "negative_gate_pass",
                   code=undeclared.get("code"))
        elif undeclared.get("code") == \
                "window_sequence_capability_unavailable":
            # capability 门先于声明门:本负例 blocked,不计通过。
            record("V7_undeclared_window_refused", "blocked_unsupported",
                   code=undeclared.get("code"),
                   note="capability 门先于 not_declared 门;此负例未达")
        else:
            record("V7_undeclared_window_refused", "fail",
                   expected="window_binding_not_declared",
                   actual=undeclared.get("code"))
            exit_code = 2
    finally:
        ended = call_tool("end_product_run", {"run_token": token})
        if ended.get("ok") is True:
            record("V8_end_product_run", "negative_gate_pass",
                   cleanup_errors=ended.get("cleanup_errors"))
        else:
            record("V8_end_product_run", "fail", code=ended.get("code"))
            exit_code = 2
        after = call_tool("window_binding_state", {
            "window_id": "WG",
            "binding_token": (binding_token or "WB-gone"),
            "run_token": token})
        if after.get("ok") is False and after.get("code") in (
                "window_binding_lease_refused", "window_binding_not_found"):
            record("V9_state_after_end_refused", "negative_gate_pass",
                   code=after.get("code"))
        else:
            record("V9_state_after_end_refused", "fail",
                   actual=after.get("code"))
            exit_code = 2

    (OUT / "s3-live-checks.json").write_text(json.dumps(
        {"at": datetime.now(timezone.utc).isoformat(),
         "target": "11.26-desktop-s917l7s-win10",
         "exit_code": exit_code,
         "stages": {k: v for k, v in stages.items()},
         "verdict": ("fail"
                     if stages["fail"]
                     else ("blocked-unsupported"
                           if stages["blocked_unsupported"]
                           else ("positive-flow-pass"
                                 if stages["positive_flow_pass"]
                                 else "not-run"))),
         "exit_semantics": {"0": "positive-flow-pass(全部正向必要项通过)",
                            "2": "fail(必要项失败/负例码不符)",
                            "3": "blocked-unsupported(能力前置阻塞,"
                                 "正向链未完成)"},
         "checks": checks, "debug": debug},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved:", OUT / "s3-live-checks.json")
    if stages["fail"]:
        final = 2
    elif stages["blocked_unsupported"]:
        final = 3
    elif stages["positive_flow_pass"]:
        final = 0
    else:
        final = 3
    print("exit_code:", final)
    return final


if __name__ == "__main__":
    sys.exit(main())
