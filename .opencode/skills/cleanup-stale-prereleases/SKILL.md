---
name: cleanup-stale-prereleases
description: Use when asked to clean up stale GitHub prereleases, PR prerelease tags, merged PR prereleases, branch prereleases, main prereleases, or outdated /prerelease artifacts in this repository.
---

# Cleanup Stale Prereleases

Use `gh` for all GitHub release and PR operations.

## Workflow

1. List current prereleases:

```bash
gh release list --limit 100 --json tagName,isPrerelease --jq '.[] | select(.isPrerelease) | .tagName'
```

2. List open PRs:

```bash
gh pr list --state open --limit 100 --json number,headRefName,headRefOid,title
```

3. List stable releases:

```bash
gh release list --exclude-pre-releases --limit 100 --json tagName,publishedAt --jq '.[] | [.tagName, .publishedAt] | @tsv'
```

4. For each prerelease tag matching `v*-pr<NUMBER>.<SHA>`:

- If the PR is merged or closed, delete the prerelease and tag.
- If the PR is open but the tag SHA prefix does not match the current PR `headRefOid`, delete the prerelease and tag.
- If the PR is open and the SHA prefix matches, keep it.

5. For each branch prerelease tag matching `v*-<branch>.<SHA>`:

- If a stable release was published after the prerelease, delete the prerelease and tag.
- This includes prereleases generated from `main`.
- If there is no stable release after it, keep it unless there is another clear stale signal.

6. Check release publish dates when needed:

```bash
gh release view <TAG> --json tagName,isPrerelease,publishedAt,targetCommitish
```

7. Check individual PR state when needed:

```bash
gh pr view <NUMBER> --json state,mergedAt,headRefName,headRefOid,title,url
```

8. Delete stale prereleases with tag cleanup:

```bash
gh release delete <TAG> --cleanup-tag --yes
```

9. Confirm remaining prereleases:

```bash
gh release list --limit 100 --json tagName,isPrerelease --jq '.[] | select(.isPrerelease) | .tagName'
```

## Notes

- Do not add workflow automation unless explicitly requested.
- Do not delete stable releases.
- Only delete prereleases that are clearly stale: merged PRs, closed PRs, outdated SHA tags for open PRs, or branch prereleases superseded by a later stable release.
