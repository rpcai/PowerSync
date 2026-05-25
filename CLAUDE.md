# PowerSync — Dev Notes

This file is the playbook for re-applying our local customisations onto fresh
upstream. Upstream moves fast (multiple patch versions per day) and refactors
the LP formulation periodically, so we expect to re-apply these features
regularly. Treat this as the source of truth — keep it in sync with the
actual commit history on the active branch.

## Branch Strategy

We keep one active branch that tracks `origin/main` with our customisations
cherry-picked on top. The branch is named for the upstream version it sits
on, e.g. `powersync-custom-v2.12.466`. When upstream advances:

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

Three independent features. Each is one cherry-picked commit. They can be
applied in any order but the order below matches the chronological intent
and the file overlap is minimal.

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

# 3. Note our 3 feature commits (subject filter avoids upstream noise)
git log --oneline --grep='feat(optimizer)\|fix(optimizer)' | head -10
# Expected: 3 commits, in order:
#   feat(optimizer): inhibit optimizer exports, pin to automation windows
#   feat(optimizer): per-free-window SoC ceiling to prevent greedy charging
#   fix(optimizer): generalise profit_max pre-window slot to all providers

# 4. Save the 3 hashes (oldest first — cherry-pick order matters)
F1=<hash of inhibit-exports commit>
F2=<hash of soc-ceiling commit>
F3=<hash of profit_max-fix commit>

# 5. Create new branch from origin/main
git checkout -b powersync-custom-v${NEW_VERSION} origin/main

# 6. Cherry-pick in order
git cherry-pick $F1
git cherry-pick $F2  # ← conflict likely; see below
git cherry-pick $F3

# 7. Run the test suite
python3 -m pytest tests/ -q
#   Our 3 features → 116 tests pass, 3 LP tests scipy-skipped.
#   Pre-existing dev-env errors (e.g. missing cryptography) are OK.

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

3. **Feature 3 — `profit_max` ceiling lift active:**
   ```
   Pre-window SOC floor: target=X.X% (capped from Y.Y%) at slot N (Z h ahead), current=W.W%
   ```
   If this line is **absent** with `profit_max` enabled, the provider gate
   has regressed (check that `_next_profit_max_target_slot` still works for
   your provider).

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

`scipy` is **not installed** in this dev environment. Tests using
`pytest.importorskip("scipy")` will be skipped (currently 3 free-window LP
tests). The full suite (~670 tests on v2.12.466) runs in <30s:

```bash
python3 -m pytest tests/ -q
```

Pre-existing dev-env errors include `cryptography` missing
(`sigenergy_tariff_conversion`) — unrelated to our work, ignore.

Real LP behaviour can only be verified on HA (which has scipy + the live
forecast/price inputs). The unit tests cover input/output shape and the
helper functions; the integration test is the deploy.

## Deployment

No CI pipeline. Manual copy + restart. Logs at
`/home/engineer/docker/home-assistant/data/config/home-assistant.log`.

## Recovery

If a rebase produces a broken result:
- The previous version branch (e.g. `powersync-custom-v2.12.459`) still
  exists locally. Check it out and redeploy from there.
- The 3 feature commits are stable git objects — even if their containing
  branch is deleted, `git reflog` will surface them for a while.
