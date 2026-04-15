"""Gate library for Strategy Engine v2.

Pure Python gates that evaluate FullDataSurface and return GateResult.
No external dependencies (no aiohttp, SQLAlchemy, structlog).
"""

from strategies.gates.base import Gate, GateResult
from strategies.gates.timing import TimingGate
from strategies.gates.direction import DirectionGate
from strategies.gates.confidence import ConfidenceGate
from strategies.gates.session_hours import SessionHoursGate
from strategies.gates.clob_sizing import CLOBSizingGate
from strategies.gates.source_agreement import SourceAgreementGate
from strategies.gates.delta_magnitude import DeltaMagnitudeGate
from strategies.gates.taker_flow import TakerFlowGate
from strategies.gates.cg_confirmation import CGConfirmationGate
from strategies.gates.spread import SpreadGate
from strategies.gates.dynamic_cap import DynamicCapGate
from strategies.gates.regime import RegimeGate
from strategies.gates.macro_direction import MacroDirectionGate
from strategies.gates.trade_advised import TradeAdvisedGate
from strategies.gates.entry_price_floor import EntryPriceFloorGate

__all__ = [
    "Gate",
    "GateResult",
    "TimingGate",
    "DirectionGate",
    "ConfidenceGate",
    "SessionHoursGate",
    "CLOBSizingGate",
    "SourceAgreementGate",
    "DeltaMagnitudeGate",
    "TakerFlowGate",
    "CGConfirmationGate",
    "SpreadGate",
    "DynamicCapGate",
    "RegimeGate",
    "MacroDirectionGate",
    "TradeAdvisedGate",
    "EntryPriceFloorGate",
]
