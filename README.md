# Autonomous Coding Agent

A small standalone coding agent built on the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk) (Python).
You give it a goal ("build a CLI todo app with tests"); it works inside its own `workspace/` folder, commits locally,
and **stops to ask you in the terminal before anything that would publish remotely** (`git push`, `gh pr create`,
`npm publish`, ...). After each iteration it summarises what changed and waits for your next instruction.

The whole agent is one file, [`agent.py`](agent.py) (~150 lines).

## How a run works

```
goal ─► Claude (system prompt: 9-step pipeline) ─► tool calls ─► permission callback ─► tool runs in workspace/
                                                       ▲                  │
                                                       └──── result ──────┘        ... until the iteration is done
                                                                          │
                                       push / publish command? ─► pause: "Allow this remote-affecting command? [y/N]"
```

The system prompt asks the model to follow, every time:

1. requirement analysis → 2. design → 3. implementation → 4. self code review → 5. structure check →
6. security review of the workspace code only (e.g. `bandit`, `npm audit`; never external hosts) →
7. local `git commit` → 8. never publish on its own initiative → 9. summarise and wait.

## What is enforced in code, and what is only asked for in the prompt

| Control | Where | How |
|---|---|---|
| File tools `Read`, `Write`, `Edit`, `Glob`, `Grep` run without prompting | code | `allowed_tools` in `ClaudeAgentOptions` (auto-approval list) |
| Every `Bash` command is inspected before it runs | code | `bash_gate`, passed as the SDK's `can_use_tool` permission callback |
| Destructive commands (`rm -rf /`, `mkfs`, `dd if=`, fork bomb, `shutdown`, ...) are refused | code | substring denylist in `bash_gate` |
| Push / publish commands (`git push`, `gh pr create`, `gh repo create`, `gh release`, `npm publish`, `twine upload`, `git remote add`) wait for a typed `y` | code | `bash_gate` pauses and asks in the terminal |
| Working directory is `workspace/` | code | `cwd` in `ClaudeAgentOptions` |
| The 9-step pipeline, the scope of the security review, "never publish on your own" | prompt | `SYSTEM_PROMPT` (followed by the model, not checked) |

## Run

```bash
python -m venv .venv
.venv\Scripts\activate             # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
set ANTHROPIC_API_KEY=sk-ant-...   # or copy .env.example to .env
python agent.py "build a CLI todo app in Python with tests"
```

Run it with no argument and it asks for the goal. After each iteration, type a follow-up instruction, or press
Enter to stop. To let it push or open PRs once you approve, log in first with `gh auth login`.

`workspace/` is ignored by this repo: it becomes its own git repository the first time the agent runs `git init`.

## Known limits

Written down on purpose; these are the next things to fix.

- **Not a true tool allow-list.** `allowed_tools` only auto-approves the file tools. Other tools still reach
  `bash_gate`, which currently allows anything that is not `Bash`. A strict version would deny every tool that is
  not on an explicit list.
- **String matching on raw commands can be bypassed.** `git -C . push`, extra spaces, or a script that pushes
  would not match the patterns. Parsing the command (e.g. `shlex`) and checking the `git` subcommand would be stronger.
  The denylist can also over-block (`rm -rf ./build` matches `rm -rf .`).
- **File writes are not confined to `workspace/`.** `cwd` sets the starting folder; `Write` / `Edit` are not
  path-checked.
- **The pipeline is a prompt, not a state machine.** Nothing verifies that the review steps actually ran.
- **No automated tests yet**, and no recorded example runs in this repo.
