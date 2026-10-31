"""The agent is wired so that every tool call reaches the gate. Skipped when claude-agent-sdk is not installed."""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import agent
except ImportError:                                          # pragma: no cover
    agent = None

import gate  # noqa: E402


@unittest.skipIf(agent is None, "claude-agent-sdk not installed")
class AgentWiringTests(unittest.TestCase):
    def test_nothing_is_pre_approved_and_settings_are_ignored(self):
        o = agent.build_options()
        self.assertEqual(o.tools, list(gate.ALLOWED_TOOLS))   # the only tools that exist
        self.assertEqual(o.allowed_tools, [])                 # nothing skips the gate
        self.assertEqual(o.setting_sources, [])               # no allow rules from ~/.claude or the project
        self.assertNotEqual(o.permission_mode, "bypassPermissions")
        self.assertIs(o.hooks["PreToolUse"][0].hooks[0], agent.pre_tool_gate)
        self.assertIsNone(o.hooks["PreToolUse"][0].matcher)   # the hook sees every tool
        self.assertIs(o.can_use_tool, agent.ask_person)

    def test_hook_maps_gate_decisions(self):
        def run(tool, data):
            out = asyncio.run(agent.pre_tool_gate({"tool_name": tool, "tool_input": data}, "id", {}))
            return out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(run("Bash", {"command": "pytest -q"}), "allow")
        self.assertEqual(run("Bash", {"command": "git -C . push"}), "ask")
        self.assertEqual(run("Bash", {"command": "rm -rf /"}), "deny")
        self.assertEqual(run("WebFetch", {"url": "https://example.com"}), "deny")


if __name__ == "__main__":
    unittest.main(verbosity=2)
