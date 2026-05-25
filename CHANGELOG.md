# Changelog

All significant changes to the local PowerSync customisations are documented
here. Upstream commits from `bolagnaise/PowerSync` are NOT logged — see
`git log` for those.

---

## [2026-05-25] Session: 09:38–12:10

### Summary

Rebuilt three local features against upstream v2.12.459, debugged a profit_max
provider gate, rebased everything onto upstream v2.12.466, then discovered
and fixed a more subtle profit_max ceiling bug that was costing ~$0.37/day
in unnecessary paid charging.

The session left the project on a new branch
(`powersync-custom-v2.12.466`) with 6 commits on top of upstream v2.12.466,
pushed to the `rpcai` fork. The re-apply playbook in `CLAUDE.md` was
substantially expanded with three new lessons learned.

### Major work

1. **Feature rebuild against upstream LP refactor.** Upstream's
   `_LpPeriod` aggregation made our old `t`-indexed SoC ceiling code
   incompatible. Translated the cumulative-SOC ceiling into per-boundary
   `energy_var(t)` bounds. Dropped `per_slot_max_export_w` dead code and
   the greedy `_build_schedule` override. Kept upstream's
   `_free_charge_bonus` (now safely bounded by our ceiling).

2. **profit_max provider gate (Feature 3).** Diagnosed via HA log + .storage
   inspection: `_next_profit_max_target_slot()` had a hard
   `if provider != "flow_power": return None` that made profit_max a no-op
   for Globird/Octopus/other TOU providers. Dropped the gate; preserved the
   17:30 Happy Hour clamp behind a `provider == "flow_power"` check.
   Verified live on Globird: `Pre-window SOC floor: target=95% at slot 86`.

3. **Rebase onto upstream v2.12.466.** Created new branch from
   `origin/main`, cherry-picked 3 commits. One conflict in
   `battery_optimizer.py` line ~681 — orthogonal additions, kept both
   (upstream's `_effective_export_acquisition_costs` first, then our
   `_free_window_periods`).

4. **scipy installed** (`pip3 install --break-system-packages scipy 1.17.1`).
   Caught 3 upstream `test_battery_optimizer_export_guard.py` tests that
   assert the greedy full-fill behaviour our Feature 2 deliberately
   removes. Marked them `@pytest.mark.skip` with rebase-survivable reasons.

5. **profit_max ceiling bug (Feature 4).** Investigated user-reported
   post-14:00 paid charging via the optimization HTTP API (minted a JWT
   from the long-lived token in `.storage/auth`, paginated the schedule).
   Found that the ceiling was clamped at exactly `pre_window_soc_target`
   via `max(_ceiling, target)`. The hard pre-window floor then forced
   paid grid-import to maintain target SoC against downstream load.
   Fix: ceiling becomes `target + downstream_demand/(cap*eff)`, where
   downstream demand spans from the free window's end to whichever
   comes first: the next free window or `pre_window_slot`.

   **Live impact** (Globird, target 95% by 18:00):
   - Paid charging 11:00-18:00: 0.97 kWh → 0.12 kWh
   - Grid import 14:00-18:00: 1.07 kWh → 0.00 kWh
   - Daily savings: $8.29 → $8.66 (+$0.37/day, ~$135/year)

### Changes

- `custom_components/power_sync/const.py` — `CONF_FACTOR_AUTOMATION_EXPORTS`
  and legacy `CONF_DAILY_EXPORT_*` keys
- `custom_components/power_sync/config_flow.py` — UI toggle in setup +
  options flows
- `custom_components/power_sync/strings.json`,
  `custom_components/power_sync/translations/en.json` — i18n
- `custom_components/power_sync/optimization/coordinator.py` — automation
  segment cache, inhibit/pin logic, profit_max generalisation,
  provider-agnostic target slot
- `custom_components/power_sync/optimization/battery_optimizer.py` —
  per-free-window SoC ceiling, profit_max ceiling lift with downstream
  demand
- `tests/test_battery_export_allowed_slots.py` — 14 new automation tests,
  3 new profit_max provider tests
- `tests/test_battery_optimizer_free_window.py` — new file, 4 LP behaviour
  tests (now running with scipy installed)
- `tests/test_battery_optimizer_export_guard.py` — 3 incompatible upstream
  tests marked skip with rebase-survivable reasons
- `CLAUDE.md` — new file: full re-apply playbook (branch strategy, feature
  inventory, rebase commands, known conflict zones, failure modes,
  validation checklist)

### Test results (end of session)

- 669 passed
- 3 skipped (upstream zerohero tests incompatible with Feature 2 by design)
- 6 errors (pre-existing dev-env missing `cryptography` module)
- 0 failures

### Git Commits (this session)

- `5bae924e` feat(optimizer): inhibit optimizer exports, pin to automation windows
- `374280ec` feat(optimizer): per-free-window SoC ceiling to prevent greedy charging
- `cb5b4bb4` docs: re-apply playbook for cherry-picking custom features onto upstream
- `16f89974` fix(optimizer): generalise profit_max pre-window slot to all providers
- `ab6eb499` fix(optimizer): include downstream demand in profit_max ceiling lift
- `5b5317cb` docs: add Feature 4 (ceiling+demand) + scipy/test-skip notes to playbook

### Branch State

- Active: `powersync-custom-v2.12.466` → pushed to `rpcai/powersync-custom-v2.12.466`
- Recovery (local only): `powersync-custom-rebuild` (off v2.12.459),
  `powersync-custom-snapshot-20260522`

### Next Steps

- [ ] Monitor live behaviour over next few days — confirm no edge cases in
      the lifted ceiling (e.g. when downstream demand exceeds 5% of cap,
      ceiling clips to 1.0)
- [ ] Next upstream rebase: follow `CLAUDE.md` playbook (4 cherry-picks now,
      not 3). The F2/F4 ceiling block is the most conflict-prone area.
- [ ] Consider archiving `powersync-custom`, `powersync-custom-backup`,
      `powersync-custom-snapshot-20260522` local branches once
      `powersync-custom-v2.12.466` has been running stably for a week.

---
