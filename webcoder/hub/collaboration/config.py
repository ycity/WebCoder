"""Feature gates are process settings, never model-editable authorization."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CollaborationConfig:
    enabled: bool = False
    events_enabled: bool = False
    collector_enabled: bool = False
    analysis_dispatch_enabled: bool = False

    @classmethod
    def from_settings(cls, settings):
        # Settings already refuses a child switch without the master switch (WEBCODER_COLLABORATION_ENABLED).
        return cls(settings.collaboration_enabled, settings.mcp_events_enabled,
                   settings.monitor_collector_enabled, settings.analysis_dispatch_enabled)
