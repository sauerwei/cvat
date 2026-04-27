# Fork Automation Setup

This setup gives you:
- Automatic upstream sync PRs from `cvat-ai/cvat:develop`
- Automatic deploy on `develop` when relevant files changed
- Automatic deploy when enough commits accumulated
- Automatic rollback to previous commit on deploy failure

## Workflows

- `.github/workflows/fork-upstream-sync.yml`
- `.github/workflows/fork-auto-deploy.yml`

## Required GitHub Secrets

No deploy secrets are required when using a self-hosted runner on the target VM.

## Required GitHub Variables

Add these repository variables in your fork:

- `DEPLOY_COMMIT_THRESHOLD` - number of commits required for threshold deploy (default: `5`)
- `DEPLOY_HEALTHCHECK_URL` - healthcheck endpoint used after deploy (default: `http://localhost:8080/api/server/about`)
- `DEPLOY_RUNS_ON` - JSON array of self-hosted runner labels (default: `["self-hosted"]`)

## Recommended Branch Protection

For your fork `develop` branch:
- Require pull request before merge
- Require status checks to pass
- Enable auto-merge for the upstream sync PR if desired

## How It Works

1. Every 6 hours, `fork-upstream-sync.yml` force-updates branch `bot/upstream-sync` from upstream.
2. The workflow creates or updates a PR into your fork `develop`.
3. After merge into `develop`, `fork-auto-deploy.yml` runs.
4. Deploy happens if:
   - relevant paths changed, or
   - commit threshold is reached, or
   - workflow is manually triggered with `force_deploy=true`.
5. If deploy or healthcheck fails, workflow rolls back to the previous commit and restarts services from that state.
6. On success, the workflow moves tag `deploy/last` to mark deploy state.

## First-Time Bootstrap

1. Register a self-hosted GitHub Actions runner on your VM.
2. Ensure Docker and Docker Compose are available to the runner user.
3. Run one manual deploy from GitHub Actions (`Fork Auto Deploy` with `force_deploy=true`).
4. Verify services with `docker compose ps` on the VM.

## Notes

- The deploy workflow uses `docker compose -f docker-compose.yml -f docker-compose.dev.yml`.
- Adjust compose files in the workflow if your VM uses a different stack.
