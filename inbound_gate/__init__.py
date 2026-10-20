"""Intake gate: filing, de-duplicating and recording incoming items."""

from .core import InboundGate
from .models import IntakeItem, IntakeRequest

__all__ = ["InboundGate", "IntakeItem", "IntakeRequest"]
__version__ = "2.0.0"