# HEO II deploy scripts

Push master (or any branch) to Home Assistant, with atomic swap and rollback.

## Files

- **deploy.sh** — shell script that runs on HA (Alpine busybox). Fetches
  a GitHub tarball, extracts, secret-scans, swaps into place atomically,
  records provenance (`.deployed_sha`, `.deployed_ref`, `.deployed_at`).
  Backups go to `/config/heo2_backups/heo2.TIMESTAMP` — explicitly outside
  `/config/custom_components/` so HA's integration loader doesn't try to
  import them as Python modules.
- **deploy-to-ha.ps1** — PowerShell wrapper from Archer. Uploads deploy.sh
  via scp, runs it via ssh, then reloads the integration via Core API.
- **rollback.sh** — restores the most recent backup from `/config/heo2_backups/`.
- **rollback-ha.ps1** — PowerShell wrapper for rollback, with optional
  reload or full HA restart.

## Usage from Archer (PowerShell)

Deploy master:

```powershell
.\scripts\deploy-to-ha.ps1
```

Deploy a specific branch for live testing (use sparingly, and only when
HEO II is disabled in HA — HACS polling + manual deploy can race):

```powershell
.\scripts\deploy-to-ha.ps1 -Ref fix/some-branch
```

Deploy without reloading (for when HEO II is disabled):

```powershell
.\scripts\deploy-to-ha.ps1 -SkipReload
```

Rollback to the previous version:

```powershell
.\scripts\rollback-ha.ps1 -Restart
```

(Restart is usually needed after rollback because HA caches Python modules
across config-entry reloads for custom integrations.)

## Requirements

- SSH key auth to `root@homeassistant2.local:2222` (HA SSH add-on).
- Long-lived HA access token at `$env:USERPROFILE\.heo2\token`
  (only needed for reload/restart steps, not for the bare deploy).
- `ssh` and `scp` on PATH (OpenSSH for Windows or Git Bash).

## Why this exists

The HA integration loader scans every directory under
`/config/custom_components/` as a potential Python module. Naming a
backup `heo2.bak.TIMESTAMP` causes the loader to try to `import
custom_components.heo2.bak` on startup, which fails and cascades into
HEO II itself refusing to load. The scripts here put backups outside
that scan path so backup artefacts can't brick the integration.

The secret-scan in deploy.sh is belt-and-braces against accidental
commits of real credentials. It looks for value SHAPES (UUIDs, common
API-key prefixes) rather than names, so generic-looking variables don't
false-positive.

## Notes on HACS

HACS tracking the same repo creates a race: HACS can overwrite a manual
deploy at its own polling cadence. Recommended setup is to either:

1. Remove the repo from HACS entirely (manual deploy is authoritative), or
2. Publish tagged releases and let HACS handle updates via releases only.

Don't run both HACS auto-update AND manual deploy on the same install.
