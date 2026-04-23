# Migrator Repository — Location

## Current state
The `migrator` project is located at `~/migrator/` (home directory).

## Planned location
Target: `~/Documents/GitHub/migrator/` — consistent with the GitHub projects convention.

## How to move (when ready)

```bash
# Move the repo
mv ~/migrator ~/Documents/GitHub/migrator

# Update your shell aliases or scripts if any reference ~/migrator
# Update .env in mlOps:
#   MIGRATOR_REPO_PATH=/Users/wlerouli/Documents/GitHub/migrator

# Verify git remote is still intact
git -C ~/Documents/GitHub/migrator remote -v
```

## Config to update after move
- `.env`: `MIGRATOR_REPO_PATH`
- `config/projects/migrator.yaml`: `data.raw_source`
- `CLAUDE.md`: paths section

## Read-only policy
Regardless of location, the `migrator` repo is **read-only** for the mlOps pipeline.
The `.claude/settings.json` in `migrator` enforces this at the CC level.
