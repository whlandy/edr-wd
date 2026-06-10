"""Pool of target-scoped subagents."""

from __future__ import annotations

import threading
from typing import Optional

from agent.target_config import TargetConfig

from .target_agent import TargetSubAgent


class TargetSubAgentPool:
    """Lazy registry of one TargetSubAgent per configured target."""

    def __init__(self, config: Optional[TargetConfig] = None):
        self.config = config or TargetConfig()
        self._agents: dict[str, TargetSubAgent] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_config(cls, config: Optional[TargetConfig] = None) -> "TargetSubAgentPool":
        return cls(config=config)

    def get(self, target: Optional[str] = None) -> TargetSubAgent:
        name = target or self.config.get_default_target()
        if not name:
            raise ValueError("No target name and no default_target configured")
        with self._lock:
            agent = self._agents.get(name)
            if agent is None:
                agent = TargetSubAgent(name, config=self.config)
                self._agents[name] = agent
            return agent

    def all(self) -> dict[str, TargetSubAgent]:
        with self._lock:
            for name in self.config.list_targets():
                self.get(name)
            return dict(self._agents)
