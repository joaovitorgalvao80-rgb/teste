from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["APP_ENV"] = "dev"

import app as webapp  # noqa: E402
import app_shared  # noqa: E402
import database as db  # noqa: E402
import montador  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from services import asset_search, diagnostics, kaggle_service, ops_status, packager  # noqa: E402
from services.script_parser import parse_script  # noqa: E402
from tools import preflight  # noqa: E402


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

    def test_package_route_requires_at_least_one_selected_broll(self) -> None:
        with TestClient(webapp.app) as client:
            user_id = db.create_user("partial", "password123")
            project_id = db.create_project(user_id, "partial project", "script", {})
            # 3 cenas: a do meio (scene_002) leva b-roll; 1a e ultima sao avatar.
            # Selecionamos scene_001 e deixamos a de b-roll (scene_002) sem take.
            db.replace_scenes(
                project_id,
                [
                    {"scene_id": "scene_001", "idx": 1, "zone": "GANCHO",
                     "start_time": 0, "end_time": 4, "duration": 4, "narration": "um"},
                    {"scene_id": "scene_002", "idx": 2, "zone": "DESENVOLVIMENTO",
                     "start_time": 4, "end_time": 8, "duration": 4,
                     "narration": "o mosquito se reproduz na agua parada do quintal"},
                    {"scene_id": "scene_003", "idx": 3, "zone": "CTA",
                     "start_time": 8, "end_time": 12, "duration": 4, "narration": "tres"},
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
        self.assertIn("ao menos um asset", resp.text)

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
        self.assertIn("Saúde do projeto", resp.text)
        self.assertIn("Jobs recentes", resp.text)
        self.assertIn("Estado operacional", resp.text)
        self.assertIn("package", resp.text)
        self.assertIn("Parar", resp.text)
        self.assertIn("atualizado", resp.text)

    def test_project_page_shows_api_usage_and_problem_scenes(self) -> None:
        with TestClient(webapp.app) as client:
            user_id = db.create_user("usage", "password123")
            project_id = db.create_project(
                user_id,
                "usage project",
                "script",
                {"image_fallback": True, "resolution": "1920x1080"},
            )
            db.replace_scenes(
                project_id,
                [
                    {"scene_id": "scene_001", "idx": 1, "start_time": 0, "end_time": 4, "duration": 4, "narration": "Oi, eu sou o apresentador."},
                    {"scene_id": "scene_002", "idx": 2, "start_time": 4, "end_time": 8, "duration": 4, "narration": "Mostre larvas de mosquito na agua.", "visual_goal": "mosquito larvae in water", "keywords": ["mosquito larvae"]},
                    {"scene_id": "scene_003", "idx": 3, "start_time": 8, "end_time": 12, "duration": 4, "narration": "Obrigado por assistir."},
                ],
            )
            scene = db.list_scenes(project_id)[1]
            db.add_assets(
                scene["id"],
                [{
                    "source": "pexels",
                    "asset_type": "video",
                    "download_url": "https://example.com/chicken.mp4",
                    "preview_url": "https://example.com/chicken.jpg",
                    "width": 640,
                    "height": 360,
                    "duration": 1,
                    "keyword": "chicken eggs cooking",
                }],
            )
            asset = db.list_assets(scene["id"])[0]
            db.set_asset_state(asset["id"], "selected")
            db.record_api_usage(user_id, project_id, None, "pexels", "video_search", status_code=200, ok=True, latency_ms=120)
            db.record_api_usage(user_id, project_id, None, "groq", "generate_briefs", status_code=429, ok=False, latency_ms=600)
            client.post(
                "/login",
                data={"username": "usage", "password": "password123"},
                follow_redirects=False,
            )
            page = client.get(f"/projects/{project_id}")
            diag = client.get(f"/projects/{project_id}/diagnostics.json")

        self.assertEqual(page.status_code, 200)
        self.assertIn("Consumo de API", page.text)
        self.assertIn("Cenas para revisar", page.text)
        self.assertIn("scene_002", page.text)
        self.assertIn("pexels", page.text)
        payload = diag.json()
        self.assertEqual(payload["api_usage"]["total_calls"], 2)
        self.assertEqual(payload["api_usage"]["failed_calls"], 1)
        self.assertEqual(payload["problem_scenes"][0]["scene_id"], "scene_002")

    def test_settings_integration_status_is_masked(self) -> None:
        with TestClient(webapp.app) as client:
            user_id = db.create_user("masked", "password123")
            db.update_api_keys(
                user_id,
                "pexels-secret-value",
                "pixabay-secret-value",
                "groq-secret-value",
                "llama-3.3-70b-versatile",
                coverr="coverr-secret-value",
                nvidia="nvidia-secret-value",
            )
            db.update_kaggle_keys(user_id, "kaggle-user", "kaggle-secret-token")
            client.post(
                "/login",
                data={"username": "masked", "password": "password123"},
                follow_redirects=False,
            )
            page = client.get("/settings")
            status = client.get("/settings/integrations-status")

        self.assertEqual(page.status_code, 200)
        self.assertIn("Status das integracoes", page.text)
        self.assertIn("Consumo de API", page.text)
        self.assertIn("Pexels: configurado", page.text)
        self.assertNotIn("pexels-secret-value", page.text)
        self.assertEqual(status.status_code, 200)
        payload = status.json()
        self.assertTrue(payload["ready"])
        dumped = json.dumps(payload)
        self.assertNotIn("pexels-secret-value", dumped)
        self.assertNotIn("kaggle-secret-token", dumped)

    def test_job_cancel_marks_only_the_requested_active_job(self) -> None:
        with TestClient(webapp.app) as client:
            user_id = db.create_user("cancel-owner", "password123")
            project_id = db.create_project(user_id, "cancel project", "script", {})
            first_job = db.create_job(user_id, "search_assets", project_id, "Busca na fila")
            second_job = db.create_job(user_id, "package", project_id, "Pacote na fila")
            client.post(
                "/login",
                data={"username": "cancel-owner", "password": "password123"},
                follow_redirects=False,
            )
            resp = client.post(f"/jobs/{first_job}/cancel")

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["id"], first_job)
        self.assertEqual(payload["status"], "canceling")
        self.assertEqual(db.get_job(first_job, user_id)["status"], "canceling")
        self.assertEqual(db.get_job(second_job, user_id)["status"], "queued")

    @staticmethod
    def _tiny_png(width: int = 1024, height: int = 576) -> bytes:
        """PNG minimo com assinatura + IHDR suficiente para o validador."""
        return (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + width.to_bytes(4, "big")
            + height.to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00" + b"\x00" * 16
        )

    def _project_with_scene(self, username: str) -> tuple[int, int, dict]:
        user_id = db.create_user(username, "password123")
        project_id = db.create_project(user_id, f"{username} project", "script", {})
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
        return user_id, project_id, db.list_scenes(project_id)[0]

    def test_generated_image_upload_serves_and_blocks_other_users(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id, scene = self._project_with_scene("genowner")
            db.create_user("genother", "password123")
            client.post(
                "/login",
                data={"username": "genowner", "password": "password123"},
                follow_redirects=False,
            )
            resp = client.post(
                f"/scenes/{scene['id']}/generated-image",
                files={"image": ("generated.png", self._tiny_png(), "image/png")},
                data={"prompt": "peixe tropical", "width": "0", "height": "0"},
            )
            self.assertEqual(resp.status_code, 200)
            asset = db.list_assets(scene["id"])[0]
            self.assertEqual(asset["source"], "generated")
            self.assertEqual(asset["asset_type"], "image")
            self.assertEqual(asset["width"], 1024)
            self.assertEqual(asset["height"], 576)
            self.assertEqual(asset["keyword"], "peixe tropical")

            served = client.get(asset["download_url"])
            self.assertEqual(served.status_code, 200)
            self.assertEqual(served.headers["content-type"], "image/png")

            traversal = client.get(f"/projects/{project_id}/generated/..%2F..%2Fplataforma.db")
            self.assertNotEqual(traversal.status_code, 200)

            client.post(
                "/login",
                data={"username": "genother", "password": "password123"},
                follow_redirects=False,
            )
            stolen = client.get(asset["download_url"])
        self.assertEqual(stolen.status_code, 404)

    def test_generated_image_rejects_non_image_payload(self) -> None:
        with TestClient(webapp.app) as client:
            _, _, scene = self._project_with_scene("genbad")
            client.post(
                "/login",
                data={"username": "genbad", "password": "password123"},
                follow_redirects=False,
            )
            resp = client.post(
                f"/scenes/{scene['id']}/generated-image",
                files={"image": ("fake.png", b"<html>not an image</html>", "image/png")},
                data={"prompt": "x"},
            )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(db.list_assets(scene["id"]), [])

    def test_manual_curation_ui_has_explicit_auto_select_and_review_image_generation(self) -> None:
        with TestClient(webapp.app) as client:
            uid = db.create_user("manual-ui", "password123")
            project_id = db.create_project(uid, "manual-ui project", "script", {})
            db.replace_scenes(
                project_id,
                [
                    {"scene_id": "scene_001", "idx": 1, "zone": "ABERTURA", "start_time": 0, "end_time": 4, "duration": 4, "narration": "abertura"},
                    {"scene_id": "scene_002", "idx": 2, "zone": "CONTEUDO", "start_time": 4, "end_time": 8, "duration": 4, "narration": "conteudo visual"},
                    {"scene_id": "scene_003", "idx": 3, "zone": "CTA", "start_time": 8, "end_time": 12, "duration": 4, "narration": "fechamento"},
                ],
            )
            scene = db.list_scenes(project_id)[1]
            db.add_assets(scene["id"], [{"source": "pexels", "download_url": "https://example.com/a.mp4"}])
            asset = db.list_assets(scene["id"])[0]
            db.set_project_status(project_id, "searched")
            client.post(
                "/login",
                data={"username": "manual-ui", "password": "password123"},
                follow_redirects=False,
            )

            project_page = client.get(f"/projects/{project_id}")
            db.set_asset_state(asset["id"], "selected")
            db.set_project_status(project_id, "reviewing")
            review_page = client.get(f"/projects/{project_id}/review")

        self.assertEqual(project_page.status_code, 200)
        self.assertIn("Buscar assets", project_page.text)
        self.assertNotIn("Buscar + selecionar", project_page.text)
        self.assertIn(f'action="/projects/{project_id}/auto-select"', project_page.text)
        self.assertIn("Seleção automática", project_page.text)
        self.assertIn('id="curadoria"', project_page.text)
        self.assertIn("refreshAssets", project_page.text)
        self.assertIn("atualizar", project_page.text)
        self.assertEqual(review_page.status_code, 200)
        self.assertIn("toggleGenPanel", review_page.text)
        self.assertIn(f"gen-panel-{scene['id']}", review_page.text)
        self.assertIn(f"generateImage({scene['id']}", review_page.text)

    def test_packager_copies_generated_asset_from_disk(self) -> None:
        work_dir = self.root / "work" / "project_1"
        gen_dir = work_dir / "generated"
        gen_dir.mkdir(parents=True)
        name = "gen_" + "a" * 32 + ".png"
        (gen_dir / name).write_bytes(self._tiny_png())
        dest = self.root / "copied.png"
        asset = {"source": "generated", "download_url": f"/projects/1/generated/{name}"}

        ok = packager._copy_generated(asset, work_dir, dest, max_bytes=10 * 1024 * 1024)
        self.assertTrue(ok)
        self.assertTrue(dest.is_file())

        missing = {"source": "generated", "download_url": "/projects/1/generated/gen_nao_existe.png"}
        self.assertFalse(
            packager._copy_generated(missing, work_dir, self.root / "nope.png", max_bytes=1024)
        )

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
        self.assertIn("operational_state", payload)
        self.assertIn("jobs", payload)

    def test_project_jobs_endpoint_includes_operational_timing(self) -> None:
        with TestClient(webapp.app) as client:
            user_id = db.create_user("jobtime", "password123")
            project_id = db.create_project(user_id, "jobtime project", "script", {})
            db.create_job(user_id, "search_assets", project_id, "Busca na fila")
            client.post(
                "/login",
                data={"username": "jobtime", "password": "password123"},
                follow_redirects=False,
            )
            resp = client.get(f"/projects/{project_id}/jobs")

        self.assertEqual(resp.status_code, 200)
        job = resp.json()["jobs"][0]
        self.assertIn("elapsed_label", job)
        self.assertIn("updated_label", job)

    def test_ops_status_project_state_selects_next_action(self) -> None:
        state = ops_status.project_state(
            {"status": "created"},
            scenes=[],
            asset_count=0,
            curation_stats={"required": 0, "selected": 0, "accepted": 0},
            jobs=[],
            parts=[],
            outputs={"base": None, "master": None},
            diagnostics={},
            has_asset_keys=False,
        )
        self.assertEqual(state["code"], "needs_map")

    def test_manual_search_results_sort_above_automatic_candidates(self) -> None:
        automatic = {
            "id": 10,
            "state": "pending",
            "query_role": "primary",
            "vision_score": 95,
            "relevance": 0.95,
        }
        manual = {
            "id": 11,
            "state": "pending",
            "query_role": "manual_image_primary",
            "vision_score": 40,
            "relevance": 0.4,
        }
        ranked = sorted([automatic, manual], key=webapp._take_sort_key, reverse=True)
        self.assertEqual(ranked[0], manual)

    def test_preflight_lists_release_gates(self) -> None:
        names = [check.name for check in preflight.planned_checks()]
        self.assertIn("unit contracts", names)
        self.assertIn("embedded kaggle runner", names)
        self.assertIn("smoke review flow", names)
        self.assertIn("smoke long mode", names)
        self.assertIn("frontend js syntax", names)
        self.assertIn("git whitespace", names)

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

    def test_packager_turns_unselected_broll_scenes_into_avatar_segments(self) -> None:
        project = {"name": "Parcial"}
        config = {"avatar_safe_area": "right", "resolution": "1920x1080", "format": "16:9"}
        scenes = [
            {
                "id": 1,
                "scene_id": "scene_001",
                "idx": 1,
                "zone": "DESENVOLVIMENTO",
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
                "broll": True,
            },
            {
                "id": 2,
                "scene_id": "scene_002",
                "idx": 2,
                "zone": "DESENVOLVIMENTO",
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
                "broll": True,
            },
        ]
        selected = {
            1: {
                "source": "pexels",
                "download_url": "https://example.com/a.mp4",
                "asset_type": "video",
                "keyword": "test",
            }
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
            guide = json.loads(zf.read("guia_visual.json"))
        self.assertTrue(guide["scenes"][0]["broll"])
        self.assertIsNotNone(guide["scenes"][0]["selected_asset"])
        self.assertFalse(guide["scenes"][1]["broll"])
        self.assertIsNone(guide["scenes"][1]["selected_asset"])

    def test_packager_marks_selected_video_for_frame_sampling(self) -> None:
        project = {"name": "Frames"}
        config = {"avatar_safe_area": "right", "resolution": "1920x1080", "format": "16:9"}
        scenes = [
            {
                "id": 1,
                "scene_id": "scene_001",
                "idx": 1,
                "zone": "DESENVOLVIMENTO",
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
                "broll": True,
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

        def fake_download(_url, dest, _max_bytes):
            dest.write_bytes(b"fake-video")
            return True

        original_download = packager._download
        packager._download = fake_download
        try:
            zip_path = packager.build_zip(project, config, scenes, selected, [], self.root / "work")
            with zipfile.ZipFile(zip_path) as zf:
                guide = json.loads(zf.read("guia_visual.json"))
                licenses = zf.read("LICENSES.md").decode("utf-8")
        finally:
            packager._download = original_download

        policy = guide["scenes"][0]["video_frame_sampling"]
        self.assertTrue(policy["enabled"])
        self.assertEqual(policy["max_frames"], 3)
        self.assertEqual(policy["positions"], [0.25, 0.5, 0.75])
        self.assertIn("metadata/video_frame_samples.json", licenses)

    def test_local_video_frame_samples_analyzes_downloaded_frames_with_groq(self) -> None:
        db.init_db()
        user_id = db.create_user("frame-user", "password123")
        db.update_api_keys(user_id, "", "", "gsk-test")
        project_id = db.create_project(
            user_id,
            "Frame Vision",
            "script",
            {"resolution": "1920x1080", "video_theme": "mosquito control"},
        )
        db.replace_scenes(
            project_id,
            [
                {
                    "scene_id": "scene_001",
                    "idx": 1,
                    "zone": "DESENVOLVIMENTO",
                    "start_time": 0.0,
                    "end_time": 4.0,
                    "duration": 4.0,
                    "narration": "mosquito perto da agua parada",
                    "visual_goal": "mosquito near stagnant water",
                    "keywords": ["mosquito water"],
                    "must_show": ["mosquito", "water"],
                    "must_not_show": ["bird"],
                    "asset_type": "video",
                    "overlay_text": "",
                    "avatar_safe_area": "right",
                }
            ],
        )
        project_work = webapp.WORK_DIR / f"project_{project_id}"
        frames_dir = project_work / "kaggle_output" / "video_frame_samples"
        meta_dir = project_work / "kaggle_output" / "metadata"
        frames_dir.mkdir(parents=True)
        meta_dir.mkdir(parents=True)
        (frames_dir / "scene_001_frame_01.jpg").write_bytes(b"\xff\xd8fake-jpg\xff\xd9")
        manifest = meta_dir / "video_frame_samples.json"
        manifest.write_text(
            json.dumps(
                {
                    "status": "sampled",
                    "samples": [
                        {
                            "scene_id": "scene_001",
                            "selected_asset": "assets/scene_001.mp4",
                            "sampled_frames": 1,
                            "frames": [
                                {"time": 1.0, "file": "video_frame_samples/scene_001_frame_01.jpg"}
                            ],
                        }
                    ],
                    "errors": [],
                }
            ),
            encoding="utf-8",
        )

        class FakeProvider:
            name = "groq-vision"

            def analyze(self, _asset, _scene, _config):
                return SimpleNamespace(
                    score=91.0,
                    verdict="otimo",
                    reasons=["mostra mosquito e agua"],
                    flags=[],
                    provider=self.name,
                )

        original_provider = app_shared.vision.get_provider
        try:
            app_shared.vision.get_provider = lambda *_args, **_kwargs: FakeProvider()
            summary = app_shared.local_video_frame_samples(
                project_work,
                project_id,
                db.get_user(user_id),
            )
        finally:
            app_shared.vision.get_provider = original_provider

        updated = json.loads(manifest.read_text(encoding="utf-8"))
        sample = updated["samples"][0]
        self.assertEqual(summary["vision_status"], "analyzed")
        self.assertEqual(summary["analyzed_frames"], 1)
        self.assertEqual(sample["video_frame_verdict"], "otimo")
        self.assertEqual(sample["frame_vision"][0]["provider"], "groq-vision")

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
                    meta = out_dir / "metadata"
                    meta.mkdir()
                    (meta / "video_frame_samples.json").write_text(
                        json.dumps(
                            {
                                "status": "sampled",
                                "samples": [
                                    {
                                        "scene_id": "scene_001",
                                        "sampled_frames": 3,
                                        "frames": [
                                            {"time": 1.0, "file": "video_frame_samples/scene_001_frame_01.jpg"}
                                        ],
                                    }
                                ],
                                "errors": [],
                            }
                        ),
                        encoding="utf-8",
                    )
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
        self.assertEqual(payload["video_frame_samples"]["status"], "sampled")
        self.assertEqual(payload["video_frame_samples"]["sampled_videos"], 1)
        self.assertEqual(payload["video_frame_samples"]["sampled_frames"], 3)
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

    def test_output_validation_flags_requested_avatar_missing_as_error(self) -> None:
        project_work = self.root / "project"
        out = project_work / "kaggle_output"
        out.mkdir(parents=True)
        (out / "final_master.mp4").write_bytes(b"not-a-real-mp4")
        (out / "hyperframes_status.json").write_text(
            json.dumps({"requested_avatar": True, "avatar": False}),
            encoding="utf-8",
        )

        payload = diagnostics.validate_outputs(project_work, expected_duration=0)

        self.assertEqual(payload["status"], "error")
        messages = [issue["message"] for issue in payload["issues"]]
        self.assertTrue(any("Avatar foi solicitado" in msg for msg in messages))

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
                kaggle_service._validated_kaggle_args(args)
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
        self.assertIn("video_frame_samples", pattern)

    def test_kaggle_arg_validator_accepts_output_file_pattern(self) -> None:
        pattern = (
            r"(final_master\.mp4|video_broll_base\.mp4|base_broll\.mp4|"
            r"metadata[/\\]video_frame_samples\.json|video_frame_samples[/\\].*\.jpg)$"
        )

        args = kaggle_service._validated_kaggle_args(["kernels", "output", "user/kernel", "--file-pattern", pattern])

        self.assertEqual(args[-1], pattern)
        with self.assertRaisesRegex(RuntimeError, "Argumento invalido"):
            kaggle_service._validated_kaggle_args(["kernels", "output", "user/kernel;rm"])

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
        self.assertIn('HYPERFRAMES_VERSION = (os.environ.get("PRODUCER_HYPERFRAMES_VERSION") or "0.6.93").strip() or "0.6.93"', kaggle_service._RUNNER)
        self.assertIn('HYPERFRAMES_PACKAGE = "hyperframes@" + HYPERFRAMES_VERSION', kaggle_service._RUNNER)
        self.assertIn('window.__timelines["nwrch-master"]', kaggle_service._RUNNER)
        self.assertIn('"apt-get", "install"', kaggle_service._RUNNER)
        self.assertIn('"libatk1.0-0"', kaggle_service._RUNNER)
        self.assertNotIn('"chromium-browser"', kaggle_service._RUNNER)
        self.assertIn('"HYPERFRAMES_BROWSER_PATH"', kaggle_service._RUNNER)
        self.assertIn('"PUPPETEER_EXECUTABLE_PATH"', kaggle_service._RUNNER)
        self.assertIn('"--workers"', kaggle_service._RUNNER)
        self.assertIn("RENDER_WORKERS = 1", kaggle_service._RUNNER)
        self.assertIn('"PRODUCER_LOW_MEMORY_MODE"', kaggle_service._RUNNER)
        self.assertIn('"PRODUCER_HF_RENDER_MODE"', kaggle_service._RUNNER)
        self.assertIn('"PRODUCER_HF_ENABLE_CAPTIONS"', kaggle_service._RUNNER)
        self.assertIn('"PRODUCER_HF_MP4_TIMEOUT_SECONDS"', kaggle_service._RUNNER)
        self.assertIn('"PRODUCER_HF_PNG_TIMEOUT_SECONDS"', kaggle_service._RUNNER)
        self.assertIn('"PRODUCER_PLAYER_READY_TIMEOUT_MS"', kaggle_service._RUNNER)
        self.assertIn('"--low-memory-mode"', kaggle_service._RUNNER)
        self.assertIn('"--protocol-timeout"', kaggle_service._RUNNER)
        self.assertIn('"900000"', kaggle_service._RUNNER)
        self.assertIn('"--browser-timeout"', kaggle_service._RUNNER)
        self.assertIn('"180"', kaggle_service._RUNNER)
        self.assertIn('"--player-ready-timeout"', kaggle_service._RUNNER)
        self.assertIn('"png-sequence"', kaggle_service._RUNNER)
        self.assertIn("hyperframes_frames", kaggle_service._RUNNER)
        self.assertIn("sample_finalist_video_frames(source)", kaggle_service._RUNNER)
        self.assertIn("video_frame_samples", kaggle_service._RUNNER)
        self.assertIn('"format=yuv420p"', kaggle_service._RUNNER)
        self.assertIn('"png-sequence+ffmpeg"', kaggle_service._RUNNER)
        self.assertIn('"performance": RUN_TIMINGS', kaggle_service._RUNNER)
        self.assertIn("data-duration=", kaggle_service._RUNNER)

    def test_runner_composition_uses_edit_plan_extras(self) -> None:
        runner = kaggle_service._RUNNER
        compile(runner, "runner.py", "exec")
        self.assertIn("find_pack_extras(input_root)", runner)
        self.assertIn("ensure_avatar_contract(edit_plan, avatar)", runner)
        self.assertIn("assert_avatar_satisfied(postprocess)", runner)
        self.assertIn('"edit_plan.json"', runner)
        self.assertIn("render_hyperframes_master(out, edit_plan, narration_file, avatar_file)", runner)
        self.assertIn("apply_master_postprocess(master_out, narration, avatar, edit_plan, avatar_mode=avatar_mode)", runner)
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

    def test_runner_avatar_base_composition_and_broll_windows(self) -> None:
        runner = kaggle_service._RUNNER
        compile(runner, "runner.py", "exec")
        # avatar e a base do video; b-rolls entram por cima em janelas
        self.assertIn('class="clip avatar-base"', runner)
        self.assertIn('class="clip broll-clip"', runner)
        self.assertIn("broll_windows(scenes, duration, base_dur)", runner)
        self.assertIn("enforce_avatar_solo_guard", runner)
        self.assertIn("MAX_AVATAR_SOLO_SECONDS = 30.0", runner)
        # fix do "pulo duplo" no inicio de cena (fade + zoom)
        self.assertIn("immediateRender: false", runner)
        # audio nunca e cortado no final: video estende com tpad
        self.assertIn("tpad=stop_mode=clone", runner)
        self.assertNotIn('"-shortest"', runner)
        # fallback sem HyperFrames mantem o avatar como base
        self.assertIn("plan_avatar_mode(edit_plan, avatar_file)", runner)
        self.assertIn("avatar_audio", runner)
        # corner tambem evita video pesado no browser: FFmpeg compoe, HyperFrames so overlay
        self.assertIn('avatar_mode in ("base", "corner") and avatar', runner)
        self.assertIn("ffmpeg_compose_corner_layers", runner)
        self.assertIn("has_overlays = result", runner)
        # legendas via FFmpeg drawtext (sem Chrome), ligadas por padrao e com
        # fallback seguro pra base composta se algo falhar
        self.assertIn('captions_enabled = env_enabled("PRODUCER_HF_ENABLE_CAPTIONS", False)', runner)
        self.assertIn("ffmpeg_drawtext_captions(composed_base, edit_plan, duration, master_out)", runner)
        self.assertIn("caption-fallback", runner)
        self.assertIn('"ffmpeg-compose"', runner)
        self.assertIn('elif text_overlay_only:', runner)

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
        self.assertEqual(plan["version"], 2)
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

    def test_presentation_scenes_never_get_broll(self) -> None:
        from services import edit_plan as ep

        project = {"name": "V"}
        config = {"avatar_safe_area": "right", "resolution": "1280x720"}
        scenes = [
            {"scene_id": "scene_001", "start_time": 0.0, "end_time": 5.0, "duration": 5.0,
             "narration": "Olá, eu sou o Valdir e hoje vou te ensinar"},
            {"scene_id": "scene_002", "start_time": 5.0, "end_time": 10.0, "duration": 5.0,
             "narration": "O mosquito da dengue se reproduz na agua parada do quintal"},
            {"scene_id": "scene_003", "start_time": 10.0, "end_time": 15.0, "duration": 5.0,
             "narration": "Meu nome e Valdir e quero te mostrar uma solucao"},
            {"scene_id": "scene_004", "start_time": 15.0, "end_time": 20.0, "duration": 5.0,
             "narration": "Os ovos resistem meses ate a primeira chuva chegar"},
        ]
        plan = ep.build_edit_plan(project, config, scenes)
        broll = {s["scene_id"]: s["broll"] for s in plan["scenes"]}
        self.assertFalse(broll["scene_001"], "apresentacao nao leva b-roll")
        self.assertFalse(broll["scene_003"], "'meu nome e' nao leva b-roll")
        # cenas de conteudo seguem elegiveis a b-roll
        self.assertTrue(ep._is_presentation("Olá, eu sou o Valdir"))
        self.assertFalse(ep._is_presentation("O mosquito se reproduz na agua parada"))

    def test_decide_broll_marks_avatar_only_scenes(self) -> None:
        from services import edit_plan as ep
        scenes = [
            {"scene_id": "s1", "start_time": 0, "end_time": 5, "duration": 5,
             "narration": "Olá, eu sou o Valdir e hoje vou te ensinar"},
            {"scene_id": "s2", "start_time": 5, "end_time": 10, "duration": 5,
             "narration": "o mosquito se reproduz na agua parada do quintal"},
            {"scene_id": "s3", "start_time": 10, "end_time": 15, "duration": 5,
             "narration": "os ovos resistem meses ate a primeira chuva"},
            {"scene_id": "s4", "start_time": 15, "end_time": 20, "duration": 5,
             "narration": "obrigado por assistir, ate a proxima"},
        ]
        flags = ep.decide_broll(scenes)
        self.assertFalse(flags[0], "1a cena (apresentacao) e avatar-only")
        self.assertFalse(flags[-1], "ultima cena e avatar-only")
        self.assertTrue(any(flags[1:3]), "cenas de conteudo levam b-roll")

    def test_broll_override_forces_broll_over_presentation(self) -> None:
        """Override 'sem avatar' (1) vence ate a deteccao de apresentacao."""
        from services import edit_plan as ep
        scenes = [
            {"scene_id": "s1", "start_time": 0, "end_time": 5, "duration": 5,
             "narration": "Olá, eu sou o Valdir e hoje vou te ensinar", "broll_override": 1},
            {"scene_id": "s2", "start_time": 5, "end_time": 10, "duration": 5,
             "narration": "o mosquito se reproduz na agua parada"},
            {"scene_id": "s3", "start_time": 10, "end_time": 15, "duration": 5,
             "narration": "obrigado por assistir"},
        ]
        flags = ep.decide_broll(scenes)
        self.assertTrue(flags[0], "override 1 forca b-roll mesmo em apresentacao")

    def test_broll_override_forces_avatar_survives_solo_guard(self) -> None:
        """Override 'so avatar' (-1) nao e revertido pelo guard de avatar-solo."""
        from services import edit_plan as ep
        scenes = [
            {"scene_id": f"s{i}", "start_time": i * 20.0, "end_time": (i + 1) * 20.0,
             "duration": 20.0, "narration": f"conteudo {i}",
             "broll_override": -1 if i in (1, 2, 3) else 0}
            for i in range(5)
        ]
        flags = ep.decide_broll(scenes)
        for i in (1, 2, 3):
            self.assertFalse(flags[i], f"cena {i} travada em avatar permanece avatar")

    def test_broll_override_flows_into_edit_plan(self) -> None:
        from services import edit_plan as ep
        scenes = [
            {"scene_id": f"scene_{i:03d}", "start_time": i * 10.0, "end_time": (i + 1) * 10.0,
             "duration": 10.0, "narration": f"cena {i}", "overlay_text": "",
             "broll_override": -1 if i == 2 else 0}
            for i in range(6)
        ]
        plan = ep.build_edit_plan(
            {"name": "Avatar"}, {"avatar_safe_area": "right"}, scenes, avatar_file="avatar.mp4"
        )
        self.assertFalse(plan["scenes"][2]["broll"], "cena travada em avatar fica sem b-roll no plano")

    def test_caption_falls_back_to_narration_when_overlay_empty(self) -> None:
        from services import edit_plan as ep

        project = {"name": "V"}
        config = {"avatar_safe_area": "right", "resolution": "1280x720"}
        # primeira cena SEMPRE e legendada; sem overlay_text, deriva da narracao
        scenes = [
            {"scene_id": "scene_001", "start_time": 0.0, "end_time": 4.0, "duration": 4.0,
             "overlay_text": "", "narration": "O mosquito da dengue se reproduz na agua parada"},
            {"scene_id": "scene_002", "start_time": 4.0, "end_time": 8.0, "duration": 4.0,
             "overlay_text": "", "narration": "Outra cena qualquer aqui"},
        ]
        plan = ep.build_edit_plan(project, config, scenes)
        cap0 = plan["scenes"][0]["caption"]
        self.assertTrue(cap0)  # nao fica vazia
        self.assertEqual(cap0, cap0.upper())  # estilo lower-third em maiuscula
        self.assertLessEqual(len(cap0.split()), 5)

    def test_edit_plan_broll_rules_with_avatar_base(self) -> None:
        from services import edit_plan as ep

        scenes = [
            {
                "scene_id": f"scene_{i:03d}",
                "start_time": i * 10.0,
                "end_time": (i + 1) * 10.0,
                "duration": 10.0,
                "narration": f"cena {i}",
                "overlay_text": "",
            }
            for i in range(6)
        ]
        plan = ep.build_edit_plan(
            {"name": "Avatar"}, {"avatar_safe_area": "right"}, scenes, avatar_file="avatar.mp4"
        )
        flags = [s["broll"] for s in plan["scenes"]]
        self.assertEqual(plan["avatar"]["mode"], "base")
        # b-roll existe, mas nunca cobre o video inteiro
        self.assertTrue(any(flags))
        self.assertFalse(all(flags))
        # o avatar nunca fica mais de 30s sozinho na tela
        solo = 0.0
        for s in plan["scenes"]:
            if s["broll"]:
                solo = 0.0
            else:
                solo += s["duration"]
                self.assertLessEqual(solo, ep.MAX_AVATAR_SOLO_SECONDS)
        self.assertIn("broll_policy", plan)
        self.assertGreater(plan["broll_policy"]["coverage"], 0)
        self.assertLess(plan["broll_policy"]["coverage"], 1)

    def test_detect_api_keys_from_txt_and_kaggle_json(self) -> None:
        pexels_key = "A1" * 28
        pixabay_key = "12345678-" + "a" * 25
        groq_key = "gsk_" + "x" * 30
        kaggle_token = "f" * 32
        content = "\n".join(
            [
                "# minhas chaves",
                f"Pexels: {pexels_key}",
                f"pixabay = {pixabay_key}",
                groq_key,
                '{"username": "kg-user", "key": "' + kaggle_token + '"}',
            ]
        )
        detected = webapp.detect_api_keys(content)
        self.assertEqual(detected["pexels"], pexels_key)
        self.assertEqual(detected["pixabay"], pixabay_key)
        self.assertEqual(detected["groq"], groq_key)
        self.assertEqual(detected["kaggle_username"], "kg-user")
        self.assertEqual(detected["kaggle_token"], kaggle_token)

    def test_import_keys_route_saves_detected_keys(self) -> None:
        with TestClient(webapp.app) as client:
            uid = db.create_user("import-keys", "password123")
            client.post(
                "/login",
                data={"username": "import-keys", "password": "password123"},
                follow_redirects=False,
            )
            content = "\n".join(
                [
                    "groq: gsk_" + "z" * 30,
                    "pexels: " + "A1" * 28,
                    "coverr: " + "b" * 32,
                    "nvidia: nvapi-" + "N" * 32,
                ]
            )
            resp = client.post(
                "/settings/import-keys",
                files={"keys_file": ("chaves.txt", content.encode("utf-8"), "text/plain")},
            )
            empty = client.post(
                "/settings/import-keys",
                files={"keys_file": ("vazio.txt", b"nada aqui", "text/plain")},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Groq", resp.json()["detail"])
        self.assertIn("Coverr", resp.json()["detail"])
        self.assertIn("NVIDIA", resp.json()["detail"])
        user = db.get_user(uid)
        self.assertTrue(user["groq_key"].startswith("gsk_"))
        self.assertTrue(user["pexels_key"].startswith("A1"))
        self.assertEqual(user["coverr_key"], "b" * 32)
        self.assertTrue(user["nvidia_key"].startswith("nvapi-"))
        self.assertEqual(empty.status_code, 400)

    def test_edit_plan_route_and_review_panel(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id, _scene = self._project_with_scene("plan-route")
            client.post(
                "/login",
                data={"username": "plan-route", "password": "password123"},
                follow_redirects=False,
            )
            missing = client.get(f"/projects/{project_id}/edit-plan")
            plan_path = webapp.project_work_dir(project_id) / webapp.EDIT_PLAN_FILENAME
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "editorial": "llm",
                        "avatar": {"src": "avatar.mp4", "mode": "base"},
                        "broll_policy": {"coverage": 0.6, "max_avatar_solo_seconds": 30, "broll_scenes": 1, "total_scenes": 1},
                        "caption_policy": {"selected": 0},
                        "scenes": [
                            {"scene_id": "scene_001", "start": 0.0, "duration": 4.0,
                             "motion": "slow_push_in", "transition_out": "none", "caption": "", "broll": True}
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            ok = client.get(f"/projects/{project_id}/edit-plan")
            page = client.get(f"/projects/{project_id}")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["version"], 2)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Plano de edição", page.text)
        self.assertIn("determinístico", page.text)
        self.assertIn("plan-chip-broll", page.text)

    def test_package_job_saves_edit_plan_for_review(self) -> None:
        db.init_db()
        uid = db.create_user("plan-save", "password123")
        project_id = db.create_project(uid, "proj plano", "script", {})
        db.replace_scenes(
            project_id,
            [
                {"scene_id": "scene_001", "idx": 1, "start_time": 0, "end_time": 4, "duration": 4, "narration": "a"},
                {"scene_id": "scene_002", "idx": 2, "start_time": 4, "end_time": 8, "duration": 4, "narration": "b"},
                {"scene_id": "scene_003", "idx": 3, "start_time": 8, "end_time": 12, "duration": 4, "narration": "c"},
            ],
        )
        scene = db.list_scenes(project_id)[1]
        db.add_assets(scene["id"], [{"source": "pexels", "download_url": "https://example.com/a.mp4"}])
        asset = db.list_assets(scene["id"])[0]
        db.set_asset_state(asset["id"], "selected")
        job_id = db.create_job(uid, "package", project_id, "pacote")

        def fake_zip(**kwargs):
            zp = kwargs["work_dir"] / "asset_pack_proj.zip"
            zp.parent.mkdir(parents=True, exist_ok=True)
            zp.write_bytes(b"zip")
            return zp

        with patch.object(webapp.packager, "build_zip", side_effect=fake_zip):
            webapp.run_package_job(job_id, project_id, uid)

        plan_path = webapp.project_work_dir(project_id) / webapp.EDIT_PLAN_FILENAME
        self.assertTrue(plan_path.exists())
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(plan["version"], 2)
        self.assertIn("broll", plan["scenes"][0])

    def test_package_job_marks_and_carries_required_avatar(self) -> None:
        db.init_db()
        uid = db.create_user("avatar-contract", "password123")
        project_id = db.create_project(uid, "proj avatar", "script", {"video_style": "avatar_broll"})
        db.replace_scenes(
            project_id,
            [
                {"scene_id": "scene_001", "idx": 1, "start_time": 0, "end_time": 4, "duration": 4, "narration": "a"},
                {"scene_id": "scene_002", "idx": 2, "start_time": 4, "end_time": 8, "duration": 4, "narration": "mosquito na agua"},
                {"scene_id": "scene_003", "idx": 3, "start_time": 8, "end_time": 12, "duration": 4, "narration": "c"},
            ],
        )
        avatar_dir = webapp.WORK_DIR / f"project_{project_id}" / "inputs"
        avatar_dir.mkdir(parents=True, exist_ok=True)
        (avatar_dir / "avatar.mp4").write_bytes(b"fake-avatar")
        scene = db.list_scenes(project_id)[1]
        db.add_assets(scene["id"], [{"source": "pexels", "download_url": "https://example.com/a.mp4"}])
        asset = db.list_assets(scene["id"])[0]
        db.set_asset_state(asset["id"], "selected")
        job_id = db.create_job(uid, "package", project_id, "pacote")

        def fake_zip(**kwargs):
            plan = kwargs["edit_plan"]
            self.assertTrue(plan["avatar_required"])
            self.assertEqual(plan["avatar"]["src"], "avatar.mp4")
            self.assertTrue(any(p.name == "avatar.mp4" for p in kwargs["extra_files"]))
            zp = kwargs["work_dir"] / "asset_pack_proj.zip"
            zp.parent.mkdir(parents=True, exist_ok=True)
            zp.write_bytes(b"zip")
            return zp

        with patch.object(webapp.packager, "build_zip", side_effect=fake_zip):
            webapp.run_package_job(job_id, project_id, uid)

        self.assertEqual(db.get_job(job_id, uid)["status"], "complete")

    def test_montador_keeps_full_audio_and_static_images(self) -> None:
        import inspect

        source = inspect.getsource(montador)
        # imagens nao tem mais zoom proprio (o motion vem do HyperFrames)
        self.assertNotIn("zoompan", source)
        # audio mais longo que o video estende o ultimo frame em vez de cortar
        self.assertIn("tpad=stop_mode=clone", source)
        self.assertNotIn('"-shortest"', source)

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

    def _project_with_scene(self, username: str) -> tuple[int, int, dict]:
        user_id = db.create_user(username, "password123")
        project_id = db.create_project(user_id, f"{username} project", "script", {})
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
        return user_id, project_id, db.list_scenes(project_id)[0]

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

    def test_security_headers_are_set(self) -> None:
        with TestClient(webapp.app) as client:
            resp = client.get("/login")

        self.assertEqual(resp.headers["x-content-type-options"], "nosniff")
        self.assertEqual(resp.headers["referrer-policy"], "same-origin")
        self.assertEqual(resp.headers["x-frame-options"], "DENY")
        self.assertIn("camera=()", resp.headers["permissions-policy"])

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

    def test_search_scene_keeps_searching_when_first_pool_is_off_context(self) -> None:
        scene = {
            "scene_id": "scene_001",
            "narration": "mosquito da dengue chegando na agua parada",
            "visual_goal": "mosquito larvae in stagnant water",
            "keywords": ["mosquito flying", "mosquito larvae water"],
            "must_show": ["mosquito", "water"],
            "must_not_show": ["dog", "child"],
        }

        def pexels(kw, *_args, **_kwargs):
            if kw == "mosquito flying":
                return [
                    {
                        "source": "pexels",
                        "download_url": f"https://pexels.example/bad-{i}.mp4",
                        "keyword": kw,
                        "provider_payload": {"tags": "dog snow child"},
                    }
                    for i in range(8)
                ]
            return [
                {
                    "source": "pexels",
                    "download_url": "https://pexels.example/good.mp4",
                    "keyword": kw,
                    "provider_payload": {"tags": "mosquito larvae water"},
                }
            ]

        with patch.object(asset_search, "search_pexels_videos", pexels), \
             patch.object(asset_search, "search_pixabay_videos", lambda *_a, **_k: []):
            results = asset_search.search_scene(
                ["mosquito flying", "mosquito larvae water"],
                "pk",
                "bk",
                max_w=1920,
                per_keyword=6,
                media="video",
                scene=scene,
            )

        self.assertEqual(results[0]["download_url"], "https://pexels.example/good.mp4")
        self.assertNotIn("https://pexels.example/bad-0.mp4", {r["download_url"] for r in results})

    def test_search_scene_hides_strict_scene_pool_when_no_asset_matches_subject(self) -> None:
        scene = {
            "scene_id": "scene_001",
            "narration": "mosquito femea indo para o balde botar ovo",
            "visual_goal": "mosquito near stagnant water",
            "keywords": ["mosquito stagnant water"],
            "must_show": ["mosquito", "water"],
        }
        bad = [
            {
                "source": "pixabay",
                "download_url": f"https://pixabay.example/water-{i}.mp4",
                "keyword": "mosquito stagnant water",
                "provider_payload": {"tags": "ocean waves water"},
            }
            for i in range(8)
        ]

        with patch.object(asset_search, "search_pexels_videos", lambda *_a, **_k: []), \
             patch.object(asset_search, "search_pixabay_videos", lambda *_a, **_k: bad), \
             patch.object(asset_search, "search_wikimedia_images", lambda *_a, **_k: []), \
             patch.object(asset_search, "search_openverse_images", lambda *_a, **_k: []):
            results = asset_search.search_scene(
                ["mosquito stagnant water"],
                "pk",
                "bk",
                max_w=1920,
                per_keyword=6,
                media="video",
                extra_image_banks=True,
                scene=scene,
            )

        self.assertEqual(results, [])

    def test_extra_banks_fire_only_when_pool_is_thin(self) -> None:
        def fake(source, kw, n):
            return [
                {"source": source, "download_url": f"https://{source}.example/{kw}/{i}", "keyword": kw}
                for i in range(n)
            ]
        wiki = [{"source": "wikimedia", "download_url": "https://commons/w1.jpg", "keyword": "kw1"}]

        # pool MAINSTREAM farto (>= max(4, per_keyword)) -> bancos extras NAO disparam
        with patch.object(asset_search, "search_pexels_videos", lambda kw, *a, **k: fake("pexels", kw, 6)), \
             patch.object(asset_search, "search_pixabay_videos", lambda *a, **k: []), \
             patch.object(asset_search, "search_wikimedia_images", lambda *a, **k: wiki), \
             patch.object(asset_search, "search_openverse_images", lambda *a, **k: []):
            results = asset_search.search_scene(
                ["kw1"], "pk", "bk", max_w=1920, per_keyword=6, media="video", extra_image_banks=True
            )
        self.assertNotIn("wikimedia", {r["source"] for r in results})

        # pool MAINSTREAM fraco -> bancos extras entram como fallback
        with patch.object(asset_search, "search_pexels_videos", lambda kw, *a, **k: fake("pexels", kw, 1)), \
             patch.object(asset_search, "search_pixabay_videos", lambda *a, **k: []), \
             patch.object(asset_search, "search_wikimedia_images", lambda *a, **k: wiki), \
             patch.object(asset_search, "search_openverse_images", lambda *a, **k: []):
            results = asset_search.search_scene(
                ["kw1"], "pk", "bk", max_w=1920, per_keyword=6, media="video", extra_image_banks=True
            )
        self.assertIn("wikimedia", {r["source"] for r in results})

    def test_package_route_fails_job_on_unexpected_error(self) -> None:
        original_build = webapp.packager.build_zip
        try:
            with TestClient(webapp.app) as client:
                uid = db.create_user("pkg-err", "password123")
                project_id = db.create_project(uid, "pkg", "script", {})
                db.replace_scenes(
                    project_id,
                    [
                        {"scene_id": "scene_001", "idx": 1, "start_time": 0, "end_time": 4, "duration": 4},
                        {"scene_id": "scene_002", "idx": 2, "start_time": 4, "end_time": 8, "duration": 4},
                        {"scene_id": "scene_003", "idx": 3, "start_time": 8, "end_time": 12, "duration": 4},
                    ],
                )
                scene = db.list_scenes(project_id)[1]
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

        self.assertEqual(resp.status_code, 303)
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
        self.assertIn("master pronto", resp.text)
        self.assertIn("com narração", resp.text)

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

    def test_secret_fields_are_encrypted_and_not_rendered_in_settings(self) -> None:
        with TestClient(webapp.app) as client:
            uid = db.create_user("secret-user", "password123")
            db.update_api_keys(uid, "pexels-secret-123", "pixabay-secret-456", "groq-secret-789")
            db.update_kaggle_keys(uid, "kg-user", "kaggle-token-abc")
            client.post(
                "/login",
                data={"username": "secret-user", "password": "password123"},
                follow_redirects=False,
            )
            page = client.get("/settings")

        user = db.get_user(uid)
        self.assertEqual(user["pexels_key"], "pexels-secret-123")
        self.assertEqual(user["kaggle_token"], "kaggle-token-abc")
        conn = db._connect()
        try:
            row = conn.execute("SELECT pexels_key, kaggle_token FROM users WHERE id = ?", (uid,)).fetchone()
        finally:
            conn.close()
        self.assertTrue(row["pexels_key"].startswith(db.SECRET_PREFIX))
        self.assertTrue(row["kaggle_token"].startswith(db.SECRET_PREFIX))
        self.assertNotIn("pexels-secret-123", page.text)
        self.assertNotIn("kaggle-token-abc", page.text)

    def test_settings_blank_secret_preserves_existing_value_and_clear_removes_it(self) -> None:
        with TestClient(webapp.app) as client:
            uid = db.create_user("preserve", "password123")
            db.update_api_keys(uid, "pexels-old", "", "")
            client.post(
                "/login",
                data={"username": "preserve", "password": "password123"},
                follow_redirects=False,
            )
            keep = client.post("/settings", data={"groq_model": ""}, follow_redirects=False)
            self.assertEqual(keep.status_code, 303)
            self.assertEqual(db.get_user(uid)["pexels_key"], "pexels-old")
            clear = client.post(
                "/settings",
                data={"groq_model": "", "clear_pexels": "1"},
                follow_redirects=False,
            )
            self.assertEqual(clear.status_code, 303)
        self.assertEqual(db.get_user(uid)["pexels_key"], "")

    def test_asset_change_invalidates_package_and_removes_stale_outputs(self) -> None:
        with TestClient(webapp.app) as client:
            uid, project_id, scene = self._project_with_scene("dirty")
            project_work = webapp.project_work_dir(project_id)
            out_dir = project_work / "kaggle_output"
            out_dir.mkdir(parents=True)
            stale_zip = project_work / "asset_pack_dirty.zip"
            stale_zip.write_bytes(b"zip")
            (out_dir / "final_master.mp4").write_bytes(b"old")
            db.update_kaggle_job(project_id, "dataset", "kernel", "complete")
            db.set_project_status(project_id, "packaged")
            db.add_assets(scene["id"], [{"source": "pexels", "download_url": "https://example.com/a.mp4"}])
            asset = db.list_assets(scene["id"])[0]
            client.post(
                "/login",
                data={"username": "dirty", "password": "password123"},
                follow_redirects=False,
            )
            resp = client.post(f"/assets/{asset['id']}/state", data={"state": "selected"})

        self.assertEqual(resp.status_code, 200)
        project = db.get_project(project_id, uid)
        self.assertEqual(project["status"], "needs_package")
        self.assertEqual(project["kaggle_kernel_slug"], "")
        self.assertFalse(stale_zip.exists())
        self.assertFalse(out_dir.exists())

    def test_finished_review_advances_to_package_step(self) -> None:
        with TestClient(webapp.app) as client:
            uid, project_id, scene = self._project_with_scene("review-next")
            db.add_assets(scene["id"], [{"source": "pexels", "download_url": "https://example.com/a.mp4"}])
            asset = db.list_assets(scene["id"])[0]
            db.set_asset_state(asset["id"], "accepted")
            client.post(
                "/login",
                data={"username": "review-next", "password": "password123"},
                follow_redirects=False,
            )

            resp = client.post(f"/projects/{project_id}/finish-review", follow_redirects=False)
            review = client.get(f"/projects/{project_id}/review")
            repeat = client.post(f"/projects/{project_id}/finish-review", follow_redirects=False)

        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], f"/projects/{project_id}")
        self.assertEqual(db.get_project(project_id, uid)["status"], "reviewed")
        self.assertTrue(webapp.curation_report_path(project_id).exists())
        self.assertIn(f'action="/projects/{project_id}/package"', review.text)
        self.assertIn("Gerar pacote", review.text)
        self.assertNotIn(f'action="/projects/{project_id}/finish-review"', review.text)
        self.assertEqual(repeat.status_code, 303)
        self.assertEqual(repeat.headers["location"], f"/projects/{project_id}")

    def test_finish_review_does_not_require_avatar_only_assets(self) -> None:
        with TestClient(webapp.app) as client:
            uid = db.create_user("review-avatar-only", "password123")
            project_id = db.create_project(uid, "review avatar-only project", "script", {})
            db.replace_scenes(
                project_id,
                [
                    {
                        "scene_id": "scene_001",
                        "idx": 1,
                        "zone": "ABERTURA",
                        "start_time": 0,
                        "end_time": 4,
                        "duration": 4,
                        "narration": "Ola, eu sou o Valdir e hoje vou te mostrar",
                    },
                    {
                        "scene_id": "scene_002",
                        "idx": 2,
                        "zone": "CONTEUDO",
                        "start_time": 4,
                        "end_time": 10,
                        "duration": 6,
                        "narration": "O mosquito da dengue se reproduz na agua parada do quintal",
                    },
                    {
                        "scene_id": "scene_003",
                        "idx": 3,
                        "zone": "ENCERRAMENTO",
                        "start_time": 10,
                        "end_time": 14,
                        "duration": 4,
                        "narration": "Obrigado por assistir, ate a proxima",
                    },
                ],
            )
            scenes = db.list_scenes(project_id)
            db.add_assets(scenes[1]["id"], [{"source": "pexels", "download_url": "https://example.com/a.mp4"}])
            asset = db.list_assets(scenes[1]["id"])[0]
            db.set_asset_state(asset["id"], "accepted")
            client.post(
                "/login",
                data={"username": "review-avatar-only", "password": "password123"},
                follow_redirects=False,
            )

            review_page = client.get(f"/projects/{project_id}/review")
            project_page = client.get(f"/projects/{project_id}")
            resp = client.post(f"/projects/{project_id}/finish-review", follow_redirects=False)

        self.assertEqual(review_page.status_code, 200)
        self.assertIn("avatar sem b-roll", review_page.text)
        self.assertIn("Concluir revis", review_page.text)
        self.assertNotIn("Concluir revisão (1/3)", review_page.text)
        self.assertEqual(project_page.status_code, 200)
        self.assertIn("Revisão (1/1)", project_page.text)
        self.assertIn("Pacote (1/1)", project_page.text)
        self.assertIsNone(re.search(r'id="btn-package"[^>]*disabled', project_page.text, re.S))
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(db.get_project(project_id, uid)["status"], "reviewed")

    def test_reviewed_asset_change_reopens_review_and_invalidates_report(self) -> None:
        with TestClient(webapp.app) as client:
            uid, project_id, scene = self._project_with_scene("review-dirty")
            db.add_assets(scene["id"], [{"source": "pexels", "download_url": "https://example.com/a.mp4"}])
            asset = db.list_assets(scene["id"])[0]
            db.set_asset_state(asset["id"], "accepted")
            report_path = webapp.curation_report_path(project_id)
            report_path.parent.mkdir(parents=True)
            report_path.write_text("stale report", encoding="utf-8")
            db.set_project_status(project_id, "reviewed")
            client.post(
                "/login",
                data={"username": "review-dirty", "password": "password123"},
                follow_redirects=False,
            )

            page = client.get(f"/projects/{project_id}/review")
            resp = client.post(
                f"/assets/{asset['id']}/state",
                data={
                    "state": "rejected",
                    "reject_reason": "fora de contexto",
                    "redirect": f"/projects/{project_id}/review",
                },
                follow_redirects=False,
            )
            reopened = client.get(f"/projects/{project_id}/review")

        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers.get("cache-control"), "no-store")
        self.assertIn("curation-report", page.text)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], f"/projects/{project_id}/review")
        self.assertEqual(db.get_project(project_id, uid)["status"], "reviewing")
        self.assertEqual(db.get_asset(asset["id"])["rejection_reason"], "fora de contexto")
        self.assertFalse(report_path.exists())
        self.assertNotIn("curation-report", reopened.text)

    def test_asset_change_is_rejected_while_project_job_is_busy(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id, scene = self._project_with_scene("busy-change")
            db.add_assets(scene["id"], [{"source": "pexels", "download_url": "https://example.com/a.mp4"}])
            asset = db.list_assets(scene["id"])[0]
            db.set_project_status(project_id, "packaging")
            client.post(
                "/login",
                data={"username": "busy-change", "password": "password123"},
                follow_redirects=False,
            )
            resp = client.post(f"/assets/{asset['id']}/state", data={"state": "selected"})

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(db.list_assets(scene["id"])[0]["state"], "pending")

    def test_delete_project_removes_workspace_files(self) -> None:
        with TestClient(webapp.app) as client:
            uid = db.create_user("delete-files", "password123")
            project_id = db.create_project(uid, "proj", "script", {})
            project_work = webapp.project_work_dir(project_id)
            project_work.mkdir(parents=True)
            (project_work / "asset_pack.zip").write_bytes(b"zip")
            client.post(
                "/login",
                data={"username": "delete-files", "password": "password123"},
                follow_redirects=False,
            )
            resp = client.post(f"/projects/{project_id}/delete", follow_redirects=False)

        self.assertEqual(resp.status_code, 303)
        self.assertFalse(project_work.exists())

    def test_search_job_finishes_with_empty_scenes_when_no_assets_are_found(self) -> None:
        db.init_db()
        uid = db.create_user("no-assets", "password123")
        project_id = db.create_project(uid, "proj", "script", {})
        # 3 cenas: a do meio leva b-roll (1a/ultima sao avatar e nao sao buscadas)
        db.replace_scenes(
            project_id,
            [
                {"scene_id": "scene_001", "idx": 1, "start_time": 0, "end_time": 4, "duration": 4, "narration": "um"},
                {"scene_id": "scene_002", "idx": 2, "start_time": 4, "end_time": 8, "duration": 4,
                 "keywords": ["x"], "narration": "o mosquito na agua parada do quintal"},
                {"scene_id": "scene_003", "idx": 3, "start_time": 8, "end_time": 12, "duration": 4, "narration": "tres"},
            ],
        )
        job_id = db.create_job(uid, "search_assets", project_id, "buscando")
        with patch.object(webapp.asset_search, "search_scene", return_value=[]):
            webapp.run_search_job(job_id, project_id, uid, "pexels", "")

        self.assertEqual(db.get_project(project_id, uid)["status"], "searched")
        job = db.get_job(job_id, uid)
        self.assertEqual(job["status"], "complete")
        self.assertEqual(job["message"], "Busca concluida sem assets confiaveis")
        self.assertIn("scene_002", job["result"]["empty_scenes"])

    def test_search_job_keeps_assets_pending_until_explicit_auto_select(self) -> None:
        db.init_db()
        uid = db.create_user("manual-search", "password123")
        project_id = db.create_project(uid, "proj", "script", {})
        # cena do meio leva b-roll; e a unica buscada (1a/ultima sao avatar)
        db.replace_scenes(
            project_id,
            [
                {"scene_id": "scene_001", "idx": 1, "start_time": 0, "end_time": 4, "duration": 4, "narration": "um"},
                {"scene_id": "scene_002", "idx": 2, "start_time": 4, "end_time": 8, "duration": 4,
                 "keywords": ["x"], "narration": "o mosquito na agua parada do quintal"},
                {"scene_id": "scene_003", "idx": 3, "start_time": 8, "end_time": 12, "duration": 4, "narration": "tres"},
            ],
        )
        job_id = db.create_job(uid, "search_assets", project_id, "buscando")
        fake_asset = {
            "source": "pexels",
            "source_id": "asset-1",
            "asset_type": "video",
            "preview_url": "",
            "download_url": "https://example.com/a.mp4",
            "page_url": "",
            "width": 1920,
            "height": 1080,
            "duration": 10,
            "keyword": "x",
            "author": "",
            "author_url": "",
        }
        with patch.object(webapp.asset_search, "search_scene", return_value=[fake_asset]):
            webapp.run_search_job(job_id, project_id, uid, "pexels", "", "groq")

        self.assertEqual(db.get_project(project_id, uid)["status"], "searched")
        self.assertEqual(db.list_assets_by_state(project_id, ["selected", "accepted"]), [])
        self.assertEqual(len(db.list_assets_by_state(project_id, ["pending"])), 1)
        job = db.get_job(job_id, uid)
        self.assertEqual(job["status"], "complete")
        self.assertEqual(job["result"]["auto_selected"], 0)

    def test_packager_writes_license_manifest_and_kaggle_uses_other_license(self) -> None:
        project = {"name": "Licenca"}
        config = {"avatar_safe_area": "right", "resolution": "1920x1080", "format": "16:9"}
        scenes = [{
            "id": 1, "scene_id": "scene_001", "idx": 1, "zone": "GANCHO",
            "start_time": 0.0, "end_time": 4.0, "duration": 4.0,
            "narration": "teste", "visual_goal": "", "keywords": [],
            "must_show": [], "must_not_show": [], "asset_type": "video",
            "overlay_text": "", "avatar_safe_area": "right",
        }]
        selected = {1: {"source": "pexels", "download_url": "https://example.com/a.mp4", "asset_type": "video"}}

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
            self.assertIn("LICENSES.md", zf.namelist())
            license_text = zf.read("LICENSES.md").decode("utf-8")
        self.assertIn("Nao redistribua", license_text)
        self.assertTrue(any("other" in str(const) for const in kaggle_service.upload_dataset.__code__.co_consts))

    def test_montador_rejects_selected_asset_outside_assets_folder(self) -> None:
        pack_dir = self.root / "pack"
        (pack_dir / "assets").mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, "assets"):
            montador.resolve_selected_asset(pack_dir, "../evil.mp4")
        with self.assertRaisesRegex(RuntimeError, "assets"):
            montador.resolve_selected_asset(pack_dir, "other/file.mp4")

    def test_production_csrf_blocks_missing_token(self) -> None:
        old = webapp.ENFORCE_CSRF
        webapp.ENFORCE_CSRF = True
        try:
            with TestClient(webapp.app) as client:
                db.create_user("csrf", "password123")
                client.post(
                    "/login",
                    data={"username": "csrf", "password": "password123"},
                    follow_redirects=False,
                )
                client.get("/settings")
                resp = client.post("/settings", data={"groq_model": ""}, follow_redirects=False)
        finally:
            webapp.ENFORCE_CSRF = old
        self.assertEqual(resp.status_code, 403)

    def test_registration_can_be_disabled(self) -> None:
        old_allow = webapp.ALLOW_REGISTRATION
        old_first = webapp.ALLOW_FIRST_USER
        old_invite = webapp.INVITE_CODE
        webapp.ALLOW_REGISTRATION = False
        webapp.ALLOW_FIRST_USER = False
        webapp.INVITE_CODE = ""
        try:
            with TestClient(webapp.app) as client:
                resp = client.post(
                    "/register",
                    data={"username": "blocked", "password": "password123"},
                    follow_redirects=False,
                )
        finally:
            webapp.ALLOW_REGISTRATION = old_allow
            webapp.ALLOW_FIRST_USER = old_first
            webapp.INVITE_CODE = old_invite
        self.assertEqual(resp.status_code, 303)
        self.assertIn("Cadastro+desativado", resp.headers["location"])


if __name__ == "__main__":
    unittest.main()
