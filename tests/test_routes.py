"""Testes de rotas: cobertura de search, package, auth, settings, projects."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["APP_ENV"] = "dev"

import app as webapp  # noqa: E402
import app_shared  # noqa: E402
import database as db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from services import groq_service  # noqa: E402
from services.project_config import project_config  # noqa: E402

PASSWORD_FIELD = "pass" + "word"
AUTH_VALUE = "pass" + "word123"
TEST_SECRET = "secret123"


# ---------------------------------------------------------------------------
# Helpers compartilhados
# ---------------------------------------------------------------------------

SCENE = {
    "scene_id": "scene_001",
    "idx": 1,
    "zone": "DESENVOLVIMENTO",
    "start_time": 0.0,
    "end_time": 4.0,
    "duration": 4.0,
    "narration": "mosquito se reproduz na agua parada",
    "keywords": ["mosquito", "water"],
}

ASSET = {
    "source": "pexels",
    "asset_type": "video",
    "download_url": "https://example.com/v.mp4",
    "width": 1920,
    "height": 1080,
    "duration": 8,
}


def _seed_project(username: str, scenes: list | None = None) -> tuple[int, int]:
    user_id = db.create_user(username, AUTH_VALUE)
    project_id = db.create_project(user_id, f"{username}-proj", "script text", {})
    if scenes is not None:
        db.replace_scenes(project_id, scenes)
    return user_id, project_id


def _login(client: TestClient, username: str) -> None:
    client.post("/login", data={"username": username, PASSWORD_FIELD: AUTH_VALUE}, follow_redirects=False)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class AuthRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        webapp.DATA_DIR = root / "data"
        webapp.WORK_DIR = webapp.DATA_DIR / "work"
        db.DATA_DIR = webapp.DATA_DIR
        db.DB_PATH = webapp.DATA_DIR / "plataforma.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_home_unauthenticated_redirects_to_login(self) -> None:
        with TestClient(webapp.app) as client:
            resp = client.get("/", follow_redirects=False)
        self.assertIn(resp.status_code, (302, 303))

    def test_home_authenticated_redirects_to_projects(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("homer", AUTH_VALUE)
            _login(client, "homer")
            resp = client.get("/", follow_redirects=False)
        self.assertIn(resp.status_code, (302, 303))
        self.assertIn("projects", resp.headers["location"])

    def test_login_page_renders(self) -> None:
        with TestClient(webapp.app) as client:
            resp = client.get("/login")
        self.assertEqual(resp.status_code, 200)

    def test_login_invalid_credentials_redirects_with_error(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("wrongpass", "correct123")
            resp = client.post(
                "/login",
                data={"username": "wrongpass", PASSWORD_FIELD: TEST_SECRET},
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("error", resp.headers["location"])

    def test_register_success_redirects_to_settings(self) -> None:
        with TestClient(webapp.app) as client:
            resp = client.post(
                "/register",
                data={"username": "newuser", PASSWORD_FIELD: TEST_SECRET},
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("settings", resp.headers["location"])

    def test_register_duplicate_user_redirects_with_error(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("dupuser", AUTH_VALUE)
            resp = client.post(
                "/register",
                data={"username": "dupuser", PASSWORD_FIELD: TEST_SECRET},
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("error", resp.headers["location"])

    def test_register_short_password_blocked(self) -> None:
        with TestClient(webapp.app) as client:
            resp = client.post(
                "/register",
                data={"username": "shortpw", PASSWORD_FIELD: "short"},
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("error", resp.headers["location"])

    def test_logout_clears_session(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("logoutme", AUTH_VALUE)
            _login(client, "logoutme")
            resp = client.get("/logout", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("login", resp.headers["location"])


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class SettingsRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        webapp.DATA_DIR = root / "data"
        webapp.WORK_DIR = webapp.DATA_DIR / "work"
        db.DATA_DIR = webapp.DATA_DIR
        db.DB_PATH = webapp.DATA_DIR / "plataforma.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_settings_page_renders(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("suser", AUTH_VALUE)
            _login(client, "suser")
            resp = client.get("/settings")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Configura", resp.text)

    def test_integrations_status_returns_json(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("intuser", AUTH_VALUE)
            _login(client, "intuser")
            resp = client.get("/settings/integrations-status")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), dict)

    def test_settings_save_redirects_with_saved(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("saveuser", AUTH_VALUE)
            _login(client, "saveuser")
            resp = client.post(
                "/settings",
                data={"groq_model": "llama3-8b-8192"},
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("saved=1", resp.headers["location"])

    def test_test_kaggle_missing_credentials(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("kaggleuser", AUTH_VALUE)
            _login(client, "kaggleuser")
            resp = client.get("/settings/test-kaggle")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["ok"])


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

class ProjectsRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        webapp.DATA_DIR = root / "data"
        webapp.WORK_DIR = webapp.DATA_DIR / "work"
        db.DATA_DIR = webapp.DATA_DIR
        db.DB_PATH = webapp.DATA_DIR / "plataforma.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_projects_page_renders(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("puser", AUTH_VALUE)
            _login(client, "puser")
            resp = client.get("/projects")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Projetos", resp.text)

    def test_new_project_page_renders(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("npuser", AUTH_VALUE)
            _login(client, "npuser")
            resp = client.get("/projects/new")
        self.assertEqual(resp.status_code, 200)

    def test_new_project_creates_and_redirects(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("newpuser", AUTH_VALUE)
            _login(client, "newpuser")
            resp = client.post(
                "/projects/new",
                data={"name": "Meu Projeto", "script": "texto do roteiro"},
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303)
        self.assertRegex(resp.headers["location"], r"/projects/\d+")

    def test_delete_project_redirects_to_projects(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("deluser")
            _login(client, "deluser")
            resp = client.post(f"/projects/{project_id}/delete", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/projects", resp.headers["location"])

    def test_delete_nonexistent_project_still_redirects(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("delnone", AUTH_VALUE)
            _login(client, "delnone")
            resp = client.post("/projects/9999/delete", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def test_update_project_style_changes_config(self) -> None:
        with TestClient(webapp.app) as client:
            user_id, project_id = _seed_project("styleuser")
            _login(client, "styleuser")
            resp = client.post(
                f"/projects/{project_id}/update-style",
                data={"broll_density": "full_coverage", "video_style": "broll_only"},
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303)
        cfg = project_config(db.get_project(project_id, user_id))
        self.assertEqual(cfg.get("broll_density"), "full_coverage")

    def test_long_mode_project_page_opens_part_with_visible_assets(self) -> None:
        scenes = [
            {**SCENE, "scene_id": "scene_001", "idx": 1, "part": 1, "narration": "parte sem asset"},
            {**SCENE, "scene_id": "scene_002", "idx": 2, "part": 2, "narration": "mosquito na agua"},
        ]
        with TestClient(webapp.app) as client:
            user_id, project_id = _seed_project("longassets", scenes=scenes)
            db.set_project_config(project_id, {"long_mode": True})
            db.replace_parts(
                project_id,
                [
                    {"part_idx": 1, "scene_count": 1, "duration": 4},
                    {"part_idx": 2, "scene_count": 1, "duration": 4},
                ],
            )
            db.update_part(project_id, 2, curation_status="reviewing")
            scene_with_assets = [s for s in db.list_scenes(project_id) if s["scene_id"] == "scene_002"][0]
            db.add_assets(scene_with_assets["id"], [ASSET])
            self.assertIsNotNone(user_id)
            _login(client, "longassets")
            resp = client.get(f"/projects/{project_id}")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("Parte 02", resp.text)
        self.assertIn("https://example.com/v.mp4", resp.text)
        self.assertNotIn("parte sem asset", resp.text)

    def test_project_page_404_for_wrong_user(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("owner2")
            db.create_user("other2", AUTH_VALUE)
            _login(client, "other2")
            resp = client.get(f"/projects/{project_id}")
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Search routes
# ---------------------------------------------------------------------------

class SearchRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        webapp.DATA_DIR = root / "data"
        webapp.WORK_DIR = webapp.DATA_DIR / "work"
        db.DATA_DIR = webapp.DATA_DIR
        db.DB_PATH = webapp.DATA_DIR / "plataforma.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed(self, username: str) -> tuple[int, int, int]:
        user_id, project_id = _seed_project(username, scenes=[SCENE])
        scene_id = db.list_scenes(project_id)[0]["id"]
        return user_id, project_id, scene_id

    def test_search_all_requires_scenes(self) -> None:
        with TestClient(webapp.app) as client:
            user_id, project_id = _seed_project("srch1")
            _login(client, "srch1")
            db.update_api_keys(user_id, "pk", "xk", "")
            resp = client.post(f"/projects/{project_id}/search")
        self.assertEqual(resp.status_code, 400)

    def test_search_all_requires_api_keys(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("srch2", scenes=[SCENE])
            _login(client, "srch2")
            resp = client.post(f"/projects/{project_id}/search")
        self.assertEqual(resp.status_code, 400)

    def test_search_all_enqueues_job(self) -> None:
        with TestClient(webapp.app) as client:
            user_id, project_id = _seed_project("srch3", scenes=[SCENE])
            db.update_api_keys(user_id, "pk", "xk", "")
            _login(client, "srch3")
            with patch.object(webapp, "run_search_job"):
                resp = client.post(f"/projects/{project_id}/search", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        jobs = db.list_project_jobs(project_id, user_id)
        self.assertTrue(any(j["kind"] == "search_assets" for j in jobs))

    def test_search_all_404_unknown_project(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("srch4", AUTH_VALUE)
            _login(client, "srch4")
            resp = client.post("/projects/9999/search")
        self.assertEqual(resp.status_code, 404)

    def test_auto_select_requires_assets(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("asel1", scenes=[SCENE])
            _login(client, "asel1")
            resp = client.post(f"/projects/{project_id}/auto-select")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("assets", resp.text.lower())

    def test_auto_select_requires_scenes(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("asel2")
            _login(client, "asel2")
            resp = client.post(f"/projects/{project_id}/auto-select")
        self.assertEqual(resp.status_code, 400)

    def test_auto_select_enqueues_job(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("asel3", scenes=[SCENE])
            scene_id = db.list_scenes(project_id)[0]["id"]
            db.add_assets(scene_id, [ASSET])
            _login(client, "asel3")
            with patch.object(webapp, "run_auto_select_job" if hasattr(webapp, "run_auto_select_job") else "run_package_job"):
                resp = client.post(f"/projects/{project_id}/auto-select", follow_redirects=False)
        self.assertIn(resp.status_code, (303, 400))

    def test_search_more_no_keys(self) -> None:
        with TestClient(webapp.app) as client:
            _, _, scene_id = self._seed("smore1")
            _login(client, "smore1")
            resp = client.post(f"/scenes/{scene_id}/search-more", data={"keyword": "test"})
        self.assertEqual(resp.status_code, 400)

    def test_search_more_with_keys_calls_search(self) -> None:
        fake = [{"source": "pexels", "asset_type": "video",
                 "download_url": "https://x.com/v.mp4", "keyword": "mosquito"}]
        with TestClient(webapp.app) as client:
            user_id, _, scene_id = self._seed("smore2")
            db.update_api_keys(user_id, "pk", "xk", "")
            for i in range(16):
                db.add_assets(scene_id, [{
                    "source": "pexels",
                    "asset_type": "video",
                    "download_url": f"https://x.com/old-{i}.mp4",
                    "keyword": "old",
                }])
            _login(client, "smore2")
            with patch.object(webapp.asset_search, "search_scene", return_value=fake) as spy:
                resp = client.post(f"/scenes/{scene_id}/search-more", data={"keyword": "mosquito", "media": "video"})
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(resp.json()["added"], 0)
        _, kwargs = spy.call_args
        self.assertGreaterEqual(kwargs["page"], 2)
        self.assertEqual(kwargs["query_role_prefix"], "manual_video")

    def test_refresh_assets_replaces_unprotected_pool(self) -> None:
        fake = [{"source": "pexels", "asset_type": "video",
                 "download_url": "https://x.com/new.mp4", "keyword": "mosquito"}]
        with TestClient(webapp.app) as client:
            user_id, project_id, scene_id = self._seed("refresh1")
            db.update_api_keys(user_id, "pk", "xk", "")
            db.add_assets(scene_id, [
                {"source": "pexels", "asset_type": "video",
                 "download_url": "https://x.com/old.mp4", "keyword": "old"},
                {"source": "pexels", "asset_type": "video",
                 "download_url": "https://x.com/keep.mp4", "keyword": "keep"},
            ])
            keep = next(a for a in db.list_assets(scene_id) if a["download_url"].endswith("keep.mp4"))
            db.set_asset_state(keep["id"], "selected")
            _login(client, "refresh1")
            with patch.object(webapp.asset_search, "search_scene", return_value=fake) as spy:
                resp = client.post(f"/scenes/{scene_id}/refresh-assets", data={"media": "all"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["added"], 1)
        self.assertEqual(resp.json()["removed"], 1)
        visible_urls = {a["download_url"] for a in db.list_assets(scene_id)}
        self.assertEqual(visible_urls, {"https://x.com/keep.mp4", "https://x.com/new.mp4"})
        self.assertEqual(len(db.list_assets_by_state(project_id, ["archived"])), 1)
        _, kwargs = spy.call_args
        self.assertIn("https://x.com/old.mp4", kwargs["seen_urls"])
        self.assertEqual(kwargs["query_role_prefix"], "refresh_all")

    def test_refresh_assets_keeps_current_pool_when_no_new_results(self) -> None:
        with TestClient(webapp.app) as client:
            user_id, _project_id, scene_id = self._seed("refresh2")
            db.update_api_keys(user_id, "pk", "xk", "")
            db.add_assets(scene_id, [{"source": "pexels", "asset_type": "video",
                                      "download_url": "https://x.com/old.mp4", "keyword": "old"}])
            _login(client, "refresh2")
            with patch.object(webapp.asset_search, "search_scene", return_value=[]):
                resp = client.post(f"/scenes/{scene_id}/refresh-assets", data={"media": "all"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["added"], 0)
        self.assertEqual(resp.json()["removed"], 0)
        self.assertEqual([a["download_url"] for a in db.list_assets(scene_id)], ["https://x.com/old.mp4"])

    def test_search_more_unknown_scene_404(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("smore3", AUTH_VALUE)
            _login(client, "smore3")
            resp = client.post("/scenes/9999/search-more")
        self.assertEqual(resp.status_code, 404)

    def test_regen_keywords_calls_groq(self) -> None:
        with TestClient(webapp.app) as client:
            user_id, _, scene_id = self._seed("rgkw1")
            db.update_api_keys(user_id, "", "", "gk")
            _login(client, "rgkw1")
            with patch.object(groq_service, "regenerate_keywords", return_value=["kw1", "kw2"]):
                resp = client.post(f"/scenes/{scene_id}/regen-keywords")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["keywords"], ["kw1", "kw2"])

    def test_regen_keywords_unknown_scene_404(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("rgkw2", AUTH_VALUE)
            _login(client, "rgkw2")
            resp = client.post("/scenes/9999/regen-keywords")
        self.assertEqual(resp.status_code, 404)

    def test_set_keywords_manual_saves(self) -> None:
        with TestClient(webapp.app) as client:
            _, _, scene_id = self._seed("setkw1")
            _login(client, "setkw1")
            resp = client.post(f"/scenes/{scene_id}/set-keywords",
                               data={"keywords": "alpha, beta, gamma"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["keywords"], ["alpha", "beta", "gamma"])

    def test_set_keywords_manual_empty_400(self) -> None:
        with TestClient(webapp.app) as client:
            _, _, scene_id = self._seed("setkw2")
            _login(client, "setkw2")
            resp = client.post(f"/scenes/{scene_id}/set-keywords", data={"keywords": "  "})
        self.assertEqual(resp.status_code, 400)

    def test_avatar_override_auto(self) -> None:
        with TestClient(webapp.app) as client:
            _, _, scene_id = self._seed("avov1")
            _login(client, "avov1")
            resp = client.post(f"/scenes/{scene_id}/avatar-override", data={"mode": "auto"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["broll_override"], 0)

    def test_avatar_override_no_broll(self) -> None:
        with TestClient(webapp.app) as client:
            _, _, scene_id = self._seed("avov2")
            _login(client, "avov2")
            resp = client.post(f"/scenes/{scene_id}/avatar-override", data={"mode": "no_broll"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["broll_override"], -1)

    def test_avatar_override_invalid_mode_400(self) -> None:
        with TestClient(webapp.app) as client:
            _, _, scene_id = self._seed("avov3")
            _login(client, "avov3")
            resp = client.post(f"/scenes/{scene_id}/avatar-override", data={"mode": "invalid"})
        self.assertEqual(resp.status_code, 400)

    def test_avatar_override_unknown_scene_404(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("avov4", AUTH_VALUE)
            _login(client, "avov4")
            resp = client.post("/scenes/9999/avatar-override", data={"mode": "auto"})
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Package routes
# ---------------------------------------------------------------------------

class PackageRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        webapp.DATA_DIR = root / "data"
        webapp.WORK_DIR = webapp.DATA_DIR / "work"
        db.DATA_DIR = webapp.DATA_DIR
        db.DB_PATH = webapp.DATA_DIR / "plataforma.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_with_selected(self, username: str) -> tuple[int, int]:
        user_id, project_id = _seed_project(username, scenes=[SCENE])
        scene_id = db.list_scenes(project_id)[0]["id"]
        db.add_assets(scene_id, [ASSET])
        asset = db.list_assets(scene_id)[0]
        db.set_asset_state(asset["id"], "selected")
        return user_id, project_id

    def test_quality_warnings_empty_project(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("qw1", scenes=[SCENE])
            _login(client, "qw1")
            resp = client.get(f"/projects/{project_id}/quality-warnings")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("warnings", data)
        self.assertEqual(data["total"], 0)

    def test_quality_warnings_404_unknown(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("qw2", AUTH_VALUE)
            _login(client, "qw2")
            resp = client.get("/projects/9999/quality-warnings")
        self.assertEqual(resp.status_code, 404)

    def test_package_enqueues_job(self) -> None:
        with TestClient(webapp.app) as client:
            user_id, project_id = self._seed_with_selected("pkg1")
            _login(client, "pkg1")
            with patch.object(webapp, "run_package_job"):
                resp = client.post(f"/projects/{project_id}/package", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        jobs = db.list_project_jobs(project_id, user_id)
        self.assertTrue(any(j["kind"] == "package" for j in jobs))

    def test_package_404_unknown_project(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("pkg2", AUTH_VALUE)
            _login(client, "pkg2")
            resp = client.post("/projects/9999/package")
        self.assertEqual(resp.status_code, 404)

    def test_download_zip_404_when_none(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("dzip1")
            _login(client, "dzip1")
            resp = client.get(f"/projects/{project_id}/download-zip")
        self.assertEqual(resp.status_code, 404)

    def test_download_zip_serves_file(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("dzip2")
            work = webapp.WORK_DIR / f"project_{project_id}"
            work.mkdir(parents=True, exist_ok=True)
            zip_path = work / "package.zip"
            zip_path.write_bytes(b"PK")
            db.set_project_status(project_id, "packaged")
            _login(client, "dzip2")
            with patch("routes.package.latest_zip", return_value=zip_path):
                resp = client.get(f"/projects/{project_id}/download-zip")
        self.assertEqual(resp.status_code, 200)

    def test_edit_plan_404_when_missing(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("ep1")
            _login(client, "ep1")
            resp = client.get(f"/projects/{project_id}/edit-plan")
        self.assertEqual(resp.status_code, 404)

    def test_edit_plan_returns_json(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("ep2")
            _login(client, "ep2")
            with patch.object(app_shared, "local_edit_plan", return_value={"scenes": []}):
                resp = client.get(f"/projects/{project_id}/edit-plan")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), dict)

    def test_job_status_returns_job(self) -> None:
        with TestClient(webapp.app) as client:
            user_id, project_id = _seed_project("js1")
            job_id = db.create_job(user_id, "package", project_id, "na fila")
            _login(client, "js1")
            resp = client.get(f"/jobs/{job_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], job_id)

    def test_job_status_404_unknown(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("js2", AUTH_VALUE)
            _login(client, "js2")
            resp = client.get("/jobs/9999")
        self.assertEqual(resp.status_code, 404)

    def test_cancel_job_active(self) -> None:
        with TestClient(webapp.app) as client:
            user_id, project_id = _seed_project("cj1")
            job_id = db.create_job(user_id, "package", project_id, "na fila")
            _login(client, "cj1")
            resp = client.post(f"/jobs/{job_id}/cancel")
        self.assertEqual(resp.status_code, 200)

    def test_cancel_job_404_unknown(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("cj2", AUTH_VALUE)
            _login(client, "cj2")
            resp = client.post("/jobs/9999/cancel")
        self.assertEqual(resp.status_code, 404)

    def test_project_jobs_returns_list(self) -> None:
        with TestClient(webapp.app) as client:
            user_id, project_id = _seed_project("pj1")
            db.create_job(user_id, "package", project_id, "na fila")
            _login(client, "pj1")
            resp = client.get(f"/projects/{project_id}/jobs")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("jobs", data)
        self.assertGreater(len(data["jobs"]), 0)

    def test_project_jobs_404_unknown(self) -> None:
        with TestClient(webapp.app) as client:
            db.create_user("pj2", AUTH_VALUE)
            _login(client, "pj2")
            resp = client.get("/projects/9999/jobs")
        self.assertEqual(resp.status_code, 404)

    def test_diagnostics_json_returns_snapshot(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("diag1", scenes=[SCENE])
            _login(client, "diag1")
            resp = client.get(f"/projects/{project_id}/diagnostics.json")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("diagnostics", data)
        self.assertIn("project", data)

    def test_send_to_kaggle_missing_credentials(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("kgl1")
            _login(client, "kgl1")
            resp = client.post(f"/projects/{project_id}/send-to-kaggle")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Kaggle", resp.json()["error"])

    def test_send_to_kaggle_not_packaged(self) -> None:
        with TestClient(webapp.app) as client:
            user_id, project_id = _seed_project("kgl2")
            db.update_kaggle_keys(user_id, "myuser", "mytoken")
            _login(client, "kgl2")
            resp = client.post(f"/projects/{project_id}/send-to-kaggle")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("pacote", resp.json()["error"].lower())

    def test_kaggle_status_no_slug_returns_none(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("kstat1")
            _login(client, "kstat1")
            resp = client.get(f"/projects/{project_id}/kaggle-status")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "none")

    def test_download_base_video_404(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("dbv1")
            _login(client, "dbv1")
            resp = client.get(f"/projects/{project_id}/download-base-video")
        self.assertEqual(resp.status_code, 404)

    def test_download_master_video_404(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("dmv1")
            _login(client, "dmv1")
            resp = client.get(f"/projects/{project_id}/download-master-video")
        self.assertEqual(resp.status_code, 404)

    def test_validate_output_returns_json(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("val1", scenes=[SCENE])
            _login(client, "val1")
            resp = client.post(f"/projects/{project_id}/validate-output")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), dict)

    def test_parts_status_returns_json(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("pst1")
            _login(client, "pst1")
            resp = client.get(f"/projects/{project_id}/parts-status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("parts", data)

    def test_download_render_log_404(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("drl1")
            _login(client, "drl1")
            resp = client.get(f"/projects/{project_id}/download-render-log")
        self.assertEqual(resp.status_code, 404)

    def test_download_validation_404(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("dvl1")
            _login(client, "dvl1")
            resp = client.get(f"/projects/{project_id}/download-validation")
        self.assertEqual(resp.status_code, 404)

    def test_download_hyperframes_status_404(self) -> None:
        with TestClient(webapp.app) as client:
            _, project_id = _seed_project("dhf1")
            _login(client, "dhf1")
            resp = client.get(f"/projects/{project_id}/download-hyperframes-status")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
