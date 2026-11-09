"""Test server module import and default configuration loading."""
from __future__ import annotations

import unittest
from app import server


class ServerModuleTests(unittest.TestCase):
    def test_server_instance_and_tools_bound(self):
        self.assertIsNotNone(server.mcp)
        self.assertTrue(callable(server.list_projects))
        self.assertTrue(callable(server.read_file))
        self.assertTrue(callable(server.read_project_file))
        self.assertTrue(callable(server.write_file))
        self.assertTrue(callable(server.write_project_file))
        self.assertTrue(callable(server.list_files))
        self.assertTrue(callable(server.list_project_files))
        self.assertTrue(callable(server.list_directory))
        self.assertTrue(callable(server.get_file_info))
        self.assertTrue(callable(server.edit_file_snippet))
        self.assertTrue(callable(server.delete_file))
        self.assertTrue(callable(server.delete_directory))
        self.assertTrue(callable(server.reload_projects_config))
        self.assertTrue(callable(server.create_project))
        self.assertTrue(callable(server.copy_path))
        self.assertTrue(callable(server.copy_directory))
        self.assertTrue(callable(server.copy_file))
        self.assertTrue(callable(server.move_path))
        self.assertTrue(callable(server.move_file))
        self.assertTrue(callable(server.rename_file))
        self.assertTrue(callable(server.create_zip_archive))
        self.assertTrue(callable(server.extract_zip_archive))
        self.assertTrue(callable(server.run_terminal_command))
        self.assertTrue(callable(server.run_command))
        self.assertTrue(callable(server.search_code))
        self.assertTrue(callable(server.search_files))
        self.assertTrue(callable(server.find_in_files))
        self.assertTrue(callable(server.get_project_info))
        self.assertTrue(callable(server.get_project))
        self.assertTrue(callable(server.git_status))
        self.assertTrue(callable(server.git_diff))
        self.assertTrue(callable(server.git_log))
        self.assertTrue(callable(server.start_background_process))
        self.assertTrue(callable(server.read_process_output))
        self.assertTrue(callable(server.send_process_input))
        self.assertTrue(callable(server.stop_background_process))
        self.assertTrue(callable(server.list_background_processes))
        self.assertTrue(callable(server.read_binary_file))
        self.assertTrue(callable(server.read_project_binary_file))
        self.assertTrue(callable(server.write_binary_file))
        self.assertTrue(callable(server.write_project_binary_file))
        self.assertTrue(callable(server.read_media_file))
        self.assertTrue(callable(server.read_image_file))

    def test_default_config_projects(self):
        projects = server.list_projects()
        names = [p["name"] for p in projects]
        # Both literary_workflow_v1 and literary-workflow must be configured
        self.assertIn("literary_workflow_v1", names)
        self.assertIn("literary-workflow", names)

        lw = next(p for p in projects if p["name"] == "literary_workflow_v1")
        self.assertTrue(lw["exists"])
        lw2 = next(p for p in projects if p["name"] == "literary-workflow")
        self.assertTrue(lw2["exists"])


if __name__ == "__main__":
    unittest.main()
