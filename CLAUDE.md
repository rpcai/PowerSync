# PowerSync — Dev Notes

This file is the playbook for re-applying our local customisations onto fresh
upstream. Upstream moves fast (multiple patch versions per day) and refactors
the LP formulation periodically, so we expect to re-apply these features
regularly. Treat this as the source of truth — keep it in sync with the
actual commit history on the active branch.

## Deployment Scope

**Our deployment is FoxESS-only.** Upstream supports 10+ battery systems
(Tesla, Sungrow, GoodWe, Sigenergy, AlphaESS, Solax, ESY Sunhome, SAJ,
Neovolt, Bytewatt, etc.) but we only run on FoxESS hardware. When auditing
upstream changes:

- **Commit subjects can mislead.** A commit prefixed `fix(sungrow):` may
  still affect us if the code path is gated on a list that includes FoxESS.
  Example: `e19b6fea` ("fix(sungrow): stabilize tariff and free-period
  charging") added `_should_smooth_free_import_schedule()` gated on
  `_supports_target_charge_power()` → membership in
  `TARGET_CHARGE_POWER_BATTERY_SYSTEMS`, which includes `BATTERY_SYSTEM_FOXESS`.
  Always read the gate, not just the prefix.
- **Systems we can ignore in upstream noise:** Tesla Powerwall, Sigenergy,
  Sungrow, GoodWe, AlphaESS, Solax, ESY Sunhome, SAJ, Neovolt, Bytewatt,
  Fronius, Huawei, Enphase, Zeversolar — provided the change is in a
  vendor-specific file path (`*/sigenergy_*`, `*/sungrow_*`, etc.) AND
  is not referenced from `optimization/`.
- **Systems that affect us regardless of subject:** Any change touching
  `TARGET_CHARGE_POWER_BATTERY_SYSTEMS` membership or the optimizer's
  cross-system bound logic. FoxESS sits in those shared lists.

## Branch Strategy

We keep one active branch that tracks `origin/main` with our customisations
cherry-picked on top. The branch is named for the upstream version it sits
on, e.g. `powersync-custom-v2.12.473`. When upstream advances:

1. Create a NEW branch from the new `origin/main` (do NOT reuse the old one).
2. Cherry-pick our feature commits onto it.
3. Resolve conflicts.
4. Run the test suite + deploy to HA for live validation.
5. Once verified, the old branch can be archived (kept as the prior recovery
   point); deprecate by renaming with a `-archive-YYYYMMDD` suffix only after
   the new branch is proven stable.

**Why a new branch and not `reset --hard origin/main` on the existing one?**
The old branch is the recovery point. If a cherry-pick goes wrong, you have
something to walk back to. `reset --hard` followed by `cherry-pick` on the
SAME branch is OK in theory but riskier when conflicts force multi-step
resolution — better to have an immutable reference.

**Never rebase mechanically.** `git rebase origin/main` has historically
caused silent conflict resolutions that dropped files. Always cherry-pick
explicitly, one commit at a time, so each conflict is a deliberate decision.

Our commit subjects are prefixed `feat(optimizer):`, `fix(optimizer):`, or
`fix:` so they stand out from upstream commits in `git log --oneline`.

## Custom Features

Four cherry-picked commits. Apply in the order below — Feature 4 depends
on Feature 2 (the ceiling block it modifies) and Feature 3 (the
provider-agnostic `pre_window_slot` wiring it relies on).

### Feature 1 — Inhibit Optimizer Exports (`CONF_FACTOR_AUTOMATION_EXPORTS`)

**Intent:** Make the LP plan no battery→grid exports. The per-day
`force_discharge` automations own the export schedule. The LP's job becomes
charging the battery enough to cover house load + the automation export
obligation. Without this toggle the LP and the automations both try to drive
exports and fight each other in the export window.

**Key files & locations:**

| File | What lives there |
|------|------------------|
| `custom_components/power_sync/const.py` | `CONF_FACTOR_AUTOMATION_EXPORTS` constant + `CONF_DAILY_EXPORT_*` legacy keys |
| `custom_components/power_sync/config_flow.py` | Bool toggle in setup flow + options flow (search `CONF_FACTOR_AUTOMATION_EXPORTS`) |
| `custom_components/power_sync/strings.json` | i18n strings (both setup + options sections) |
| `custom_components/power_sync/translations/en.json` | English translations |
| `custom_components/power_sync/optimization/coordinator.py` | All the runtime logic — see below |
| `tests/test_battery_export_allowed_slots.py` | `_automation_coordinator` helper + ~14 tests |

**Runtime logic in `coordinator.py`:**

- `_refresh_automation_segments` (async): reads `.storage/power_sync.automations`
  in an executor thread, filters enabled 7-day time-triggered `force_discharge`
  automations, rounds start/end to 5-min boundary, caches as
  `(start_h, start_m, end_h, end_m, power_w, name)` tuples.
- `_parse_automation_export_segments`: returns cache (sync, called from LP path).
- `_get_automation_export_load`, `_get_automation_export_cap_w`,
  `_get_automation_export_allowed_slots`: per-slot helpers. **All use the snap
  pattern** `now = raw_now.replace(minute=(raw_now.minute // interval) * interval, …)`
  to avoid phantom slot indices when called mid-interval.
- `_apply_automation_export_power`: post-processing pass. Pins each non-charge
  slot in an automation window to the segment's configured wattage, preserves
  charge slots, recalculates SoC trajectory.
- Inhibit block (search `CONF_FACTOR_AUTOMATION_EXPORTS`): when the toggle is
  on, `battery_export_allowed` is forced all-False, and the segment power is
  overlaid as extra `load_forecast[i]` so the LP charges enough to cover the
  export obligation.
- `_should_spread_export_schedule` returns False when CFAE on (otherwise
  spread-export would smear the LP's planned exports across slots, undoing
  the pin).

### Feature 2 — Per-free-window SoC ceiling

**Intent:** Stop the LP charging the battery to 100% during every 0c free
import window when downstream demand is small. The LP's near-zero import
cost combined with the terminal SOC valuation will otherwise pull the
battery to full regardless of how little energy is genuinely useful — leaving
the battery topped-up at sunrise blocking solar self-consumption.

**Behaviour:** for each consecutive run of 0c import periods, cap the
battery energy at the end of that window to:

```
ceiling = backup_reserve + inter_window_demand_kwh/cap + free_window_soc_buffer
```

`inter_window_demand_kwh` is `sum(max(0, load - solar))` between this window
and the next free window. In `profit_max` mode the ceiling is lifted to
`pre_window_soc_target` for any free window ending at-or-before
`pre_window_slot`, so the LP can still fill cheaply before a known
high-value export window.

**Key files & locations:**

| File | What lives there |
|------|------------------|
| `custom_components/power_sync/optimization/battery_optimizer.py` | `_energy_ceiling` computation in `_solve_lp_inner`, applied to `energy_var(t)` bounds |
| `tests/test_battery_optimizer_free_window.py` | scipy-skipped tests (cannot run in this dev env) |

**Where in `battery_optimizer.py`:** the `_free_window_periods` scan and
`_energy_ceiling[]` array are computed AFTER period setup
(`p_import`, `p_export`, … assignments) and BEFORE `optimizer_reserve = …`.
The energy ceiling is applied in the variable bounds loop:
```
bounds.append((soc_0 * cap, soc_0 * cap))
for t in range(1, p_n + 1):
    _lower = reserve_floor[t] * cap
    _upper = min(cap, _energy_ceiling[t] * cap)
    bounds.append((_lower, max(_lower, _upper)))
```
The `max(_lower, _upper)` guards against `_upper < _lower` infeasibility when
recovery_target pulls `reserve_floor` above the ceiling.

`__init__` adds `self.free_window_soc_buffer: float = 0.0` — a tunable knob
for sites that want more headroom (e.g. inverter taper). Default 0 means
"cap exactly at demand coverage."

### Feature 4 — Profit-max free-window ceiling includes downstream demand

**Intent:** When `profit_max` is on, the free-window ceiling should let the
LP charge to exactly the SoC needed to satisfy `pre_window_soc_target` at the
deadline, accounting for any load between the free window and the deadline.
Otherwise the LP charges to exactly `pre_window_soc_target` during the 0c
window, then has to top up at paid grid-import rates to hold that SoC against
load — defeating the point.

**Bug we fixed:** the ceiling lift was `max(_ceiling, pre_window_soc_target)`,
which capped at exactly the target. The hard pre-window floor then forced
paid charging downstream to maintain target SoC against load.

**Resolution:** compute downstream demand from the free window's end to
whichever comes first — the next free window (which can refill itself) or
`pre_window_slot`. Lift the ceiling to
`target + downstream_demand / (cap * eff)`, clipped at 1.0. Divide by `eff`
so the charge INPUT covers discharge efficiency losses.

**Key file:** `custom_components/power_sync/optimization/battery_optimizer.py`
— inside the `_free_window_periods` loop in `_solve_lp_inner`. Look for the
`_demand_to_target_kwh` block; the debug log line is
`"Free-window profit-max ceiling: window_period=[…], target=X%, downstream_demand_to_target=Y kWh, lifted_ceiling=Z%"`.

**Regression test:**
`tests/test_battery_optimizer_free_window.py::test_profit_max_lifts_ceiling_by_downstream_demand`
— covers the exact production scenario (3h free → 4h paid → deadline →
post-deadline).

### Feature 3 — Generalise `profit_max` to all providers

**Intent:** Allow `profit_max` mode to work for any provider, not just
Flow Power. Globird's SUPER_OFF_PEAK 0c period, Octopus IOG cheap windows,
etc. all benefit from filling the battery before their PEAK starts.

**Bug we fixed:** `_next_profit_max_target_slot()` in `coordinator.py` had
two hard gates that returned `None` for non-flow-power providers:
1. `if provider != "flow_power": return None`
2. `if not state: return None` (CONF_FLOW_POWER_STATE required)

With those returning None, `pre_window_slot` was never set on the optimizer,
the pre-window SOC floor was never added to the LP, and the entire
`profit_max` code path was a no-op.

**Resolution:** drop both gates. The Flow Power-specific 17:30 Happy Hour
clamp (rejecting targets ≥ 17:30) is preserved but only fires when provider
== `"flow_power"`; other providers respect their user-configured
`profit_max_target_time` as-is.

**Key file:** `custom_components/power_sync/optimization/coordinator.py` —
the `_next_profit_max_target_slot` method only.

## Rebase Playbook

Step-by-step. Assume you're on the active branch with a clean working tree.

```bash
# 1. Pull upstream
git fetch origin

# 2. Identify the new tip
NEW_BASE=$(git rev-parse origin/main)
NEW_VERSION=$(git show origin/main:custom_components/power_sync/manifest.json | grep version | head -1 | sed 's/[^0-9.]//g')

# 3. Note our 4 feature commits (subject filter avoids upstream noise)
git log --oneline --grep='feat(optimizer)\|fix(optimizer)' | head -10
# Expected: 4 commits, in order:
#   feat(optimizer): inhibit optimizer exports, pin to automation windows
#   feat(optimizer): per-free-window SoC ceiling to prevent greedy charging
#   fix(optimizer): generalise profit_max pre-window slot to all providers
#   fix(optimizer): include downstream demand in profit_max ceiling lift

# 4. Save the 4 hashes (oldest first — cherry-pick order matters)
F1=<hash of inhibit-exports commit>
F2=<hash of soc-ceiling commit>
F3=<hash of profit_max-generalise commit>
F4=<hash of profit_max-ceiling-lift-with-demand commit>

# 5. Create new branch from origin/main
git checkout -b powersync-custom-v${NEW_VERSION} origin/main

# 6. Cherry-pick in order
git cherry-pick $F1
git cherry-pick $F2  # ← conflict likely; see below
git cherry-pick $F3
git cherry-pick $F4  # ← may conflict with F2 if upstream restructures _free_window block

# 7. Run the test suite
python3 -m pytest tests/ -q
#   Our 4 features → 117 tests pass.
#   3 upstream zerohero tests are skipped (incompatible with Feature 2 by
#   design — see "Pre-existing dev-env errors (e.g. missing cryptography)
#   in test_sigenergy_tariff_conversion) are OK.

# 8. Deploy + validate (see Validation Checklist)
```

## Known Conflict Zones

Where conflicts have historically occurred:

| Commit | File | Cause | Resolution |
|--------|------|-------|------------|
| Feature 2 | `battery_optimizer.py` ~line 681 | Upstream inserts new per-period computations after the period setup block; we also insert after that block | Keep BOTH. Upstream's lines first, blank line, then our `_free_window_periods` + `_energy_ceiling` block. Order matters only stylistically. |
| Feature 1 | `coordinator.py` | Upstream sometimes restructures `_async_update_data` flow | Inhibit block + spread-export check + pin call must each find their spot. Re-anchor by searching for the upstream landmarks (`battery_export_allowed = …`, `if self._should_spread_export_schedule()`, `self._last_update_time = dt_util.now()`). |
| Feature 1 | `tests/test_battery_export_allowed_slots.py` | Upstream adds tests in the same file | Auto-merge usually succeeds; if not, our additions go at the end (after the last upstream test, before any module-level helpers). |

If the LP formulation refactors (period structure, energy-variable model,
etc.) the conflict surface in `battery_optimizer.py` grows. In that case
read the new LP code first and translate our ceiling semantics rather than
mechanically resolving — see "Failure Modes" below.

## Upstream Tests We Intentionally Skip

`tests/test_battery_optimizer_export_guard.py` contains 3 tests that assert
upstream's greedy "fill to 100% during every 0c window" behaviour. Our
Feature 2 (per-free-window SoC ceiling) deliberately removes this behaviour
— our LP charges only to demand-coverage (or to `target + downstream demand`
in profit_max mode). Those tests are therefore incompatible with our
contract and are marked `@pytest.mark.skip` with a reason that references
Feature 2:

- `test_charge_block_mask_overrides_free_import_force_charge`
- `test_zerohero_free_import_window_reports_continuous_force_charge`
- `test_zerohero_free_import_before_positive_fit_schedules_export`

**On rebase**: if upstream modifies these tests, the skip marker should
still apply — the underlying contract conflict has not changed. Re-add the
`@pytest.mark.skip(...)` decorator if a merge drops it.

## Known Broken Upstream Tests

These tests are broken on `origin/main` itself — not caused by our patches.
Confirm by checking out the file from `origin/main` alone and re-running.

| Test | Origin | Reason |
|------|--------|--------|
| `tests/test_battery_optimizer_export_guard.py::test_target_export_cap_is_separate_from_total_discharge` | Added in upstream `f6ac6dfa` (v2.12.473) | Asserts `battery_discharge_w > 2500` but actual max is 1000W; appears to be a test-only bug. Survives `git checkout origin/main -- tests/test_battery_optimizer_export_guard.py custom_components/power_sync/optimization/battery_optimizer.py`. |

Run the suite with `--deselect <test>` to exclude broken upstream tests, or
just visually exclude them when reviewing the summary. **Do not patch the
test or production code** — let upstream own the fix; revisit on each rebase.

## Failure Modes (lessons from past rounds)

- **LP refactor: `t`-indexed → period-indexed.** Upstream rewrote the LP to
  use `_LpPeriod` aggregation. Our SoC ceiling used `_t` base-slot indices
  and applied to a cumulative-SOC `b_ub` row that no longer existed. The
  translation: cumulative SOC row → per-boundary `energy_var(t)` upper bound;
  base-slot index `_t` → period index `_p`; `_wb` base-slot → `periods[_wb].end`
  base-slot. The semantics are identical; the array indexing is different.
  If upstream refactors the LP again, **do not blindly resolve conflicts** —
  read the new structure first.

- **`profit_max` provider gate.** A `provider != "flow_power"` check at the
  top of `_next_profit_max_target_slot()` silently disabled the entire
  `profit_max` feature for non-Flow-Power users. The other parts of the
  feature (the LP ceiling lift, the hard floor constraint) all looked
  correct, but the function feeding them returned None. Lesson: when a
  feature appears "to have no effect," trace from the OUTPUT (LP constraint
  not added) backwards to the INPUTS (`pre_window_slot` is None), not from
  the user-facing toggle forwards.

- **`per_slot_max_export_w` dead code.** Coordinator set this to None
  unconditionally; the optimizer branch reading it never fired. Dropped
  entirely during the rebuild rather than ported. Lesson: when porting,
  audit for dead code before re-applying.

- **Greedy charge override in `_build_schedule`.** Pre-LP-refactor code had
  a `if free_import_slot and action == "charge":` branch that would force
  max-charge in any 0c slot regardless of LP output. Once the SoC ceiling
  feature shipped, this override defeated it. **Must remove** during any
  port; upstream re-introduces a softer version via `_free_charge_bonus`
  which is OK because the ceiling hard-bounds it.

- **Ceiling `max(ceiling, target)` capped at target instead of lifting
  enough.** First version of Feature 3 set the profit_max ceiling lift
  to `max(_ceiling, pre_window_soc_target)`. The LP charged exactly to
  the target during free windows then paid for top-ups downstream. Always
  remember: a `target` is a FLOOR, not a CAP. If the LP is allowed to go
  above target where it's free, it should. Fix: ceiling becomes
  `target + downstream_demand/(cap*eff)`. See Feature 4.

- **`_spread_import_schedule` masks per-slot LP decisions.** The coordinator
  post-processes the LP solution to flatten same-price charge windows into
  uniform power across all slots. When debugging "why did the LP only charge
  at X kW?", check whether the spread function is rewriting it.
  `_should_spread_import_schedule()` returns True when `spread_import_enabled`
  is set in options. The total ENERGY in the spread is preserved — only the
  per-slot rate changes — so this doesn't change LP behaviour, only its
  appearance in the schedule.

- **Python module cache prevents in-place code reload.** With `link_dev.sh`
  symlinking the dev tree into HA, editing a `.py` file does NOT make the
  running HA pick up the change. `homeassistant.reload_config_entry` does
  NOT reimport modules; `homeassistant.reload_all` reloads YAML only.
  **You need `homeassistant.restart`** (or restart the docker container).

## Validation Checklist (after deploy)

Deploy:
```bash
cp -r custom_components/power_sync \
   /home/engineer/docker/home-assistant/data/config/custom_components/
# Restart HA or reload the integration
```

Then `grep` the HA log for:

1. **Feature 1 — Inhibit Exports active:**
   ```
   Automation export segments: 'Force Discharge 5.0kw' 18:00–20:00 @ 5.0kW, ...
   Automation power pin: 72/576 slots in automation windows set to export
   ```
   And the optimizer action distribution should show export slots only inside
   automation windows (no spread).

2. **Feature 2 — SoC ceiling active:**
   When a free window exists and `profit_max` is off, the Schedule API SOC
   range should NOT touch 100% during the free window. If it does, the
   ceiling isn't capping. Verify by computing
   `backup_reserve + (load-solar)*hours/cap` for the period between the
   free window and the next one — that's the expected ceiling.

3. **Feature 3 — `profit_max` provider-agnostic:**
   ```
   Pre-window SOC floor: target=X.X% (capped from Y.Y%) at slot N (Z h ahead), current=W.W%
   ```
   If this line is **absent** with `profit_max` enabled, the provider gate
   has regressed (check that `_next_profit_max_target_slot` still works for
   your provider).

4. **Feature 4 — `profit_max` ceiling lifted with downstream demand:**
   ```
   Free-window profit-max ceiling: window_period=[A,B] (base slots S-E), target=X.X%, downstream_demand_to_target=Y.YY kWh, lifted_ceiling=Z.Z%
   ```
   `lifted_ceiling` should be >= `target` and may be up to 100%.
   If you see paid charging happening between a free window and the
   `pre_window_slot` deadline (check the schedule API for `grid_import_w`
   spikes during paid hours before the deadline), the lift isn't covering
   downstream demand correctly.

## Key Files Reference

```
custom_components/power_sync/
  optimization/
    battery_optimizer.py   # LP solver core — _solve_lp_inner, _energy_ceiling
    coordinator.py         # Scheduling loop, automation helpers, profit_max plumbing
  config_flow.py           # HA options flow — CONF_FACTOR_AUTOMATION_EXPORTS
  const.py                 # CONF_* constants
  strings.json             # i18n
  translations/en.json     # i18n

tests/
  test_battery_optimizer_free_window.py   # Feature 2 — scipy-skipped
  test_battery_export_allowed_slots.py    # Feature 1 + Feature 3 tests
```

## Testing

**`scipy` IS installed** (v1.17.1) via:
```bash
pip3 install --break-system-packages scipy
```
Without scipy, `pytest.importorskip("scipy")` tests are silently skipped —
the 4 LP-behaviour tests in `tests/test_battery_optimizer_free_window.py`
plus most tests in `tests/test_battery_optimizer_export_guard.py`. **Keep
scipy installed** so we catch LP regressions before deploy.

The full suite (~700 tests on v2.12.473) runs in <30s:

```bash
python3 -m pytest tests/ -q
```

Expected pre-existing dev-env errors (unrelated to our work):
- `tests/test_sigenergy_tariff_conversion.py` — 6 errors from missing
  `cryptography` module. Fix if you care:
  `pip3 install --break-system-packages cryptography`.

Real LP behaviour for novel scenarios can only be verified on HA (with the
live forecast/price inputs). The unit tests cover the LP's response to
shape inputs (free window position, demand pattern, etc); the integration
test is the deploy + log-line check.

## Deployment

No CI pipeline. Manual copy + restart. Logs at
`/home/engineer/docker/home-assistant/data/config/home-assistant.log`.

## Recovery

If a rebase produces a broken result:
- The previous version branch (e.g. `powersync-custom-v2.12.466`) still
  exists locally. Check it out and redeploy from there.
- The 3 feature commits are stable git objects — even if their containing
  branch is deleted, `git reflog` will surface them for a while.
