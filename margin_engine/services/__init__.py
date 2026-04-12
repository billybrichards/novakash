"""
Margin engine services — pure business logic modules.

This package contains stateless services that operate on domain objects.
No infrastructure dependencies; all configuration is injected.
"""

from margin_engine.services.cascade_detector import (
    analyze_cascade,
    CascadeState,
    CascadeInfo,
)
from margin_engine.services.cascade_fade import (
    CascadeFadeStrategy,
    CascadeFadeConfig,
)

__all__ = [
    "analyze_cascade",
    "CascadeState",
    "CascadeInfo",
    "CascadeFadeStrategy",
    "CascadeFadeConfig",
]
