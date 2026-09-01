"""顺手把黄金轨迹导出成 maa-fw 的节点表（v2）。

**为什么在这里做**：不这么做的话，导出就得靠人记得去跑一条命令。忘了的后果
不是报错，是**根本没人做** —— 而没做的表现是「maa-fw 那边加载了什么都不做」，
两边都不报错。

实测过不做的样子：edr-wd 的原生轨迹直接喂给 maa-fw，5 个节点 coerce 成功 4 个，
但每个都变成 DirectHit + DoNothing，next 的 "step-0002" 被按字符拆成
['s','t','e','p',…]。加载成功、能跑、什么都不做。

**转换器不在这个仓库**：v2 的形状定义只在 edr-cloud-recorder 的 trace_schema
一处。在这边再抄一份就是第二个会漂移的定义，而漂移之后我们会拿自己那份形状
去喂 maa-fw —— 正好回到上面那个坑里。

录制器不在本机时**不算失败**：导出是附加产物，编译不能因此挂掉。
位置用 EDR_RECORDER_HOME 覆盖，默认找同级目录。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Mapping

DEFAULT_RECORDER = Path(__file__).resolve().parents[2].parent / "edr-cloud-recorder"


def recorder_home() -> Path | None:
    home = Path(os.environ.get("EDR_RECORDER_HOME") or DEFAULT_RECORDER).expanduser()
    return home if (home / "scripts" / "desktop_to_v2.py").exists() else None


def to_maa_nodes(golden: Mapping[str, Any]) -> tuple[dict | None, str]:
    """返回 (v2 轨迹, 说明)。做不成时轨迹为 None，说明里写清为什么。"""
    home = recorder_home()
    if home is None:
        return None, (
            "找不到 edr-cloud-recorder（设 EDR_RECORDER_HOME 指向它）；"
            "跳过 maa 节点表导出")
    scripts = str(home / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        from desktop_to_v2 import convert
    except Exception as error:                        # 依赖不全也只是跳过
        return None, f"导入转换器失败：{type(error).__name__}: {error}"
    try:
        return convert(dict(golden)), "ok"
    except Exception as error:
        # 这个要显眼：轨迹**本身**有问题，不是环境问题
        return None, f"转换失败：{type(error).__name__}: {error}"
