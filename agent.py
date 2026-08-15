"""Autonomous coding agent.

Pipeline for every request: analyze requirements -> design -> implement ->
self code review -> structure check -> security review -> local git commit
-> (pause for human confirmation before any push/publish) -> iterate.

Usage:
    python agent.py "build a CLI todo app in Python"
    python agent.py                      # prompts for the goal interactively

Requires:
    ANTHROPIC_API_KEY set in the environment (or an `ant auth login` profile).
"""

import asyncio
import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
)
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

WORKSPACE = Path(__file__).parent / "workspace"

SYSTEM_PROMPT = """You are an autonomous software engineering agent. For every request, follow this pipeline:

1. Requirement analysis: restate the goal, scope, and assumptions in your own words before writing code.
2. Architecture / framework design: decide the project structure, key modules, and technology choices. Keep it as simple as the task allows.
3. Implementation: write the code, following the design.
4. Self code review: read back what you wrote; look for bugs, unnecessary complexity, and missed edge cases; fix what you find.
5. Structure check: verify the project layout is organized (clear module boundaries, no stray files, sensible naming).
6. Security review: inspect the code you just wrote for common vulnerabilities (injection, unsafe deserialization, hardcoded secrets, path traversal, insecure defaults). Run static analysis tools via Bash if available (e.g. bandit for Python, npm audit for Node). This review is limited to the code inside this workspace -- never scan, probe, or attempt to exploit any external host, network, or third-party service.
7. Version control: once you have a working increment, stage and commit it locally with a clear message (`git init` once if needed, then `git add` + `git commit`). Do this automatically -- you do not need to ask before committing locally.
8. Never publish anything remotely on your own initiative. Commands like `git push`, `gh pr create`, `gh repo create`, or anything that publishes to a remote/public destination will be paused by the harness for a human to confirm -- just run them normally when you believe the work is ready to share, and the confirmation happens outside your control.
9. After finishing an iteration, summarize what changed and suggest the next improvement, then stop and wait for further instructions.

If requirements are ambiguous, state a reasonable assumption and proceed -- do not stall waiting for clarification unless the ambiguity is safety-critical.
"""

DANGEROUS_PATTERNS = [
    "rm -rf /",
    "rm -rf ~",
    "rm -rf .",
    "mkfs",
    ":(){:|:&};:",
    "dd if=",
    "> /dev/sd",
    "shutdown",
    "reboot",
    "del /f /s /q c:",
]

REMOTE_ACTION_PATTERNS = [
    "git push",
    "gh pr create",
    "gh repo create",
    "gh release",
    "npm publish",
    "pip upload",
    "twine upload",
    "git remote add",
]


async def confirm(prompt: str) -> bool:
    loop = asyncio.get_event_loop()
    answer = await loop.run_in_executor(None, input, prompt)
    return answer.strip().lower() in ("y", "yes")


async def bash_gate(
    tool_name: str,
    input_data: dict,
    context: ToolPermissionContext,
) -> PermissionResultAllow | PermissionResultDeny:
    if tool_name != "Bash":
        return PermissionResultAllow(updated_input=input_data)

    cmd = input_data.get("command", "")
    lower = cmd.lower()

    if any(p in lower for p in DANGEROUS_PATTERNS):
        return PermissionResultDeny(
            message=f"Blocked potentially destructive command: {cmd}"
        )

    if any(p in lower for p in REMOTE_ACTION_PATTERNS):
        print(f"\n[confirm] The agent wants to run:\n  {cmd}\n")
        if await confirm("Allow this remote-affecting command? [y/N]: "):
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(message="User declined to run this command.")

    return PermissionResultAllow(updated_input=input_data)


def render(message) -> None:
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock):
                print(block.text)
            else:
                name = getattr(block, "tool_name", None) or getattr(block, "name", "")
                print(f"[{type(block).__name__}] {name}")


async def main() -> None:
    WORKSPACE.mkdir(exist_ok=True)

    goal = " ".join(sys.argv[1:]).strip()
    if not goal:
        goal = input("What should the agent build? ").strip()
    if not goal:
        print("No goal given, exiting.")
        return

    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        cwd=str(WORKSPACE),
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
        permission_mode="default",
        can_use_tool=bash_gate,
    )

    async with ClaudeSDKClient(options=options) as client:
        prompt = goal
        while True:
            await client.query(prompt)
            async for message in client.receive_response():
                render(message)

            next_instruction = input(
                "\nContinue iterating? Enter the next instruction, or press Enter to quit: "
            ).strip()
            if not next_instruction or next_instruction.lower() in ("q", "quit", "exit"):
                break
            prompt = next_instruction


if __name__ == "__main__":
    asyncio.run(main())
