import tempfile
import unittest
from pathlib import Path

from craft_aris import CraftAris


class CraftArisTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.agent = CraftAris(
            workspace=self.temp.name,
            base_url="https://example.invalid/v1",
            model="test-model",
            api_key="test-key",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_write_and_read_file(self):
        self.assertEqual(
            self.agent.write_file("src/app.js", "console.log('ok');"),
            {"ok": True, "path": "src/app.js"},
        )
        self.assertEqual(
            self.agent.read_file("src/app.js"),
            {"content": "console.log('ok');"},
        )

    def test_blocks_workspace_escape(self):
        with self.assertRaises(ValueError):
            self.agent.safe_path("../outside.txt")

    def test_blocks_destructive_command(self):
        result = self.agent.run_command("rm -rf /")
        self.assertEqual(result, {"error": "Forbidden destructive command"})

    def test_lists_created_files(self):
        self.agent.write_file("index.html", "<h1>Hello</h1>")
        result = self.agent.list_files(".", 3)
        self.assertIn("index.html", result["files"])


if __name__ == "__main__":
    unittest.main()
