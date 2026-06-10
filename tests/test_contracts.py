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
from services import asset_search, kaggle_service, packager  # noqa: E402
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

    def test_pull_output_video_prefers_hyperframes_master(self) -> None:
        original_run = kaggle_service._run
        try:
            def fake_run(args, _username, _token, **_kwargs):
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
        # camadas da composicao master
        self.assertIn("slow_push_in", runner)
        self.assertIn("slow_pull_out", runner)
        self.assertIn('class="clip caption pos-', runner)
        self.assertIn('class="clip fadeov"', runner)
        self.assertIn('data-has-audio="true"', runner)
        self.assertIn('class="clip avatar-clip pos-', runner)
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
        motions = [s["motion"] for s in plan["scenes"]]
        self.assertEqual(motions, ["slow_push_in", "slow_pull_out", "slow_push_in"])
        transitions = [s["transition_out"] for s in plan["scenes"]]
        self.assertEqual(transitions, ["fade", "fade", "none"])
        self.assertEqual(plan["scenes"][0]["caption"], "Abertura")
        self.assertEqual(plan["scenes"][1]["caption"], "")

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


if __name__ == "__main__":
    unittest.main()
