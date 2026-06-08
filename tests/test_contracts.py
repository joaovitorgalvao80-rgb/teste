from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

os.environ["APP_ENV"] = "dev"

import app as webapp  # noqa: E402
import database as db  # noqa: E402
import montador  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from services import kaggle_service, packager  # noqa: E402
from services.script_parser import parse_script  # noqa: E402


class DeployContractsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        webapp.DATA_DIR = self.root / "data"
        webapp.WORK_DIR = webapp.DATA_DIR / "work"
        db.DATA_DIR = webapp.DATA_DIR
        db.DB_PATH = webapp.DATA_DIR / "plataforma.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_login_rejects_external_next_redirect(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("alice", "password123")
            resp = client.post(
                "/login",
                data={
                    "username": "alice",
                    "password": "password123",
                    "next": "https://evil.example/phish",
                },
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/projects")

    def test_asset_state_requires_asset_owner(self) -> None:
        with TestClient(webapp.app) as client:
            owner_id = db.create_user("owner", "password123")
            other_id = db.create_user("other", "password123")
            project_id = db.create_project(owner_id, "owner project", "script", {})
            db.replace_scenes(
                project_id,
                [
                    {
                        "scene_id": "scene_001",
                        "idx": 1,
                        "zone": "GANCHO",
                        "start_time": 0,
                        "end_time": 4,
                        "duration": 4,
                        "narration": "teste",
                    }
                ],
            )
            scene = db.list_scenes(project_id)[0]
            db.add_assets(
                scene["id"],
                [
                    {
                        "source": "pexels",
                        "asset_type": "video",
                        "download_url": "https://example.com/a.mp4",
                    }
                ],
            )
            asset = db.list_assets(scene["id"])[0]

            self.assertIsNotNone(other_id)
            client.post(
                "/login",
                data={"username": "other", "password": "password123"},
                follow_redirects=False,
            )
            resp = client.post(f"/assets/{asset['id']}/state", data={"state": "selected"})

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(db.get_asset(asset["id"])["state"], "pending")

    def test_packager_fails_when_no_selected_asset_downloads(self) -> None:
        project = {"name": "Teste"}
        config = {"avatar_safe_area": "right", "resolution": "1920x1080", "format": "16:9"}
        scenes = [
            {
                "id": 1,
                "scene_id": "scene_001",
                "idx": 1,
                "zone": "GANCHO",
                "start_time": 0.0,
                "end_time": 4.0,
                "duration": 4.0,
                "narration": "teste",
                "visual_goal": "teste",
                "keywords": [],
                "must_show": [],
                "must_not_show": [],
                "asset_type": "video",
                "overlay_text": "",
                "avatar_safe_area": "right",
            }
        ]
        selected = {
            1: {
                "source": "pexels",
                "download_url": "https://example.com/a.mp4",
                "asset_type": "video",
                "keyword": "test",
            }
        }

        original_download = packager._download
        packager._download = lambda *_args, **_kwargs: False
        try:
            with self.assertRaisesRegex(RuntimeError, "nenhum asset"):
                packager.build_zip(project, config, scenes, selected, [], self.root / "work")
        finally:
            packager._download = original_download

    def test_montador_rejects_zip_slip_paths(self) -> None:
        bad_zip = self.root / "bad.zip"
        work = self.root / "work"
        work.mkdir()
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zf.writestr("../evil.txt", "owned")
            zf.writestr("guia_visual.json", "{}")

        log = montador.Logger(self.root / "log.txt")
        with self.assertRaisesRegex(RuntimeError, "caminho inseguro"):
            montador.prepare_input(bad_zip, work, log)

        self.assertFalse((work / "evil.txt").exists())

    def test_timestamp_parser_accepts_dash_variants(self) -> None:
        for separator in ["-", "\u2013", "\u2014"]:
            scenes = parse_script(f"[00:00.0 {separator} 00:01.5] Ola mundo")
            self.assertEqual(len(scenes), 1)
            self.assertEqual(scenes[0]["duration"], 1.5)

    def test_kaggle_status_complete_when_video_output_exists(self) -> None:
        original_video = kaggle_service.get_video_url
        try:
            kaggle_service.get_video_url = lambda *_args, **_kwargs: "https://video.example/out.mp4"
            result = kaggle_service.get_status("kernel", "user", "token")
        finally:
            kaggle_service.get_video_url = original_video

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["video_url"], "https://video.example/out.mp4")

    def test_kaggle_status_waits_without_status_endpoint_when_output_missing(self) -> None:
        original_video = kaggle_service.get_video_url
        original_exists = kaggle_service.kernel_exists
        try:
            kaggle_service.get_video_url = lambda *_args, **_kwargs: ""
            kaggle_service.kernel_exists = lambda *_args, **_kwargs: (True, "")
            result = kaggle_service.get_status("kernel", "user", "token")
        finally:
            kaggle_service.get_video_url = original_video
            kaggle_service.kernel_exists = original_exists

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["url"], "https://www.kaggle.com/code/user/kernel")

    def test_kernel_exists_uses_files_command_not_status_endpoint(self) -> None:
        calls = []
        original_run = kaggle_service._run
        try:
            def fake_run(args, username, token, **kwargs):
                calls.append(args)
                return SimpleNamespace(stdout="fileName,totalBytes\nlog_render.txt,10\n", stderr="")

            kaggle_service._run = fake_run
            exists, _detail = kaggle_service.kernel_exists("kernel", "user", "token")
        finally:
            kaggle_service._run = original_run

        self.assertTrue(exists)
        self.assertEqual(calls[0], ["kernels", "files", "user/kernel", "-v", "--page-size", "200"])

    def test_push_kernel_uses_actual_slug_from_push_output(self) -> None:
        original_run = kaggle_service._run
        try:
            def fake_run(args, username, token, **kwargs):
                self.assertEqual(args[:2], ["kernels", "push"])
                return SimpleNamespace(
                    stdout="Kernel pushed: https://www.kaggle.com/code/user/actual-kaggle-slug\n",
                    stderr="",
                )

            kaggle_service._run = fake_run
            slug, _output = kaggle_service.push_kernel("dataset-slug", "Meu Projeto", "user", "token")
        finally:
            kaggle_service._run = original_run

        self.assertEqual(slug, "actual-kaggle-slug")

    def test_production_requires_strong_session_secret(self) -> None:
        old_env = webapp.APP_ENV
        old_secret = os.environ.get("APP_SECRET_KEY")
        try:
            webapp.APP_ENV = "production"
            os.environ.pop("APP_SECRET_KEY", None)
            with self.assertRaises(RuntimeError):
                webapp._require_secret()
            os.environ["APP_SECRET_KEY"] = "short"
            with self.assertRaises(RuntimeError):
                webapp._require_secret()
        finally:
            webapp.APP_ENV = old_env
            if old_secret is None:
                os.environ.pop("APP_SECRET_KEY", None)
            else:
                os.environ["APP_SECRET_KEY"] = old_secret


if __name__ == "__main__":
    unittest.main()
