"""Shared strategy and execution settings."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    risk_percent: float = 0.5
    max_daily_loss_percent: float = 3.0
    max_spread_points: int = 50  # Two-decimal equivalent, see spread_reference_point
    spread_reference_point: float = 0.01
    max_concurrent_trades: int = 1
    magic_number: int = 310521
    sessions: tuple = ((7, 11), (13, 17))
    stop_spread_multiple: float = 1.5
    partial_r: float = 1.5
    partial_fraction: float = 0.5
    target_r: float = 4.0
    ema_period: int = 21
    impulse_body_fraction: float = 0.50
    setup_m5_bars: int = 96       # 8 hours max from QML formation to entry
    sweep_to_tap_bars: int = 12   # 1 hour max from fakeout sweep to QML zone tap
    warmup_bars: int = 600
    poll_seconds: float = 0.25
    entry_window_seconds: int = 10
    deviation_points: int = 15

    def accepts_spread(self, spread):
        return 0 < spread <= self.max_spread_points * self.spread_reference_point + 1e-9


SETTINGS = Settings()
BAR_SECONDS = 300
STRUCTURE_SECONDS = 900
DATA_DIR = Path(__file__).resolve().parent / "data" / "XAUUSD_BARS"