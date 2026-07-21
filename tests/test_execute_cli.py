import contextlib
import csv
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from src.candidate_execution import SafetyError
from tests.test_single_candidate_flow import rows


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/execute-single-candidate.py"
SPEC = importlib.util.spec_from_file_location("execute_single_candidate_resume", SCRIPT)
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


class ExecuteCliConstructionTest(unittest.TestCase):
    def write_manifest(self, directory):
        values = rows(); path = Path(directory) / "manifest.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=values[0].keys())
            writer.writeheader(); writer.writerows(values)
        return path

    def test_resume_without_api_key_does_not_construct_glm(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.write_manifest(directory)
            flow = mock.Mock(); flow.execute.return_value = {"english_post_id": 1001, "status": "completed"}
            with mock.patch.dict(os.environ, {"WP_ADMIN_COOKIE": "c", "WP_REST_NONCE": "n"}, clear=True), \
                 mock.patch("src.wordpress_clients.WordPressRestClient") as wp, \
                 mock.patch("src.wordpress_clients.SlyTranslateClient") as translator, \
                 mock.patch("src.polylang_ssh.PolylangSshChecker") as polylang, \
                 mock.patch("src.glm47_excerpt_client.Glm47ExcerptClient") as glm, \
                 mock.patch("src.single_candidate_flow.SingleCandidateFlow", return_value=flow) as flow_class, \
                 contextlib.redirect_stdout(io.StringIO()):
                code = CLI.main(["--post-id", "1", "--execute", "--resume",
                                 "--manifest", str(manifest)])
            self.assertEqual(0, code); glm.assert_not_called()
            wp.assert_called_once(); translator.assert_called_once(); polylang.assert_called_once()
            self.assertIsNone(flow_class.call_args.args[3 - 1])
            flow.execute.assert_called_once_with(1, resume=True)

    def test_normal_execute_without_api_key_still_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.write_manifest(directory)
            with mock.patch.dict(os.environ, {"WP_ADMIN_COOKIE": "c", "WP_REST_NONCE": "n"}, clear=True), \
                 mock.patch("src.wordpress_clients.WordPressRestClient"), \
                 mock.patch("src.wordpress_clients.SlyTranslateClient"), \
                 mock.patch("src.polylang_ssh.PolylangSshChecker"):
                with self.assertRaisesRegex(SafetyError, "ZHIPU_API_KEY"):
                    CLI.main(["--post-id", "1", "--execute", "--manifest", str(manifest)])

    def test_normal_execute_constructs_glm_and_passes_it_to_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.write_manifest(directory)
            flow = mock.Mock(); flow.execute.return_value = {"english_post_id": 1001, "status": "completed"}
            glm_instance = object()
            with mock.patch("src.wordpress_clients.WordPressRestClient"), \
                 mock.patch("src.wordpress_clients.SlyTranslateClient"), \
                 mock.patch("src.polylang_ssh.PolylangSshChecker"), \
                 mock.patch("src.glm47_excerpt_client.Glm47ExcerptClient", return_value=glm_instance) as glm, \
                 mock.patch("src.single_candidate_flow.SingleCandidateFlow", return_value=flow) as flow_class, \
                 contextlib.redirect_stdout(io.StringIO()):
                CLI.main(["--post-id", "1", "--execute", "--manifest", str(manifest)])
            glm.assert_called_once(); self.assertIs(glm_instance, flow_class.call_args.args[2])
            flow.execute.assert_called_once_with(1, resume=False)


if __name__ == "__main__":
    unittest.main()
