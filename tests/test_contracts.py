from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["APP_ENV"] = "dev"

import app as webapp  # noqa: E402
import database as db  # noqa: E402
import montador  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from services import asset_search, diagnostics, kaggle_service, packager  # noqa: E402
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

    def test_package_route_requires_selection_for_every_scene(self) -> None:
        with TestClient(webapp.app) as client:
            user_id = db.create_user("partial", "password123")
            project_id = db.create_project(user_id, "partial project", "script", {})
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
                        "narration": "um",
                    },
                    {
                        "scene_id": "scene_002",
                        "idx": 2,
                        "zone": "CTA",
                        "start_time": 4,
                        "end_time": 8,
                        "duration": 4,
                        "narration": "dois",
                    },
                ],
            )
            first_scene = db.list_scenes(project_id)[0]
            db.add_assets(
                first_scene["id"],
                [
                    {
                        "source": "pexels",
                        "asset_type": "video",
                        "download_url": "https://example.com/a.mp4",
                    }
                ],
            )
            asset = db.list_assets(first_scene["id"])[0]
            db.set_asset_state(asset["id"], "selected")
            client.post(
                "/login",
                data={"username": "partial", "password": "password123"},
                follow_redirects=False,
            )
            resp = client.post(f"/projects/{project_id}/package", follow_redirects=False)

        self.assertEqual(resp.status_code, 400)
        self.assertIn("scene_002", resp.text)

    def test_project_page_shows_operational_diagnostics(self) -> None:
        with TestClient(webapp.app) as client:
            user_id = db.create_user("ops", "password123")
            project_id = db.create_project(user_id, "ops project", "script", {})
            db.create_job(user_id, "package", project_id, "Preparando pacote ZIP")
            client.post(
                "/login",
                data={"username": "ops", "password": "password123"},
                follow_redirects=False,
            )
            resp = client.get(f"/projects/{project_id}")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("Saude do projeto", resp.text)
        self.assertIn("Jobs recentes", resp.text)
        self.assertIn("package", resp.text)

    def test_diagnostics_json_route_reports_project_snapshot(self) -> None:
        with TestClient(webapp.app) as client:
            user_id = db.create_user("diag", "password123")
            project_id = db.create_project(user_id, "diag project", "script", {})
            client.post(
                "/login",
                data={"username": "diag", "password": "password123"},
                follow_redirects=False,
            )
            resp = client.get(f"/projects/{project_id}/diagnostics.json")

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["project"]["name"], "diag project")
        self.assertIn("diagnostics", payload)
        self.assertIn("jobs", payload)

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

    def test_packager_rejects_partial_scene_downloads(self) -> None:
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
                "narration": "parte um",
                "visual_goal": "teste",
                "keywords": [],
                "must_show": [],
                "must_not_show": [],
                "asset_type": "video",
                "overlay_text": "",
                "avatar_safe_area": "right",
            },
            {
                "id": 2,
                "scene_id": "scene_002",
                "idx": 2,
                "zone": "CTA",
                "start_time": 4.0,
                "end_time": 8.0,
                "duration": 4.0,
                "narration": "parte dois",
                "visual_goal": "teste",
                "keywords": [],
                "must_show": [],
                "must_not_show": [],
                "asset_type": "video",
                "overlay_text": "",
                "avatar_safe_area": "right",
            },
        ]
        selected = {
            1: {
                "source": "pexels",
                "download_url": "https://example.com/a.mp4",
                "asset_type": "video",
                "keyword": "test",
            },
            2: {
                "source": "pexels",
                "download_url": "https://example.com/b.mp4",
                "asset_type": "video",
                "keyword": "test",
            },
        }

        def fake_download(url, dest, _max_bytes):
            if url.endswith("/a.mp4"):
                dest.write_bytes(b"fake-video")
                return True
            return False

        original_download = packager._download
        packager._download = fake_download
        try:
            with self.assertRaisesRegex(RuntimeError, "pacote incompleto"):
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

    def test_project_config_sanitizes_invalid_values(self) -> None:
        raw = {
            "resolution": "bogus",
            "avatar_safe_area": "center",
            "scene_duration": -9,
            "avatar_safe_width_ratio": 2,
            "per_keyword": 999,
            "max_download_mb": "bad",
            "image_fallback": "0",
            "visual_style": "  ",
        }
        cfg = webapp.normalize_project_config(raw)
        self.assertEqual(cfg["resolution"], "1920x1080")
        self.assertEqual(cfg["avatar_safe_area"], "right")
        self.assertEqual(cfg["scene_duration"], 2.0)
        self.assertEqual(cfg["avatar_safe_width_ratio"], 0.45)
        self.assertEqual(cfg["per_keyword"], 20)
        self.assertEqual(cfg["max_download_mb"], 90)
        self.assertFalse(cfg["image_fallback"])
        self.assertEqual(cfg["visual_style"], webapp.DEFAULT_CONFIG["visual_style"])

    def test_pexels_video_endpoint_uses_current_v1_path(self) -> None:
        self.assertEqual(asset_search.PEXELS_VIDEO_URL, "https://api.pexels.com/v1/videos/search")

    def test_pixabay_video_download_url_gets_download_flag(self) -> None:
        url = asset_search._with_query_param("https://example.com/video.mp4?token=abc", "download", "1")
        self.assertEqual(url, "https://example.com/video.mp4?token=abc&download=1")

    def test_pixabay_per_page_respects_api_minimum(self) -> None:
        self.assertEqual(asset_search._bounded_per_page(1, minimum=3), 3)
        self.assertEqual(asset_search._bounded_per_page(2, minimum=3), 3)

    def test_kaggle_status_complete_when_video_output_exists(self) -> None:
        original_video = kaggle_service.get_video_url
        try:
            kaggle_service.get_video_url = lambda *_args, **_kwargs: "https://video.example/out.mp4"
            result = kaggle_service.get_status("kernel", "user", "token")
        finally:
            kaggle_service.get_video_url = original_video

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["video_url"], "https://video.example/out.mp4")

    def test_get_video_url_prefers_hyperframes_master(self) -> None:
        class FakeResponse:
            ok = True

            @staticmethod
            def json():
                return {
                    "files": [
                        {"fileName": "video_broll_base.mp4", "url": "https://video.example/base.mp4"},
                        {"fileName": "final_master.mp4", "url": "https://video.example/master.mp4"},
                    ]
                }

        with patch("requests.get", return_value=FakeResponse()):
            url = kaggle_service.get_video_url("kernel", "user", "token")

        self.assertEqual(url, "https://video.example/master.mp4")

    def test_get_video_url_ignores_hyperframes_asset_copy(self) -> None:
        class FakeResponse:
            ok = True

            @staticmethod
            def json():
                return {
                    "files": [
                        {
                            "fileName": "hyperframes_master/assets/video_broll_base.mp4",
                            "url": "https://video.example/internal-asset.mp4",
                        },
                        {"fileName": "video_broll_base.mp4", "url": "https://video.example/base.mp4"},
                    ]
                }

        with patch("requests.get", return_value=FakeResponse()):
            url = kaggle_service.get_video_url("kernel", "user", "token")

        self.assertEqual(url, "https://video.example/base.mp4")

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

    def test_kaggle_status_does_not_complete_from_stale_files_while_running(self) -> None:
        original_video = kaggle_service.get_video_url
        original_files = kaggle_service.list_kernel_files
        original_hint = kaggle_service.kernel_status_hint
        try:
            kaggle_service.get_video_url = lambda *_args, **_kwargs: ""
            kaggle_service.list_kernel_files = lambda *_args, **_kwargs: (["video_broll_base.mp4"], "")
            kaggle_service.kernel_status_hint = lambda *_args, **_kwargs: 'has status "KernelWorkerStatus.RUNNING"'
            result = kaggle_service.get_status("kernel", "user", "token")
        finally:
            kaggle_service.get_video_url = original_video
            kaggle_service.list_kernel_files = original_files
            kaggle_service.kernel_status_hint = original_hint

        self.assertEqual(result["status"], "queued")
        self.assertIn("execucao", result["error"])

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

    def test_kaggle_status_route_downloads_local_video_fallback(self) -> None:
        original_status = webapp.kaggle_service.get_status
        original_pull = webapp.kaggle_service.pull_output_video
        try:
            with TestClient(webapp.app) as client:
                user_id = db.create_user("video-user", "password123")
                db.update_kaggle_keys(user_id, "video-user", "token")
                project_id = db.create_project(user_id, "video project", "script", {})
                db.update_kaggle_job(project_id, "dataset", "kernel", "queued")
                client.post(
                    "/login",
                    data={"username": "video-user", "password": "password123"},
                    follow_redirects=False,
                )

                def fake_pull(_slug, _username, _token, out_dir):
                    out_dir.mkdir(parents=True, exist_ok=True)
                    base = out_dir / "video_broll_base.mp4"
                    base.write_bytes(b"fake-mp4")
                    (out_dir / "final_master.mp4").write_bytes(b"fake-master")
                    return base

                webapp.kaggle_service.get_status = lambda *_args, **_kwargs: {
                    "status": "complete",
                    "url": "https://www.kaggle.com/code/video-user/kernel",
                    "video_url": "",
                    "error": "",
                }
                webapp.kaggle_service.pull_output_video = fake_pull
                resp = client.get(f"/projects/{project_id}/kaggle-status")
        finally:
            webapp.kaggle_service.get_status = original_status
            webapp.kaggle_service.pull_output_video = original_pull

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["video_url"], f"/projects/{project_id}/download-master-video")
        self.assertEqual(payload["master_video_url"], f"/projects/{project_id}/download-master-video")
        self.assertEqual(payload["base_video_url"], f"/projects/{project_id}/download-base-video")
        self.assertIn("validation", payload)

    def test_output_validation_writes_diagnostic_file(self) -> None:
        project_work = self.root / "project"
        out = project_work / "kaggle_output"
        out.mkdir(parents=True)
        (out / "video_broll_base.mp4").write_bytes(b"not-a-real-mp4")

        payload = diagnostics.validate_outputs(project_work, expected_duration=8.0)

        self.assertEqual(payload["status"], "warn")
        self.assertTrue(payload["outputs"]["base"]["exists"])
        self.assertTrue(diagnostics.validation_path(project_work).exists())
        cached = diagnostics.read_validation(project_work)
        self.assertEqual(cached["status"], "warn")

    def test_output_validation_flags_requested_audio_missing(self) -> None:
        project_work = self.root / "project"
        out = project_work / "kaggle_output"
        out.mkdir(parents=True)
        (out / "final_master.mp4").write_bytes(b"not-a-real-mp4")
        (out / "hyperframes_status.json").write_text(
            json.dumps({"requested_audio": True, "audio": False}),
            encoding="utf-8",
        )

        payload = diagnostics.validate_outputs(project_work, expected_duration=0)

        self.assertEqual(payload["status"], "error")
        messages = [issue["message"] for issue in payload["issues"]]
        self.assertTrue(any("narracao" in msg for msg in messages))

    def test_send_to_kaggle_requires_valid_package_status(self) -> None:
        original_upload = webapp.kaggle_service.upload_dataset
        try:
            webapp.kaggle_service.upload_dataset = lambda *_args, **_kwargs: self.fail("nao deveria enviar")
            with TestClient(webapp.app) as client:
                user_id = db.create_user("kaggle-user", "password123")
                db.update_kaggle_keys(user_id, "kaggle-user", "token")
                project_id = db.create_project(user_id, "video project", "script", {})
                client.post(
                    "/login",
                    data={"username": "kaggle-user", "password": "password123"},
                    follow_redirects=False,
                )
                resp = client.post(f"/projects/{project_id}/send-to-kaggle")
        finally:
            webapp.kaggle_service.upload_dataset = original_upload

        self.assertEqual(resp.status_code, 400)
        self.assertIn("pacote valido", resp.json()["error"])

    def test_send_to_kaggle_runs_as_background_job(self) -> None:
        original_upload = webapp.kaggle_service.upload_dataset
        original_push = webapp.kaggle_service.push_kernel
        try:
            with TestClient(webapp.app) as client:
                user_id = db.create_user("bg-user", "password123")
                db.update_kaggle_keys(user_id, "bg-user", "token")
                project_id = db.create_project(user_id, "video project", "script", {})
                db.set_project_status(project_id, "packaged")
                project_work = webapp.WORK_DIR / f"project_{project_id}"
                project_work.mkdir(parents=True)
                (project_work / "asset_pack_video.zip").write_bytes(b"zip")
                calls = []

                def fake_upload(zip_path, project_name, username, token, project_id=None):
                    calls.append(("upload", Path(zip_path).name, project_name, username, token, project_id))
                    return "dataset-slug"

                def fake_push(ds_slug, project_name, username, token, project_id=None):
                    calls.append(("push", ds_slug, project_name, username, token, project_id))
                    return "kernel-slug", "pushed"

                webapp.kaggle_service.upload_dataset = fake_upload
                webapp.kaggle_service.push_kernel = fake_push
                client.post(
                    "/login",
                    data={"username": "bg-user", "password": "password123"},
                    follow_redirects=False,
                )
                resp = client.post(f"/projects/{project_id}/send-to-kaggle")
                payload = resp.json()
                job = client.get(f"/jobs/{payload['job_id']}").json()
                project = db.get_project(project_id, user_id)
        finally:
            webapp.kaggle_service.upload_dataset = original_upload
            webapp.kaggle_service.push_kernel = original_push

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(payload["status"], "uploading")
        self.assertEqual(job["status"], "complete")
        self.assertEqual(job["result"]["kernel_slug"], "kernel-slug")
        self.assertEqual(project["kaggle_kernel_slug"], "kernel-slug")
        self.assertEqual(calls[0][0], "upload")
        self.assertEqual(calls[1][0], "push")

    def test_pull_output_video_prefers_hyperframes_master(self) -> None:
        original_run = kaggle_service._run
        calls = []
        try:
            def fake_run(args, _username, _token, **_kwargs):
                calls.append(args)
                out_dir = Path(args[args.index("-p") + 1])
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "video_broll_base.mp4").write_bytes(b"base")
                (out_dir / "final_master.mp4").write_bytes(b"master")
                return SimpleNamespace(stdout="", stderr="")

            kaggle_service._run = fake_run
            path = kaggle_service.pull_output_video("kernel", "user", "token", self.root / "out")
        finally:
            kaggle_service._run = original_run

        self.assertIsNotNone(path)
        self.assertEqual(path.name, "final_master.mp4")
        pattern = calls[0][calls[0].index("--file-pattern") + 1]
        self.assertIn("hyperframes_status", pattern)
        self.assertIn("log_render", pattern)

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

    def test_push_kernel_enables_internet_for_hyperframes(self) -> None:
        original_run = kaggle_service._run
        try:
            def fake_run(args, username, _token, **_kwargs):
                self.assertEqual(username, "user")
                self.assertEqual(args[:2], ["kernels", "push"])
                metadata = json.loads((Path(args[-1]) / "kernel-metadata.json").read_text(encoding="utf-8"))
                self.assertTrue(metadata["enable_internet"])
                self.assertEqual(metadata["dataset_sources"], ["user/dataset-slug"])
                return SimpleNamespace(
                    stdout="Kernel pushed: https://www.kaggle.com/code/user/hyperframes-kernel\n",
                    stderr="",
                )

            kaggle_service._run = fake_run
            slug, _output = kaggle_service.push_kernel("dataset-slug", "Meu Projeto", "user", "token")
        finally:
            kaggle_service._run = original_run

        self.assertEqual(slug, "hyperframes-kernel")

    def test_kaggle_runner_accepts_unpacked_asset_pack(self) -> None:
        compile(kaggle_service._RUNNER, "<kaggle-runner>", "exec")
        self.assertIn('rglob("guia_visual.json")', kaggle_service._RUNNER)
        self.assertIn('source = guides[0].parent', kaggle_service._RUNNER)
        self.assertIn("final_master.mp4", kaggle_service._RUNNER)
        self.assertIn("hyperframes_status.json", kaggle_service._RUNNER)
        self.assertIn('"node@22"', kaggle_service._RUNNER)
        self.assertIn('"hyperframes"', kaggle_service._RUNNER)
        self.assertIn('window.__timelines["nwrch-master"]', kaggle_service._RUNNER)
        self.assertIn('"apt-get", "install"', kaggle_service._RUNNER)
        self.assertIn('"libatk1.0-0"', kaggle_service._RUNNER)
        self.assertNotIn('"chromium-browser"', kaggle_service._RUNNER)
        self.assertIn('"HYPERFRAMES_BROWSER_PATH"', kaggle_service._RUNNER)
        self.assertIn('"PUPPETEER_EXECUTABLE_PATH"', kaggle_service._RUNNER)
        self.assertIn('"--workers"', kaggle_service._RUNNER)
        self.assertIn('"PRODUCER_LOW_MEMORY_MODE"', kaggle_service._RUNNER)
        self.assertIn('"PRODUCER_PLAYER_READY_TIMEOUT_MS"', kaggle_service._RUNNER)
        self.assertIn('"--low-memory-mode"', kaggle_service._RUNNER)
        self.assertIn('"--protocol-timeout"', kaggle_service._RUNNER)
        self.assertIn('"900000"', kaggle_service._RUNNER)
        self.assertIn('"--browser-timeout"', kaggle_service._RUNNER)
        self.assertIn('"180"', kaggle_service._RUNNER)
        self.assertIn('"--player-ready-timeout"', kaggle_service._RUNNER)
        self.assertIn('"png-sequence"', kaggle_service._RUNNER)
        self.assertIn("hyperframes_frames", kaggle_service._RUNNER)
        self.assertIn('"format=yuv420p"', kaggle_service._RUNNER)
        self.assertIn('"png-sequence+ffmpeg"', kaggle_service._RUNNER)
        self.assertIn("data-duration=", kaggle_service._RUNNER)

    def test_runner_composition_uses_edit_plan_extras(self) -> None:
        runner = kaggle_service._RUNNER
        compile(runner, "runner.py", "exec")
        self.assertIn("find_pack_extras(input_root)", runner)
        self.assertIn('"edit_plan.json"', runner)
        self.assertIn("render_hyperframes_master(out, edit_plan, narration_file, avatar_file)", runner)
        self.assertIn("apply_master_postprocess(master_out, narration, avatar, edit_plan)", runner)
        self.assertIn('"--no-overlay"', runner)
        # camadas da composicao master
        self.assertIn("slow_push_in", runner)
        self.assertIn("slow_pull_out", runner)
        self.assertIn("drift_left", runner)
        self.assertIn("drift_right", runner)
        self.assertIn('class="clip caption pos-', runner)
        self.assertIn('class="clip fadeov"', runner)
        self.assertIn('data-has-audio="true"', runner)
        self.assertIn('class="clip avatar-clip pos-', runner)
        self.assertIn("ffmpeg_audio_mix", runner)
        self.assertIn("ffmpeg_overlay", runner)
        # zip-slip: extras extraidos apenas pelo nome do arquivo
        self.assertIn("PACK_EXTRAS_DIR / Path(member).name", runner)

    def test_edit_plan_builder_is_deterministic(self) -> None:
        from services import edit_plan as ep

        project = {"name": "Meu Video"}
        config = {"avatar_safe_area": "right", "resolution": "1280x720", "avatar_safe_width_ratio": 0.25}
        scenes = [
            {"scene_id": "scene_001", "start_time": 0.0, "end_time": 4.0, "duration": 4.0, "overlay_text": "Abertura"},
            {"scene_id": "scene_002", "start_time": 4.0, "end_time": 9.5, "duration": 5.5, "overlay_text": ""},
            {"scene_id": "scene_003", "start_time": 9.5, "end_time": 12.0, "duration": 2.5, "overlay_text": "Fim"},
        ]
        plan = ep.build_edit_plan(project, config, scenes)
        self.assertEqual(plan["version"], 1)
        self.assertEqual(plan["resolution"], "1280x720")
        self.assertEqual(plan["caption_position"], "left")
        self.assertIsNone(plan["audio"])
        self.assertIsNone(plan["avatar"])
        self.assertEqual(plan["editorial_mode"], "deterministic_v2")
        self.assertEqual(plan["caption_policy"]["selected"], 2)
        motions = [s["motion"] for s in plan["scenes"]]
        self.assertEqual(motions, ["slow_push_in", "drift_left", "slow_push_in"])
        transitions = [s["transition_out"] for s in plan["scenes"]]
        self.assertEqual(transitions, ["none", "none", "none"])
        self.assertEqual(plan["scenes"][0]["caption"], "Abertura")
        self.assertEqual(plan["scenes"][1]["caption"], "")
        self.assertEqual(plan["scenes"][2]["caption"], "Fim")
        self.assertEqual(plan["scenes"][0]["caption_start"], 0.55)
        self.assertGreater(plan["scenes"][0]["caption_duration"], 0)

        with_media = ep.build_edit_plan(
            project, config, scenes, narration_file="narration.mp3", avatar_file="avatar.webm"
        )
        self.assertEqual(with_media["audio"], {"src": "narration.mp3", "volume": 1.0})
        self.assertEqual(with_media["avatar"]["src"], "avatar.webm")
        self.assertEqual(with_media["avatar"]["position"], "right")
        self.assertAlmostEqual(with_media["avatar"]["scale"], 0.25)

    def test_llm_edit_plan_merges_valid_directives_only(self) -> None:
        from services import edit_plan as ep
        from services import llm_service

        project = {"name": "Meu Video"}
        config = {"avatar_safe_area": "right", "resolution": "1280x720"}
        scenes = [
            {"scene_id": "scene_001", "start_time": 0.0, "duration": 4.0, "overlay_text": "Abertura", "narration": "n1"},
            {"scene_id": "scene_002", "start_time": 4.0, "duration": 5.5, "overlay_text": "", "narration": "n2"},
            {"scene_id": "scene_003", "start_time": 9.5, "duration": 2.5, "overlay_text": "Fim", "narration": "n3"},
        ]
        llm_payload = {
            "scenes": [
                # motion invalido (descartado), caption valido (aplicado)
                {"scene_id": "scene_001", "motion": "spin_360", "transition_out": "none", "caption": "Comece agora"},
                # tudo valido, incluindo "none" para descansar a camera
                {"scene_id": "scene_002", "motion": "none", "transition_out": "fade", "caption": ""},
                # ultima cena: LLM pede fade, mas a regra forca "none"
                {"scene_id": "scene_003", "motion": "slow_pull_out", "transition_out": "fade", "caption": '"Ate amanha"'},
            ]
        }
        fake_resp = SimpleNamespace(
            status_code=200,
            json=lambda: {"choices": [{"message": {"content": json.dumps(llm_payload)}}]},
        )
        with patch.object(llm_service.requests, "post", return_value=fake_resp) as mocked:
            plan = ep.build_edit_plan_with_llm(project, config, scenes, openrouter_key="sk-or-test")
        self.assertTrue(mocked.called)
        self.assertEqual(plan["editorial"], "llm")
        s1, s2, s3 = plan["scenes"]
        # timing nunca muda
        self.assertEqual([s1["start"], s2["start"], s3["start"]], [0.0, 4.0, 9.5])
        # motion invalido mantem o deterministico; caption do LLM entra
        self.assertEqual(s1["motion"], "slow_push_in")
        self.assertEqual(s1["transition_out"], "none")
        self.assertEqual(s1["caption"], "Comece agora")
        self.assertEqual(s2["motion"], "none")
        self.assertEqual(s2["transition_out"], "fade")
        self.assertEqual(s2["caption"], "")
        self.assertEqual(s3["motion"], "slow_pull_out")
        self.assertEqual(s3["transition_out"], "none")
        self.assertEqual(s3["caption"], "Ate amanha")

    def test_llm_edit_plan_falls_back_when_llm_fails(self) -> None:
        from services import edit_plan as ep
        from services import llm_service

        project = {"name": "Meu Video"}
        config = {"avatar_safe_area": "right", "resolution": "1280x720"}
        scenes = [
            {"scene_id": "scene_001", "start_time": 0.0, "duration": 4.0, "overlay_text": "Abertura"},
            {"scene_id": "scene_002", "start_time": 4.0, "duration": 5.5, "overlay_text": ""},
        ]
        baseline = ep.build_edit_plan(project, config, scenes)

        with patch.object(llm_service.requests, "post", side_effect=OSError("rede caiu")):
            plan = ep.build_edit_plan_with_llm(project, config, scenes, openrouter_key="sk-or-test")
        self.assertNotIn("editorial", plan)
        self.assertEqual(plan, baseline)

        # sem chave nem tenta chamar a rede
        with patch.object(llm_service.requests, "post", side_effect=AssertionError("nao deveria chamar")):
            plan = ep.build_edit_plan_with_llm(project, config, scenes, openrouter_key="")
        self.assertEqual(plan, baseline)

    def test_llm_edit_plan_can_clear_deterministic_caption(self) -> None:
        from services import edit_plan as ep
        from services import llm_service

        project = {"name": "Meu Video"}
        config = {"avatar_safe_area": "right", "resolution": "1280x720"}
        scenes = [
            {"scene_id": "scene_001", "start_time": 0.0, "duration": 4.0, "overlay_text": "Abertura", "narration": "n1"},
            {"scene_id": "scene_002", "start_time": 4.0, "duration": 4.0, "overlay_text": "Final", "narration": "n2"},
        ]
        llm_payload = {
            "scenes": [
                {"scene_id": "scene_001", "motion": "hold", "transition_out": "none", "caption": ""},
                {"scene_id": "scene_002", "motion": "hold", "transition_out": "none", "caption": ""},
            ]
        }
        fake_resp = SimpleNamespace(
            status_code=200,
            json=lambda: {"choices": [{"message": {"content": json.dumps(llm_payload)}}]},
        )
        with patch.object(llm_service.requests, "post", return_value=fake_resp):
            plan = ep.build_edit_plan_with_llm(project, config, scenes, openrouter_key="sk-or-test")

        self.assertEqual([scene["caption"] for scene in plan["scenes"]], ["", ""])

    def test_settings_saves_openrouter_key(self) -> None:
        with TestClient(webapp.app) as client:
            uid = db.create_user("editor", "password123")
            client.post(
                "/login",
                data={"username": "editor", "password": "password123"},
                follow_redirects=False,
            )
            resp = client.post(
                "/settings",
                data={"openrouter": "sk-or-test-123", "groq_model": ""},
                follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 303)
        user = db.get_user(uid)
        self.assertEqual(user["openrouter_key"], "sk-or-test-123")

    def test_packager_includes_edit_plan_and_extra_files(self) -> None:
        project = {"name": "Plano"}
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
        narration = self.root / "narration.mp3"
        narration.write_bytes(b"fake-audio")

        def fake_download(_url, dest, _max_bytes):
            dest.write_bytes(b"fake-video")
            return True

        original_download = packager._download
        packager._download = fake_download
        try:
            zip_path = packager.build_zip(
                project,
                config,
                scenes,
                selected,
                [],
                self.root / "work",
                edit_plan={"version": 1, "scenes": []},
                extra_files=[narration],
            )
        finally:
            packager._download = original_download

        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
        self.assertIn("edit_plan.json", names)
        self.assertIn("narration.mp3", names)

    def test_upload_media_saves_replaces_and_removes(self) -> None:
        with TestClient(webapp.app) as client:
            uid = db.create_user("media", "password123")
            project_id = db.create_project(uid, "proj", "script", {})
            client.post(
                "/login",
                data={"username": "media", "password": "password123"},
                follow_redirects=False,
            )
            resp = client.post(
                f"/projects/{project_id}/upload-media",
                data={"kind": "narration"},
                files={"media": ("voz.mp3", b"audio-bytes", "audio/mpeg")},
                follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 303)
            saved = webapp.find_input_media(project_id, "narration")
            self.assertIsNotNone(saved)
            self.assertEqual(saved.name, "narration.mp3")

            # extensao invalida e rejeitada
            bad = client.post(
                f"/projects/{project_id}/upload-media",
                data={"kind": "narration"},
                files={"media": ("voz.exe", b"x", "application/octet-stream")},
                follow_redirects=False,
            )
            self.assertEqual(bad.status_code, 400)

            # substituir por outra extensao remove a anterior
            client.post(
                f"/projects/{project_id}/upload-media",
                data={"kind": "narration"},
                files={"media": ("voz.wav", b"wav-bytes", "audio/wav")},
                follow_redirects=False,
            )
            saved = webapp.find_input_media(project_id, "narration")
            self.assertEqual(saved.name, "narration.wav")
            folder = webapp.project_inputs_dir(project_id)
            self.assertEqual(len(list(folder.glob("narration.*"))), 1)

            client.post(
                f"/projects/{project_id}/remove-media",
                data={"kind": "narration"},
                follow_redirects=False,
            )
            self.assertIsNone(webapp.find_input_media(project_id, "narration"))

    def test_new_project_audio_upload_becomes_narration_media(self) -> None:
        with TestClient(webapp.app) as client:
            uid = db.create_user("newmedia", "password123")
            client.post(
                "/login",
                data={"username": "newmedia", "password": "password123"},
                follow_redirects=False,
            )
            resp = client.post(
                "/projects/new",
                data={
                    "name": "proj com voz",
                    "script": "[00:00.0 - 00:03.0] teste",
                    "avatar_safe_area": "right",
                    "resolution": "1280x720",
                    "scene_duration": "4",
                },
                files={"narration_media": ("voz.mp3", b"audio-bytes", "audio/mpeg")},
                follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 303)

        projects = db.list_projects(uid)
        self.assertEqual(len(projects), 1)
        saved = webapp.find_input_media(projects[0]["id"], "narration")
        self.assertIsNotNone(saved)
        self.assertEqual(saved.name, "narration.mp3")

    def test_local_output_videos_separates_base_and_master(self) -> None:
        project_work = self.root / "pw"
        out = project_work / "kaggle_output"
        (out / "hyperframes_master" / "assets").mkdir(parents=True)
        (out / "video_broll_base.mp4").write_bytes(b"base")
        (out / "final_master.mp4").write_bytes(b"master")
        (out / "hyperframes_master" / "assets" / "video_broll_base.mp4").write_bytes(b"copy")

        outputs = webapp.local_output_videos(project_work)
        self.assertEqual(outputs["base"].name, "video_broll_base.mp4")
        self.assertEqual(outputs["base"].parent, out)
        self.assertEqual(outputs["master"].name, "final_master.mp4")

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


