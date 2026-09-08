from __future__ import annotations

import unittest

from agent_team import mcp_protocol as protocol


class NamedMcpProtocolTest(unittest.TestCase):
    def test_every_role_tool_lists_only_selected_node_ids(self) -> None:
        selected = ("implementation-a", "implementation-b", "review-b")
        result = protocol.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            lambda _name, _arguments: self.fail("listing must not execute a tool"),
            tool_catalog=lambda: protocol.tools(selected),
        )
        self.assertIsNotNone(result)
        role_tools = 0
        for tool in result["result"]["tools"]:
            properties = tool["inputSchema"]["properties"]
            if "role" in properties:
                role_tools += 1
                self.assertEqual(properties["role"]["enum"], list(selected))
        self.assertEqual(role_tools, 6)

    def test_catalog_failure_does_not_advertise_default_roles(self) -> None:
        def unavailable():
            raise ValueError("selected state identity changed")

        result = protocol.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            lambda _name, _arguments: {},
            tool_catalog=unavailable,
        )
        self.assertEqual(result["error"]["code"], -32603)
        self.assertNotIn("result", result)


if __name__ == "__main__":
    unittest.main()
