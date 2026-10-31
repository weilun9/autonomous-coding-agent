"""Autonomous coding agent.

Pipeline for every request: analyze requirements -> design -> implement ->
self code review -> structure check -> security review -> local git commit
-> (pause for human confirmation before any push/publish) -> iterate.

Every tool call passes through gate.check() (see gate.py):
  - only the tools in gate.ALLOWED_TOOLS exist (`tools=`), and no settings file can pre-approve anything
    (`setting_sources=[]`, `allowed_tools=[]`);
  - a PreToolUse hook runs the gate on *every* call and allows, denies, or escalates to "ask";
  - "ask" reaches `can_use_tool`, which asks a person in the terminal.

Usage:
    python agent.py "build a CLI todo app in Python"
    python agent.py                      # prompts for the goal interactively

Requires either:
    a Claude Code login (`claude` CLI, personal use; billed to that account), with ANTHROPIC_API_KEY unset, or
    ANTHROPIC_API_KEY set in the environment (billed per token; wins if both are present).
"""

import asyncio
import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    TextBlock,
)
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

import gate

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

You can only use these tools: Read, Write, Edit, Glob, Grep, Bash. All paths must stay inside the current working directory;
calls outside it are refused. If a tool call is refused, read the reason and adjust instead of retrying the same call.
If requirements are ambiguous, state a reasonable assumption and proceed -- do not stall waiting for clarification unless the ambiguity is safety-critical.
"""


async def confirm(prompt: str) -> bool:
    loop = asyncio.get_event_loop()
    answer = await loop.run_in_executor(None, input, prompt)
    return answer.strip().lower() in ("y", "yes")


async def pre_tool_gate(input_data, tool_use_id, context) -> dict:
    """PreToolUse hook: runs on every tool call, before any permission rule."""
    decision = gate.check(input_data["tool_name"], input_data.get("tool_input") or {}, WORKSPACE)
    if decision.action == "allow":
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
    if decision.action == "deny":
        print(f"\n[blocked] {input_data['tool_name']}: {decision.reason}")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": decision.action,
                                   "permissionDecisionReason": decision.reason}}


async def ask_person(
    tool_name: str,
    input_data: dict,
    context: ToolPermissionContext,
) -> PermissionResultAllow | PermissionResultDeny:
    """Reached only for calls the gate marked "ask". The gate runs again first as a second check."""
    decision = gate.check(tool_name, input_data, WORKSPACE)
    if decision.action == "deny":
        return PermissionResultDeny(message=decision.reason)
    if decision.action == "ask":
        detail = input_data.get("command") or input_data
        print(f"\n[confirm] {decision.reason}\n  {detail}\n")
        if not await confirm("Allow this? [y/N]: "):
            return PermissionResultDeny(message="A person declined this action.")
    return PermissionResultAllow(updated_input=input_data)


def render(message) -> None:
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock):
                print(block.text)
            else:
                name = getattr(block, "tool_name", None) or getattr(block, "name", "")
                print(f"[{type(block).__name__}] {name}")


def build_options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        cwd=str(WORKSPACE),
        tools=list(gate.ALLOWED_TOOLS),      # the only tools that exist in this session
        allowed_tools=[],                    # nothing is pre-approved; every call goes through the gate
        setting_sources=[],                  # ignore ~/.claude and project settings (their allow rules would bypass the gate)
        permission_mode="default",
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_gate])]},
        can_use_tool=ask_person,
    )


async def main() -> None:
    WORKSPACE.mkdir(exist_ok=True)

    goal = " ".join(sys.argv[1:]).strip()
    if not goal:
        goal = input("What should the agent build? ").strip()
    if not goal:
        print("No goal given, exiting.")
        return

    async with ClaudeSDKClient(options=build_options()) as client:
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
