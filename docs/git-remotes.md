# Git remote push aliases (per-repo)

This repo defines git aliases in local repository config (`.git/config`) for pushing to all configured remotes:

- `git pushall <branch>`: pushes to `all`, `lab`, `personal` and continues even if one remote fails.
- `git pushall-fast <branch>`: pushes to `all`, `lab`, `personal` and stops on the first failure.

Example:

```sh
git pushall master
```

Notes:

- These aliases are local to this repository (`.git/config`), not global.
- Run in repo root after cloning.
- They use `--recurse-submodules=on-demand` so submodule updates are handled correctly.

If you prefer a single `all` remote push target, you can use:

```sh
git push all <branch>
```
