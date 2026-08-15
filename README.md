# Autonomous Coding Agent

A standalone agent (Python, [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk)) that
runs the full loop: analyze requirements -> design -> implement -> self code review ->
structure check -> security review -> local git commit -> iterate. All generated code lives
under `workspace/`, which becomes its own git repository.

Local file edits and commits happen automatically. Anything that publishes remotely
(`git push`, `gh pr create`, `gh repo create`, `npm publish`, ...) pauses and asks for
confirmation in the terminal before running.

## Setup

1. Dependencies are already installed in `.venv` (`pip install -r requirements.txt`).
2. Set your API key for this shell session:
   ```
   set ANTHROPIC_API_KEY=sk-ant-...
   ```
   or copy `.env.example` to `.env` and load it, or run `ant auth login` once and skip
   the env var entirely.
3. If you want the agent to push branches or open PRs, log in to GitHub first:
   ```
   gh auth login
   ```

## Run

```
.venv\Scripts\python agent.py "build a CLI todo app in Python with tests"
```

Or run it with no argument and it will ask for the goal interactively. After each
iteration it asks what to do next -- type a follow-up instruction, or press Enter to stop.

## Notes

- `workspace/` is gitignored from *this* repo -- it's meant to become its own independent
  git repository once the agent runs `git init` inside it.
- The security review step is scoped to the code inside `workspace/` only. It will not
  scan, probe, or attempt to exploit any external host or third-party service.
