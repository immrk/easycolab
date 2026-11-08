import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("colab_bridge", ROOT / "colab_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class DriveQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.token_path = Path(self.temp.name) / "token.json"
        self.token = {
            "token": "fake-access-token", "refresh_token": "fake-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "fake-client", "client_secret": "fake-secret",
            "scopes": ["https://www.googleapis.com/auth/drive.file"],
            "expiry": "2099-01-01T00:00:00Z",
        }
        self.token_path.write_text(json.dumps(self.token))
        self.path_patch = patch.object(bridge, "TOKEN_CONFIG_PATH", str(self.token_path))
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.config_path = Path(self.temp.name) / "drive_config.json"
        self.config_patch = patch.object(bridge, "DRIVE_CONFIG_PATH", str(self.config_path), create=True)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.account_patch = patch.object(bridge, "_get_account_email", return_value="creator@example.com")
        self.account_patch.start()
        self.addCleanup(self.account_patch.stop)
        self.env_patch = patch.dict(os.environ, {}, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def drive_response(self, status=200):
        response = Mock(status_code=status, ok=status == 200)
        response.json.return_value = (
            {"user": {"emailAddress": "creator@example.com"}, "storageQuota": {"limit": "123"}}
            if status == 200 else {"error": {"message": "Drive API is disabled"}}
        )
        return response

    def test_configure_probes_project_before_persisting_it(self):
        with patch.object(bridge.requests, "get", return_value=self.drive_response()) as get:
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                bridge.cmd_drive_configure("example-drive-project")
        self.assertTrue(json.loads(output.getvalue())["ok"])
        self.assertEqual(get.call_args.kwargs["headers"]["x-goog-user-project"], "example-drive-project")
        saved = json.loads(self.config_path.read_text())
        self.assertEqual(saved["accounts"]["creator@example.com"], "example-drive-project")
        self.assertEqual(json.loads(self.token_path.read_text()), self.token)

    def test_denied_project_does_not_change_credentials(self):
        original = self.token_path.read_bytes()
        with patch.object(bridge.requests, "get", return_value=self.drive_response(403)):
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                bridge.cmd_drive_configure("example-drive-project")
        self.assertFalse(json.loads(output.getvalue())["ok"])
        self.assertEqual(self.token_path.read_bytes(), original)
        self.assertFalse(self.config_path.exists())

    def test_persisted_project_is_used_without_shell_environment(self):
        self.config_path.write_text(json.dumps({"accounts": {"creator@example.com": "saved-drive-project"}}))
        self.assertEqual(bridge._get_drive_quota_project(), "saved-drive-project")

    def test_switching_accounts_does_not_reuse_another_accounts_project(self):
        self.config_path.write_text(json.dumps({"accounts": {"other@example.com": "other-project"}}))
        self.assertEqual(bridge._get_drive_quota_project(), "")

    def test_environment_override_takes_precedence(self):
        self.config_path.write_text(json.dumps({"accounts": {"creator@example.com": "saved-drive-project"}}))
        os.environ["GOOGLE_CLOUD_QUOTA_PROJECT"] = "override-project"
        self.assertEqual(bridge._get_drive_quota_project(), "override-project")

    def test_generated_cloud_code_passes_header_to_child_environment(self):
        os.environ["RCLONE_HEADER"] = '"X-Custom: one,two",x-goog-user-project: old-project'
        code = bridge._with_drive_quota_project("captured_env = easycolab_drive_env\n", "new-project")
        namespace = {}
        exec(compile(code, "<cloud-bootstrap>", "exec"), namespace)
        headers = next(csv.reader([namespace["captured_env"]["RCLONE_HEADER"]]))
        self.assertEqual(headers, ["X-Custom: one,two", "x-goog-user-project: new-project"])
        self.assertIn("old-project", os.environ["RCLONE_HEADER"])

    def test_no_project_preserves_existing_environment(self):
        os.environ["RCLONE_HEADER"] = "X-Custom: keep"
        namespace = {}
        exec(bridge._with_drive_quota_project("captured_env = easycolab_drive_env\n", ""), namespace)
        self.assertEqual(namespace["captured_env"]["RCLONE_HEADER"], "X-Custom: keep")

    def test_project_survives_token_refresh_during_rclone_export(self):
        self.config_path.write_text(json.dumps({"accounts": {"creator@example.com": "saved-drive-project"}}))
        self.token["expiry"] = "2000-01-01T00:00:00Z"
        self.token_path.write_text(json.dumps(self.token))

        def refresh(creds, request):
            creds.token = "refreshed-fake-token"

        with patch.object(bridge.Credentials, "refresh", refresh):
            config = bridge._build_rclone_drive_conf()
        self.assertIn("[easycolab_gdrive]", config)
        self.assertEqual(bridge._get_drive_quota_project(), "saved-drive-project")


if __name__ == "__main__":
    unittest.main()
