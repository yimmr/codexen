"""Unit tests for terminal command security inspection and executable parsing."""
import unittest
from codexen.config import ServiceSettings
from codexen.core.command import (
    check_command_security,
    extract_command_executable,
    is_safe_test_command,
)
from codexen.exceptions import CommandSecurityError


class CommandSecurityUnitTests(unittest.TestCase):
    def setUp(self):
        self.settings = ServiceSettings(
            command_whitelist=[
                "git", "pytest", "python", "py", "node", "npm", "pnpm", "yarn",
                "cargo", "rustc", "go", "make", "ruff", "flake8", "tsc",
                "pip", "poetry", "uv", "dir", "echo", "type", "cat", "findstr", "grep",
            ],
            command_blacklist=[
                "format", "diskpart", "mklink", "reg", "regedit",
                "shutdown", "bcdedit", "vssadmin", "cipher", "takeown",
            ],
            command_require_confirmation=[
                "rmdir", "del", "rm", "git push", "git reset --hard",
                "git clean", "npm publish", "pip install --upgrade",
            ],
        )

    def test_extract_command_executable(self):
        # Plain command
        clean, base = extract_command_executable("python -m unittest")
        self.assertEqual(clean, "python")
        self.assertEqual(base, "python")

        # Windows path with .exe
        clean, base = extract_command_executable(r".venv\Scripts\python.exe -m unittest tests -v")
        self.assertEqual(clean, ".venv/scripts/python")
        self.assertEqual(base, "python")

        # Quoted Windows path
        clean, base = extract_command_executable(r'".venv\Scripts\python.exe" -m unittest')
        self.assertEqual(clean, ".venv/scripts/python")
        self.assertEqual(base, "python")

        # POSIX path
        clean, base = extract_command_executable(".venv/bin/pytest tests/")
        self.assertEqual(clean, ".venv/bin/pytest")
        self.assertEqual(base, "pytest")

        # PowerShell invocation prefix
        clean, base = extract_command_executable(r'& ".venv\Scripts\python.exe" -V')
        self.assertEqual(clean, ".venv/scripts/python")
        self.assertEqual(base, "python")

    def test_is_safe_test_command(self):
        self.assertTrue(is_safe_test_command(r".venv\Scripts\python.exe -m unittest tests.test_core_server_dispatcher -v"))
        self.assertTrue(is_safe_test_command(r'".venv\Scripts\python.exe" -m unittest tests -v'))
        self.assertTrue(is_safe_test_command("python -m unittest discover"))
        self.assertTrue(is_safe_test_command("python -m pytest tests/"))
        self.assertTrue(is_safe_test_command("pytest -v"))
        self.assertTrue(is_safe_test_command("npm test"))
        self.assertTrue(is_safe_test_command("pnpm test"))
        self.assertTrue(is_safe_test_command("cargo test"))
        self.assertTrue(is_safe_test_command("go test ./..."))

        # Non-test commands should return False
        self.assertFalse(is_safe_test_command("python harmful_script.py"))
        self.assertFalse(is_safe_test_command("node server.js"))
        self.assertFalse(is_safe_test_command("git status"))

    def test_virtualenv_python_unittest_allowed(self):
        cmd = r".venv\Scripts\python.exe -m unittest tests.test_core_server_dispatcher -v"
        verdict, msg = check_command_security(cmd, self.settings, confirmed=False)
        self.assertEqual(verdict, "allow")
        self.assertEqual(msg, "")

    def test_safe_test_bypasses_interpreter_confirmation(self):
        # Even if python is explicitly placed in command_require_confirmation by strict user config
        strict_settings = ServiceSettings(
            command_whitelist=["git", "pytest", "python"],
            command_blacklist=["format", "shutdown"],
            command_require_confirmation=["python", "rmdir", "del"],
        )
        cmd = r".venv\Scripts\python.exe -m unittest tests.test_core_server_dispatcher -v"
        verdict, msg = check_command_security(cmd, strict_settings, confirmed=False)
        self.assertEqual(verdict, "allow")

        # But arbitrary python execution in strict config still requires confirmation
        arbitrary_cmd = "python script.py"
        verdict2, msg2 = check_command_security(arbitrary_cmd, strict_settings, confirmed=False)
        self.assertEqual(verdict2, "needs_confirmation")

    def test_token_boundary_prevents_substring_false_positives(self):
        # "rm" in confirmation list should NOT match "format" or "performance"
        strict_settings = ServiceSettings(
            command_whitelist=["pnpm", "npm", "git"],
            command_blacklist=[],
            command_require_confirmation=["rm"],
        )
        verdict, msg = check_command_security("pnpm run performance", strict_settings, confirmed=False)
        self.assertEqual(verdict, "allow")

        # "rm" as standalone command should require confirmation
        verdict2, msg2 = check_command_security("rm -rf temp", strict_settings, confirmed=False)
        self.assertEqual(verdict2, "needs_confirmation")

    def test_destructive_blacklists_strictly_blocked(self):
        with self.assertRaises(CommandSecurityError):
            check_command_security("format C: /fs:NTFS", self.settings, confirmed=True)

        with self.assertRaises(CommandSecurityError):
            check_command_security("diskpart /s script.txt", self.settings, confirmed=True)

    def test_unregistered_binary_requires_confirmation(self):
        cmd = "custom_tool --flag"
        verdict, msg = check_command_security(cmd, self.settings, confirmed=False)
        self.assertEqual(verdict, "needs_confirmation")
        self.assertIn("custom_tool", msg)

        # Once confirmed, allowed
        verdict_confirmed, _ = check_command_security(cmd, self.settings, confirmed=True)
        self.assertEqual(verdict_confirmed, "allow")


if __name__ == "__main__":
    unittest.main()
