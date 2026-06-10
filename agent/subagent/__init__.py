"""
Target-scoped subagents for EDR-WD.

Each TargetSubAgent owns one target's lifecycle/MCP state. The main agent
should ask the pool for a target agent instead of juggling global sessions.
"""

from .pool import TargetSubAgentPool
from .state import TargetState
from .target_agent import TargetSubAgent

__all__ = ["TargetState", "TargetSubAgent", "TargetSubAgentPool"]