class HardeningAndOptimizationTest(unittest.TestCase):
    """Cobre as correcoes de seguranca, consistencia e otimizacao."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        webapp.DATA_DIR = self.root / "data"
        webapp.WORK_DIR = webapp.DATA_DIR / "work"
        db.DATA_DIR = webapp.DATA_DIR
        db.DB_PATH = webapp.DATA_DIR / "plataforma.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_safe_next_url_rejects_backslash_and_control_chars(self) -> None:
        # navegadores tratam '\' como '/', virando redirect externo //evil.com
        self.assertEqual(webapp.safe_next_url("/\\evil.com"), "/projects")
        self.assertEqual(webapp.safe_next_url("\\\\evil.com"), "/projects")
        self.assertEqual(webapp.safe_next_url("/ok\r\nLocation: x"), "/projects")
        self.assertEqual(webapp.safe_next_url("/projects/7"), "/projects/7")

    def test_fetch_requests_get_json_errors_not_login_html(self) -> None:
        with TestClient(webapp.app) as client:
            resp = client.post(
                "/assets/1/state",
                data={"state": "selected"},
                headers={"sec-fetch-mode": "cors"},
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 401)
        self.assertIn("detail", resp.json())

    def test_browser_navigation_still_redirects_to_login(self) -> None:
        with TestClient(webapp.app) as client:
            resp = client.get(
                "/projects",
                headers={"sec-fetch-mode": "navigate"},
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(resp.headers["location"].startswith("/login"))

    def test_verify_password_handles_corrupted_hash(self) -> None:
        self.assertFalse(db.verify_password("x", "sem-separador"))
        self.assertFalse(db.verify_password("x", "nao-hex$abc"))

    def test_stale_jobs_fail_on_startup(self) -> None:
        db.init_db()
        uid = db.create_user("stale", "password123")
        running = db.create_job(uid, "kaggle_send", None, "enviando")
        db.update_job(running, status="running")
        done = db.create_job(uid, "package", None, "ok")
        db.finish_job(done, "Pacote pronto")

        changed = db.fail_stale_jobs()

        self.assertEqual(changed, 1)
        self.assertEqual(db.get_job(running, uid)["status"], "error")
        self.assertEqual(db.get_job(done, uid)["status"], "complete")

    def test_resolve_model_replaces_decommissioned_groq_models(self) -> None:
        from services import groq_service

        self.assertEqual(groq_service.resolve_model("mixtral-8x7b-32768"), groq_service.DEFAULT_MODEL)
        self.assertEqual(groq_service.resolve_model("gemma2-9b-it"), groq_service.DEFAULT_MODEL)
        self.assertEqual(groq_service.resolve_model(""), groq_service.DEFAULT_MODEL)
        self.assertEqual(groq_service.resolve_model("llama-3.1-8b-instant"), "llama-3.1-8b-instant")
        for value, _label in groq_service.GROQ_MODELS:
            self.assertEqual(groq_service.resolve_model(value), value)

    def test_timestamp_parser_accepts_long_videos_over_99_minutes(self) -> None:
        scenes = parse_script("[105:00.0 - 105:04.5] cena tardia")
        self.assertEqual(len(scenes), 1)
        self.assertEqual(scenes[0]["start_time"], 6300.0)
        self.assertEqual(scenes[0]["duration"], 4.5)

    def test_list_assets_for_project_matches_per_scene_listing(self) -> None:
        db.init_db()
        uid = db.create_user("bulk", "password123")
        project_id = db.create_project(uid, "bulk", "script", {})
        db.replace_scenes(
            project_id,
            [
                {"scene_id": "scene_001", "idx": 1, "start_time": 0, "end_time": 4, "duration": 4},
                {"scene_id": "scene_002", "idx": 2, "start_time": 4, "end_time": 8, "duration": 4},
            ],
        )
        scenes = db.list_scenes(project_id)
        db.add_assets(scenes[0]["id"], [
            {"source": "pexels", "download_url": "https://example.com/a.mp4"},
            {"source": "pixabay", "download_url": "https://example.com/b.mp4"},
        ])
        db.add_assets(scenes[1]["id"], [
            {"source": "pexels", "download_url": "https://example.com/c.mp4"},
        ])

        grouped = db.list_assets_for_project(project_id)

        for scene in scenes:
            self.assertEqual(grouped.get(scene["id"], []), db.list_assets(scene["id"]))

    def test_search_scene_parallel_keeps_order_and_dedupes(self) -> None:
        def fake(source, kw, n):
            return [
                {"source": source, "download_url": f"https://{source}.example/{kw}/{i}", "keyword": kw}
                for i in range(n)
            ]

        with patch.object(asset_search, "search_pexels_videos", lambda kw, *_a, **_k: fake("pexels", kw, 2)), \
             patch.object(asset_search, "search_pixabay_videos", lambda kw, *_a, **_k: fake("pixabay", kw, 2)):
            results = asset_search.search_scene(
                ["kw1", "kw2"], "pk", "bk", max_w=1920, per_keyword=2, media="video"
            )

        urls = [r["download_url"] for r in results]
        self.assertEqual(urls, [
            "https://pexels.example/kw1/0", "https://pexels.example/kw1/1",
            "https://pixabay.example/kw1/0", "https://pixabay.example/kw1/1",
            "https://pexels.example/kw2/0", "https://pexels.example/kw2/1",
            "https://pixabay.example/kw2/0", "https://pixabay.example/kw2/1",
        ])

        # dedupe contra URLs ja vistas
        seen = {"https://pexels.example/kw1/0"}
        with patch.object(asset_search, "search_pexels_videos", lambda kw, *_a, **_k: fake("pexels", kw, 1)), \
             patch.object(asset_search, "search_pixabay_videos", lambda *_a, **_k: []):
            results = asset_search.search_scene(
                ["kw1"], "pk", "bk", max_w=1920, per_keyword=1, media="video", seen_urls=seen
            )
        self.assertEqual(results, [])

    def test_package_route_fails_job_on_unexpected_error(self) -> None:
        original_build = webapp.packager.build_zip
        try:
            with TestClient(webapp.app) as client:
                uid = db.create_user("pkg-err", "password123")
                project_id = db.create_project(uid, "pkg", "script", {})
                db.replace_scenes(
                    project_id,
                    [{"scene_id": "scene_001", "idx": 1, "start_time": 0, "end_time": 4, "duration": 4}],
                )
                scene = db.list_scenes(project_id)[0]
                db.add_assets(scene["id"], [
                    {"source": "pexels", "download_url": "https://example.com/a.mp4"},
                ])
                asset = db.list_assets(scene["id"])[0]
                db.set_asset_state(asset["id"], "selected")
                client.post(
                    "/login",
                    data={"username": "pkg-err", "password": "password123"},
                    follow_redirects=False,
                )

                def boom(*_args, **_kwargs):
                    raise ValueError("falha inesperada de disco")

                webapp.packager.build_zip = boom
                resp = client.post(f"/projects/{project_id}/package", follow_redirects=False)
                jobs = db.list_project_jobs(project_id, uid)
                project = db.get_project(project_id, uid)
        finally:
            webapp.packager.build_zip = original_build

        self.assertEqual(resp.status_code, 502)
        self.assertEqual(project["status"], "package_failed")
        self.assertEqual(jobs[0]["status"], "error")
        self.assertIn("falha inesperada", jobs[0]["error"])

    def test_kaggle_status_requires_credentials(self) -> None:
        with TestClient(webapp.app) as client:
            uid = db.create_user("no-keys", "password123")
            project_id = db.create_project(uid, "proj", "script", {})
            db.update_kaggle_job(project_id, "dataset", "kernel", "queued")
            client.post(
                "/login",
                data={"username": "no-keys", "password": "password123"},
                follow_redirects=False,
            )
            resp = client.get(f"/projects/{project_id}/kaggle-status")

        payload = resp.json()
        self.assertEqual(payload["status"], "error")
        self.assertIn("/settings", payload["error"])

    def test_project_page_shows_hyperframes_fallback_status(self) -> None:
        with TestClient(webapp.app) as client:
            uid = db.create_user("hf-fb", "password123")
            project_id = db.create_project(uid, "proj", "script", {})
            out_dir = webapp.WORK_DIR / f"project_{project_id}" / "kaggle_output"
            out_dir.mkdir(parents=True)
            (out_dir / "hyperframes_status.json").write_text(
                json.dumps({"status": "fallback_complete", "audio": True, "avatar": False}),
                encoding="utf-8",
            )
            client.post(
                "/login",
                data={"username": "hf-fb", "password": "password123"},
                follow_redirects=False,
            )
            resp = client.get(f"/projects/{project_id}")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("fallback FFmpeg", resp.text)

    def test_kaggle_slugs_are_unique_per_project(self) -> None:
        # dois projetos com o mesmo nome nao podem compartilhar dataset/kernel
        self.assertNotEqual(
            kaggle_service.dataset_slug("Meu Video", 1),
            kaggle_service.dataset_slug("Meu Video", 2),
        )
        self.assertNotEqual(
            kaggle_service.kernel_slug("Meu Video", 1),
            kaggle_service.kernel_slug("Meu Video", 2),
        )
        # sem project_id mantem o formato antigo (compatibilidade)
        self.assertEqual(kaggle_service.dataset_slug("Meu Video"), "brolls-meu-video")
        self.assertEqual(kaggle_service.kernel_slug("Meu Video"), "b-rolls-render-meu-video")
        # slugs continuam validos para o Kaggle (lowercase, <= 50 chars)
        long_name = "Projeto com um nome extremamente longo para testar truncamento"
        for slug in [kaggle_service.dataset_slug(long_name, 123), kaggle_service.kernel_slug(long_name, 123)]:
            self.assertLessEqual(len(slug), 50)
            self.assertRegex(slug, r"^[a-z0-9][a-z0-9-]*$")

    def test_packager_parallel_download_keeps_scene_order(self) -> None:
        project = {"name": "Ordem"}
        config = {"avatar_safe_area": "right", "resolution": "1920x1080", "format": "16:9"}
        scenes = []
        selected = {}
        for i in range(1, 4):
            scenes.append({
                "id": i, "scene_id": f"scene_{i:03d}", "idx": i, "zone": "DESENVOLVIMENTO",
                "start_time": float(i - 1) * 4, "end_time": float(i) * 4, "duration": 4.0,
                "narration": f"cena {i}", "visual_goal": "", "keywords": [],
                "must_show": [], "must_not_show": [], "asset_type": "video",
                "overlay_text": "", "avatar_safe_area": "right",
            })
            selected[i] = {
                "source": "pexels",
                "download_url": f"https://example.com/{i}.mp4",
                "asset_type": "video",
                "keyword": f"kw{i}",
            }

        def fake_download(_url, dest, _max_bytes):
            dest.write_bytes(b"fake-video")
            return True

        original_download = packager._download
        packager._download = fake_download
        try:
            zip_path = packager.build_zip(project, config, scenes, selected, [], self.root / "work")
        finally:
            packager._download = original_download

        with zipfile.ZipFile(zip_path) as zf:
            sources = json.loads(zf.read("metadata/pexels_sources.json"))
            guide = json.loads(zf.read("guia_visual.json"))
        self.assertEqual([s["scene_id"] for s in sources], ["scene_001", "scene_002", "scene_003"])
        self.assertTrue(all(s["selected_asset"] for s in guide["scenes"]))


if __name__ == "__main__":
    unittest.main()
