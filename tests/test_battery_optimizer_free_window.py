"""Tests for free-window SoC ceiling: LP charges only to demand coverage.

These tests require scipy and are automatically skipped when it is not installed.
They verify that during zero-cost import windows the LP charges only enough to
cover load + automation-export demand through to the next free window, rather
than filling to 100% SoC via speculative arbitrage.
"""

from __future__ import annotations

import importlib
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"

_SENTINEL = object()

_STUB_MODULE_NAMES = (
    "homeassistant",
    "homeassistant.util",
    "homeassistant.util.dt",
    "power_sync",
    "power_sync.optimization",
    "power_sync.optimization.battery_optimizer",
    "power_sync.optimization.schedule_reader",
)


def _install_stubs() -> None:
    ha_root = types.ModuleType("homeassistant")
    ha_util = types.ModuleType("homeassistant.util")
    ha_dt = types.ModuleType("homeassistant.util.dt")
    ha_dt.now = lambda *args, **kwargs: datetime(2026, 5, 4, 0, 0, tzinfo=timezone.utc)
    ha_dt.utcnow = lambda *args, **kwargs: datetime(2026, 5, 4, 0, 0, tzinfo=timezone.utc)
    ha_dt.UTC = timezone.utc
    ha_util.dt = ha_dt
    ha_root.util = ha_util

    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.util"] = ha_util
    sys.modules["homeassistant.util.dt"] = ha_dt

    ps_module = types.ModuleType("power_sync")
    ps_module.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = ps_module

    optimization_module = types.ModuleType("power_sync.optimization")
    optimization_module.__path__ = [str(COMPONENT_ROOT / "optimization")]
    sys.modules["power_sync.optimization"] = optimization_module


@pytest.fixture()
def battery_optimizer_module():
    saved_modules = {name: sys.modules.get(name, _SENTINEL) for name in _STUB_MODULE_NAMES}
    for name in _STUB_MODULE_NAMES:
        sys.modules.pop(name, None)

    _install_stubs()
    module = importlib.import_module("power_sync.optimization.battery_optimizer")
    try:
        yield module
    finally:
        for name in _STUB_MODULE_NAMES:
            if saved_modules[name] is _SENTINEL:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved_modules[name]


def _optimizer(module, **kwargs):
    defaults = dict(
        capacity_wh=13500,
        max_charge_w=7000,
        max_discharge_w=7000,
        backup_reserve=0.20,
        interval_minutes=5,
    )
    defaults.update(kwargs)
    return module.BatteryOptimizer(**defaults)


def test_free_window_charges_to_demand_ceiling(battery_optimizer_module):
    """LP charges only to cover paid-period demand, not to 100% SoC."""
    pytest.importorskip("scipy")

    # 3h free (0c) → 3h paid (34c), 0.5 kW flat load, no solar.
    # Battery: 13.5 kWh, 20% SoC (= backup_reserve, no usable charge on entry).
    # Ceiling = backup_reserve + inter-window demand / capacity
    #         = 0.20 + (36 slots × 0.5 kW × 5/60 h) / 13.5
    #         = 0.20 + 1.5 / 13.5 ≈ 0.311
    n_per_hour = 12
    free_slots = 3 * n_per_hour   # 36
    paid_slots = 3 * n_per_hour   # 36
    n = free_slots + paid_slots    # 72

    opt = _optimizer(battery_optimizer_module, horizon_hours=6)

    result = opt.optimize(
        import_prices=[0.0] * free_slots + [0.341] * paid_slots,
        export_prices=[0.0] * n,
        solar_forecast=[0.0] * n,
        load_forecast=[0.5] * n,
        current_soc=0.20,
        acquisition_cost_kwh=0.0,
    )

    assert result.feasible
    free_window = result.schedule.actions[:free_slots]

    peak_soc = max(a.soc for a in free_window)
    # LP should charge to roughly 0.311 — well below 100%.
    assert peak_soc >= 0.28, f"LP under-charged: peak SoC {peak_soc:.3f} below expected ~0.311"
    assert peak_soc < 0.45, f"LP over-charged: peak SoC {peak_soc:.3f} exceeds demand ceiling"


def test_free_window_two_windows_each_caps_independently(battery_optimizer_module):
    """Each free window is capped by the demand until its own next free window."""
    pytest.importorskip("scipy")

    # Free (3h) → paid (3h, 0.5 kW) → free (3h) → paid (3h, 0.5 kW)
    # Each free window has the same inter-window demand (1.5 kWh), so each ceiling ≈ 0.311.
    n_per_hour = 12
    seg = 3 * n_per_hour   # 36 slots per segment
    n = 4 * seg             # 144

    opt = _optimizer(battery_optimizer_module, horizon_hours=12)

    result = opt.optimize(
        import_prices=[0.0] * seg + [0.341] * seg + [0.0] * seg + [0.341] * seg,
        export_prices=[0.0] * n,
        solar_forecast=[0.0] * n,
        load_forecast=[0.5] * n,
        current_soc=0.20,
        acquisition_cost_kwh=0.0,
    )

    assert result.feasible

    free_window_1 = result.schedule.actions[:seg]
    free_window_2 = result.schedule.actions[2 * seg : 3 * seg]

    peak_soc_1 = max(a.soc for a in free_window_1)
    peak_soc_2 = max(a.soc for a in free_window_2)

    # Neither window should charge to max; each caps at ~0.311.
    assert peak_soc_1 < 0.45, f"Window 1 over-charged: {peak_soc_1:.3f}"
    assert peak_soc_2 < 0.45, f"Window 2 over-charged: {peak_soc_2:.3f}"


def test_free_window_buffer_adds_headroom(battery_optimizer_module):
    """free_window_soc_buffer raises the ceiling above pure demand."""
    pytest.importorskip("scipy")

    n_per_hour = 12
    free_slots = 3 * n_per_hour
    paid_slots = 3 * n_per_hour
    n = free_slots + paid_slots

    opt = _optimizer(battery_optimizer_module, horizon_hours=6)
    # buffer = 0.10 raises ceiling from ~0.311 to ~0.411
    opt.free_window_soc_buffer = 0.10

    result = opt.optimize(
        import_prices=[0.0] * free_slots + [0.341] * paid_slots,
        export_prices=[0.0] * n,
        solar_forecast=[0.0] * n,
        load_forecast=[0.5] * n,
        current_soc=0.20,
        acquisition_cost_kwh=0.0,
    )

    assert result.feasible
    free_window = result.schedule.actions[:free_slots]
    peak_soc = max(a.soc for a in free_window)

    # Buffer adds ~10% capacity headroom above the no-buffer ceiling (~0.311).
    assert peak_soc >= 0.38, f"Buffer not applied: peak SoC {peak_soc:.3f} below ~0.411"
    assert peak_soc < 0.55, f"LP over-charged even with buffer: {peak_soc:.3f}"
