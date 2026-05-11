# Changelog

Session history for local development work on PowerSync. Upstream release notes live in RELEASE_NOTES.md.

---

## [2026-05-11] Session: 06:32

### Summary
Set up a local development workflow so file edits in the repo are immediately reflected in the running Home Assistant instance (no manual copying). Also identified and fixed a long-standing incorrect Modbus register for `min_soc` on FoxESS H3-Pro and H3-Smart.

### Changes
- **Local dev symlink** (`scripts/link_dev.sh`): replaces the HACS-installed `custom_components/power_sync` directory with a symlink to the local repo. Run after a HACS update to restore the link.
- **Docker volume mount** (`/home/engineer/docker/home-assistant/docker-compose.yml`): added the PowerSync source path as a second volume so the HA container can follow the symlink.
- **min_soc register fix** (`custom_components/power_sync/inverters/foxess.py`): corrected register from `46609` (read-only Backup Cut-off SoC, not writable under firmware 1.39/1.24) to `46611` (writable min SoC) for both `H3_PRO` and `H3_SMART`.
- **Branch cleanup**: deleted 5 merged/stale local branches (`fix/foxess-h3-smart-battery-voltage-scaling`, `fix/ha-startup-delay-tesla-capability-task`, `foxESSHealth`, `foxEssTest2`, `nonGreedyOptimizer`). Kept `foxEssLogging`.

### Git Commits
- `23683d1f` — Add dev symlink script for local HA development (main)
- `f02cf096` — fix(foxess): correct min_soc register for H3-Pro and H3-Smart (foxEss-min-soc)

### PRs
- [#81](https://github.com/bolagnaise/PowerSync/pull/81) — fix(foxess): correct min_soc register for H3-Pro and H3-Smart. Tested on H3-Smart (fw 1.39); H3-Pro untested.

### Next Steps
- [ ] Await merge of PR #81
- [ ] Continue work on `foxEssLogging` branch

---
