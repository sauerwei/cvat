Troubleshooting: PR creation failures from Actions
===============================================

Symptom
-------
Errors such as:

  GraphQL: Resource not accessible by integration (createPullRequest)

or

  pull request create failed: GraphQL: Resource not accessible by integration

Cause
-----
- The token used by the workflow
  (GitHub App installation token or `GITHUB_TOKEN`)
  does not have permission to create pull requests
  in the target repository. This commonly happens
  when a workflow runs from a fork or when the
  GitHub App is not installed on the target repo
  or lacks `pull_requests: write` permission.

Fixes
-----
1. Preferred: Install the GitHub App
   (if workflows use an app token) on
   the target repository and grant it
   `Pull requests: Read & Write` and
   `Contents: Read & Write`
   repository permissions.

2. Add job-level permissions in the workflow (when using `GITHUB_TOKEN`):

```yaml
permissions:
  contents: write
  pull-requests: write
```

3. If workflows run from forks and need
   to create PRs in the upstream, use a
   Personal Access Token (PAT) stored as
   a repository secret (e.g. `GH_PAT`)
   with `repo` scope, then pass it to
   commands that create PRs:

```yaml
env:
  GH_TOKEN: ${{ secrets.GH_PAT }}
run: |
  gh pr create --title "..." --body "..."
```

For this repository's fork sync workflow,
you can use the secret `SYNC_FORK_TOKEN`.
The workflow will prefer it and fall back
to `github.token`:

```yaml
env:
  GH_TOKEN: ${{ secrets.SYNC_FORK_TOKEN || github.token }}
```

4. Check the `create-github-app-token`
   usage: ensure the App ID and private
   key secrets are correct and that the
   App installation includes the target
   repository with required permissions.

Verification
------------
- Run the workflow again (from a non-fork branch if possible) and confirm `gh pr create` succeeds.
- Alternatively, test locally with the same token and `gh` CLI.

If you want, I can:
- Add `permissions:` to additional workflows, or
- Update a specific workflow to use `secrets.GH_PAT` and show a PR creation example.
