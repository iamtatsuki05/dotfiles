from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from agent_team import mcp_protocol


class TaskMcpTest(unittest.TestCase):
    def test_raw_duplicate_task_key_cannot_reach_dispatch(self) -> None:
        request = '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"task_dispatch","arguments":{"role":"worker","task":{"task_id":"a","task_id":"b"},"message":"work"}}}\n'
        execute = mock.Mock(return_value={})
        output = io.StringIO()
        with (
            mock.patch.object(mcp_protocol.sys, "stdin", io.StringIO(request)),
            mock.patch.object(mcp_protocol.sys, "stdout", output),
        ):
            self.assertEqual(mcp_protocol.serve(execute), 0)
        execute.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], -32700)


if __name__ == "__main__":
    unittest.main()
