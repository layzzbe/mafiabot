# Mafia Bot Development Rules

## Branch and Git
- Work only on the laziz-dev branch.
- Never modify or merge main directly.
- Never commit, push, create a pull request, or change Git remotes unless explicitly requested.
- Before every task, verify the current branch and working tree status.

## Scope
- Modify only files explicitly required by the current task.
- Do not refactor unrelated code.
- Do not rename unrelated variables, functions, files, or folders.
- Do not reformat entire files when changing a small section.
- Preserve the existing architecture and coding style.

## Workflow
- For implementation tasks, first inspect only the relevant files.
- Present a concise implementation plan before making changes when the task is risky or affects multiple modules.
- Complete one small feature or fix at a time.
- Do not pause for approval after each individual file edit.
- Never leave a partial implementation in the working tree.

## Dependencies and Database
- Do not add, remove, or upgrade dependencies unless explicitly approved.
- Do not modify pyproject.toml, package.json, lock files, migrations, or database schemas unless the task requires it and approval is given.
- Never run destructive database commands.
- Use SQLite and MemoryStorage for local development unless instructed otherwise.

## Testing
- Run the smallest relevant tests first.
- Do not run the entire test suite unless requested or necessary.
- If a test fails, report the exact failure before making unrelated fixes.
- Do not hide failures or weaken tests merely to make them pass.

## Security
- Never display, log, commit, or expose BOT_TOKEN, passwords, secret keys, Telegram IDs, or environment-file contents.
- Never modify .env.local.
- Treat Telegram Stars, diamonds, dollars, inventory, premium, and rewards as financial operations requiring extra caution.

## Completion Report
At the end of every implementation task, report:
1. Files changed.
2. What was implemented.
3. Tests or checks run.
4. Remaining risks or limitations.
5. Git status.
