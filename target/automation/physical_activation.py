"""进程级显式物理激活声明（106 轮方案 C 收窄版）。

配置 env `EDR_WD_PHYSICAL_ACTIVATION`：
- 多条规则以 ';' 分隔；每条为 `process_name|window_title|automation_id`
  三段竖线分隔；
- 三段必须**同时精确匹配**（归一 strip+casefold）才让路物理点击；
  不接受 suffix、通配符（按字面字符处理）、仅 ID 全局匹配；
- 配置缺失/为空 → 无规则（默认行为零变化）；
- 格式非法 → PhysicalActivationConfigError（明确失败，不静默忽略）。

本模块刻意不依赖 pywinauto，便于在任意平台做配置契约单测；
WindowsGUI 集成点只做薄封装（见 pywinauto_client._semantic_activate）。
"""
from __future__ import annotations

from dataclasses import dataclass


class PhysicalActivationConfigError(ValueError):
    """EDR_WD_PHYSICAL_ACTIVATION 配置格式非法（明确失败，不静默忽略）。"""


@dataclass(frozen=True)
class PhysicalActivationRule:
    """一条物理激活声明：三段全部精确匹配才生效。"""

    process_name: str
    window_title: str
    automation_id: str


def _normalize(value: str) -> str:
    return value.strip().casefold()


def parse_physical_activation_spec(raw: str | None) -> tuple[PhysicalActivationRule, ...]:
    """解析配置字符串；缺失/空白 → 空规则集；非法 → 明确异常。"""
    if raw is None or not raw.strip():
        return ()
    rules: list[PhysicalActivationRule] = []
    for index, chunk in enumerate(raw.split(";"), start=1):
        if not chunk.strip():
            raise PhysicalActivationConfigError(
                f"EDR_WD_PHYSICAL_ACTIVATION 第 {index} 条规则为空段")
        parts = chunk.split("|")
        if len(parts) != 3:
            raise PhysicalActivationConfigError(
                f"EDR_WD_PHYSICAL_ACTIVATION 第 {index} 条规则必须是 "
                f"'process_name|window_title|automation_id' 三段，实际 {len(parts)} 段")
        fields = [part.strip() for part in parts]
        if any(not field for field in fields):
            raise PhysicalActivationConfigError(
                f"EDR_WD_PHYSICAL_ACTIVATION 第 {index} 条规则存在空字段")
        rules.append(PhysicalActivationRule(
            process_name=fields[0],
            window_title=fields[1],
            automation_id=fields[2],
        ))
    return tuple(rules)


def physical_activation_required(
    rules: tuple[PhysicalActivationRule, ...],
    process_name: str | None,
    window_title: str | None,
    automation_id: str | None,
) -> bool:
    """三元组同时精确匹配（归一后全等）才返回 True；空规则集恒 False。"""
    if not rules:
        return False
    if not process_name or not window_title or not automation_id:
        return False
    proc = _normalize(process_name)
    title = _normalize(window_title)
    aid = _normalize(automation_id)
    return any(
        _normalize(rule.process_name) == proc
        and _normalize(rule.window_title) == title
        and _normalize(rule.automation_id) == aid
        for rule in rules
    )
