# Fix: Duplicate/Triplicate Modbus Writes on Optimizer Force Charge/Discharge

**Branch:** `foxEssLogging`
**File:** `custom_components/power_sync/optimization/coordinator.py`

---

## Symptom

On each optimizer decision to force charge or discharge, the inverter received 2–3 identical Modbus write sequences within a few seconds of each other. Observed in logs as repeated `_write_remote_control` calls to registers 46001/46002/46003 within the same ~4-second window:

```
11:22:25.680  Force charge 10min at 11100W       ← execute 1
11:22:25.861  Force charge 10min at 11100W       ← execute 2 (183ms later)
11:22:29.534  Force charge 10min at 11100W       ← execute 3 (polling heartbeat)
```

---

## Root Causes

### 1. Concurrent LP Solves → Duplicate Executes

`_run_optimization()` was called from two independent periodic sources that shared the same 5-minute interval:

- **`_schedule_polling_loop`** — background task that calls `_run_optimization()` at the bottom of each loop iteration
- **`_async_update_data`** — the `DataUpdateCoordinator` base-class method, which also called `_run_optimization()` on its own 5-minute `UPDATE_INTERVAL`

Both fired at approximately the same time. Because `_run_optimization()` offloads the LP solve to an executor thread (`async_add_executor_job`), two LP solves ran concurrently on separate worker threads. Both completed within ~180ms of each other. Each one then called `_execute_optimizer_action()` on the main thread, producing **executes 1 and 2**.

At the time the second execute ran, the first's Modbus writes hadn't completed yet (they take ~1.4s), so the force state was still `inactive`. This meant execute 2 didn't recognise an already-active force charge — it issued a full new command rather than an extension.

### 2. Structural Double in the Polling Loop

The polling loop had this structure:

```
iteration N:
    execute (heartbeat)          ← EXECUTE N
    sleep 5 min
    _run_optimization()          ← LP solve → execute inside  ← EXECUTE N+1
← loop back
iteration N+1:
    execute (heartbeat)          ← EXECUTE N+2  ← immediate duplicate of N+1!
    sleep 5 min
    ...
```

After `_run_optimization()` completed (with its own embedded execute), the loop returned to the top and immediately fired the heartbeat execute again — producing **execute 3** ~4 seconds after executes 1 and 2 (once the blocking Modbus service calls from those finished).

---

## Fixes

### Fix 1 — Lock on `_run_optimization()` (prevents concurrent LP solves)

Added `self._optimization_lock: asyncio.Lock` to `__init__`. At the top of `_run_optimization()`, if the lock is already held, the call is skipped immediately:

```python
if self._optimization_lock.locked():
    _LOGGER.debug("Optimization already in progress — skipping concurrent request")
    return

await self._optimization_lock.acquire()
try:
    ...
except Exception as e:
    _LOGGER.error(...)
finally:
    self._optimization_lock.release()
```

The `locked()` check and `acquire()` have no `await` between them, so there is no race condition on the single-threaded asyncio event loop. This also protects against price-update-triggered re-optimisations (`_on_price_update`) overlapping with the polling loop.

### Fix 2 — Polling loop restructured to sleep-first (eliminates structural double)

The heartbeat execute block was removed from the top of `_schedule_polling_loop`. The loop now sleeps first, then re-optimises. Since `_run_optimization()` already executes the action immediately after the LP solve, there is no need for a separate execute at the top of the next iteration:

```
# Before
execute (heartbeat) → sleep → _run_optimization (executes) → loop back → execute (duplicate)

# After
sleep → _run_optimization (executes) → loop back → sleep → _run_optimization (executes)
```

The FoxESS hardware timer is not affected: it is extended to `interval_minutes + 5` minutes (10 min) on each execute, and the LP runs every `interval_minutes` (5 min), so the hardware timer is always renewed well before expiry.

### Fix 3 — Removed `_run_optimization()` from `_async_update_data()` (removes redundant LP trigger)

`_async_update_data()` now simply returns `get_api_data()`. LP optimisation is driven exclusively by `_schedule_polling_loop` (every 5 min) and `_initial_opt_task` (on startup). The `DataUpdateCoordinator` still fires its 5-min refresh cycle, keeping HA sensors up to date with the latest cached data — just without re-running the LP.

---

## Result

One Modbus write sequence per LP solve cycle, regardless of how many concurrent triggers fire. The three independent sources that previously could produce concurrent executes (`_async_update_data`, `_schedule_polling_loop`, `_on_price_update`) are now all guarded by the same lock.
