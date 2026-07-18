# Rollback Cheat Sheet

This project uses Git for local version control. Local secrets are ignored:

- `series_trailer_downloader.settings.json`
- `youtube-cookies.txt`
- `trailer-results.json`

## Save A Good Point

After a change works:

```powershell
git status
git add series_trailer_downloader.py README.md install.ps1 assets .gitignore ROLLBACK.md tests
git commit -m "Describe the working change"
```

## See What Changed

```powershell
git status
git diff
```

## Undo Uncommitted Edits

Undo one file:

```powershell
git restore series_trailer_downloader.py
```

Undo all tracked files back to the last commit:

```powershell
git restore .
```

## Make A Named Checkpoint Before Risky Work

```powershell
git branch before-risky-change
```

Return to that checkpoint:

```powershell
git switch before-risky-change
```

## Restore A Previous Commit

Show commits:

```powershell
git log --oneline
```

Move the project back to a known commit:

```powershell
git switch main
git reset --hard COMMIT_ID
```

Use `reset --hard` only when you intentionally want to discard later tracked edits.
