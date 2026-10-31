# Autonomous Coding Agent

[![tests](https://github.com/weilun9/autonomous-coding-agent/actions/workflows/test.yml/badge.svg)](https://github.com/weilun9/autonomous-coding-agent/actions/workflows/test.yml)

A small standalone coding agent built on the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk) (Python).
You give it a goal ("build a CLI todo app with tests"); it works inside its own `workspace/` folder, commits locally,
and **stops to ask you in the terminal before anything that would publish or reach a remote host** (`git push`,
`gh pr create`, `npm publish`, `scp`, ...). After each iteration it summarises what changed and waits for your next
instruction.

- [`agent.py`](agent.py): the agent loop and SDK wiring (~150 lines)
- [`gate.py`](gate.py): the permission gate, plain Python with no SDK import, so it can be tested on its own
- [`tests/`](tests): 74 table-driven command cases + file-tool and wiring tests, run in CI on Linux and Windows

## How a run works

```
goal ─► Claude (system prompt: 9-step pipeline) ─► tool call ─► PreToolUse hook ─► gate.check()
                                                                     │
                       allow ◄──────────────────────────────────────┤
                       deny  ◄── reason sent back to the model ─────┤
                       ask   ─► can_use_tool ─► gate.check() again ─► "Allow this? [y/N]" in the terminal
```

The system prompt asks the model to follow, every time: requirement analysis → design → implementation →
self code review → structure check → security review of the workspace code only → local `git commit` →
never publish on its own → summarise and wait.

## The permission gate

Every tool call goes through `gate.check()`; nothing can skip it:

| Layer | How |
|---|---|
| Only six tools exist: `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash` | `tools=` in `ClaudeAgentOptions`; the gate also denies any other tool name (web, subagents, MCP) |
| Nothing is pre-approved | `allowed_tools=[]`; `setting_sources=[]` so allow rules in `~/.claude` or project settings cannot bypass the gate |
| The gate runs on every call | a `PreToolUse` hook with no matcher (the SDK skips `can_use_tool` for pre-approved calls, a hook does not) |
| File tools stay inside `workspace/` | paths are resolved (`..`, `~`, symlinks, Git Bash `/c/...`) and must land inside the workspace; Glob patterns are checked by their fixed prefix |
| Bash commands are parsed, not substring-matched | `shlex` tokens, split into simple commands on `; && \|\| \| & ( )` and newlines; `$( )`, backticks and `bash -c "..."` are checked recursively; heredoc bodies are treated as data; `cd` is tracked |

What happens to a Bash command:

| Result | Examples |
|---|---|
| **deny** | `sudo`; `mkfs`, `dd of=/dev/...`, `shutdown`; `rm`/`mv`/`cp`/`chmod`/redirects that touch anything outside the workspace (or delete the workspace itself); `curl ... \| sh`; `eval`; `xargs rm`; `find / -delete`; `git config --global`; fork bombs; commands that cannot be parsed |
| **ask a person** | `git push` (in any form: `git -C . push`, `cd x && git push`, `$(git push)`), `git remote add`; `gh` commands that change something (`pr create`, `repo create`, `release`, `api -X POST`); `npm`/`twine`/`poetry`/`cargo publish`, `docker push`; `ssh`, `scp`, remote `rsync`; `curl`/`wget` uploads; global installs; commands in another shell's syntax (`powershell -Command`) |
| **allow** | everything else: building, testing, linting, local git (`add`, `commit`, `log`, `status`), reading GitHub with `gh ... list/view`, downloads into the workspace |

Quoted text is data: `git commit -m "fix push bug"` or `echo 'rm -rf /' > notes.txt` is allowed.

**Compared with the first version** (substring matching on the raw command, file tools auto-approved): on the same
74-command test table it classified 44 correctly and let 27 risky commands through, including `git -C . push`,
`docker push`, `scp` and `gh api -X POST`; it also never checked `Write`/`Edit` paths at all. The table was written
for this change, so treat it as a regression suite rather than proof that the gate is complete.

## Run

```bash
python -m venv .venv
.venv\Scripts\activate             # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python agent.py "build a CLI todo app in Python with tests"
```

Authentication, either:
- **your own Claude login:** log in once with `claude` (Claude Code CLI) and leave `ANTHROPIC_API_KEY` unset. The run is
  billed to that account; whether it uses plan limits or prepaid credits depends on the plan, and an account without
  either stops at the first call with "Credit balance is too low". For personal use only: Anthropic does not allow
  offering Claude.ai login to other people through an app built on the Agent SDK;
- **an API key:** `set ANTHROPIC_API_KEY=sk-ant-...` (or copy `.env.example` to `.env` and load it); billed per token.
  If both are present, the API key wins.

Run it with no argument and it asks for the goal. After each iteration, type a follow-up instruction, or press
Enter to stop. To let it push or open PRs once you approve, log in first with `gh auth login`.

`workspace/` is ignored by this repo: it becomes its own git repository the first time the agent runs `git init`.

Tests (no API key needed): `python -m unittest discover -s tests -v`

## Known limits

- **The gate checks commands, not what programs do once they run.** `bash script.sh`, `python -c "..."`, `make`,
  `npm run` or a test suite can do anything the user account can. Real isolation needs an OS-level sandbox
  (container, separate user, no credentials in the environment); the gate is a guard rail, not a sandbox.
- **Reads inside the workspace are not limited,** and Bash commands such as `cat` are not path-checked (only
  commands that write or delete are). The file tools themselves cannot read outside the workspace.
- **The pipeline is a prompt, not a state machine.** Nothing verifies that the review steps actually ran.
- **No recorded example runs** in this repo yet; the tests cover the gate and the wiring, not the model's behaviour.
