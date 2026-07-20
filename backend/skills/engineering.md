# Engineering

## Git — prefer SSH for cloning

```
git@github.com:{org}/{repo}.git      ← preferred. SSH key handles auth, no token.
```

Default clone destination: `~/.friday/repos/{repo}/` (unless the user gives a path).
"in the atlas repo" without a path → look in `~/.friday/repos/` for a matching directory.

Branch naming (you pick the name when the user doesn't):
- Prefixes: `feature/` `fix/` `hotfix/` `chore/` `docs/` `test/` `refactor/` `wip/`
- kebab-case, 3-5 words max, include ticket id if mentioned: `feature/TKT-1234-payment-retry`
- "branch for fixing the refund bug" → `fix/refund-validation`

Conventions:
- Run git via bash (silent). `git push` triggers the confirmation gate — expected.
- Conventional commits: `feat: …`, `fix: …`, `chore: …`.
- Repo not found? Search before asking:
  `gh repo list {org} --limit 200 --json name --jq '.[].name' | grep -i {partial}`
  (fallback `ls ~/.friday/repos/`), then offer the close matches to the user.

## Project scaffolding

- Write real, complete code — no placeholder comments like "# your logic here".
- Descriptive filenames: `deploy_status_query.py`, not `script.py`.
- New project pattern: mkdir → git init → write files → install deps (confirm first) →
  open the folder in VS Code with open_path (never bash `code`, CLI may be missing from PATH).
- Servers / long-running processes: bash with `background: true`, then verify with
  `curl -s localhost:PORT` or a process check before declaring success.
- Missing tool? `which <cmd>` first, then install via brew/npm/pip after confirmation.
