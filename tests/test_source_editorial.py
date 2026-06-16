from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import database as db
from services import editorial_analysis, edit_plan, packager, source_discovery


class SourceEditorialTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        db.DATA_DIR = self.root / "data"
        db.DB_PATH = db.DATA_DIR / "plataforma.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_database_stores_deep_keys_and_source_metadata(self) -> None:
        db.init_db()
        uid = db.create_user("deep-user", "password123")
        db.update_api_keys(
            uid,
            "pexels",
            "pixabay",
            "groq",
            exa="exa-secret-123",
            firecrawl="fc-secret-45678901234567890",
        )
        user = db.get_user(uid)
        self.assertEqual(user["exa_key"], "exa-secret-123")
        self.assertEqual(user["firecrawl_key"], "fc-secret-45678901234567890")

        project_id = db.create_project(uid, "deep", "script", {})
        db.replace_scenes(
            project_id,
            [{"scene_id": "scene_001", "idx": 1, "duration": 5, "narration": "teste"}],
        )
        scene = db.list_scenes(project_id)[0]
        db.add_assets(
            scene["id"],
            [
                {
                    "source": "firecrawl",
                    "asset_type": "image",
                    "download_url": "https://cdn.example/a.jpg",
                    "page_url": "https://example.com/a",
                    "license": "review_required",
                    "discovery_provider": "firecrawl",
                    "provider_payload": {"title": "Example"},
                    "confidence": 0.62,
                }
            ],
        )
        asset = db.list_assets(scene["id"])[0]
        self.assertEqual(asset["license"], "review_required")
        self.assertEqual(asset["discovery_provider"], "firecrawl")
        self.assertAlmostEqual(asset["confidence"], 0.62)
        self.assertEqual(json.loads(asset["provider_payload_json"])["title"], "Example")

    def test_firecrawl_search_normalizes_image_assets(self) -> None:
        def fake_post(url, **_kwargs):
            self.assertIn("firecrawl", url)
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "data": {
                        "images": [
                            {
                                "imageUrl": "https://cdn.example.com/scene.jpg",
                                "url": "https://example.com/page",
                                "title": "Scene image",
                                "width": 1600,
                                "height": 900,
                            }
                        ]
                    }
                },
                text="{}",
            )

        scene = {
            "scene_id": "scene_001",
            "visual_goal": "old city street",
            "keywords": ["old city street"],
        }
        with patch.object(source_discovery.requests, "post", side_effect=fake_post):
            assets = source_discovery.discover_scene_assets(
                scene,
                {"firecrawl": "fc-test", "exa": ""},
                max_w=1920,
                limit=3,
            )
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["source"], "firecrawl")
        self.assertEqual(assets[0]["asset_type"], "image")
        self.assertEqual(assets[0]["license"], "review_required")

    def test_editorial_report_turns_plan_into_assisted_v3(self) -> None:
        project = {"name": "Editorial"}
        config = {"resolution": "1920x1080", "video_style": "avatar_broll", "broll_density": "moderate"}
        scenes = [
            {"id": 1, "scene_id": "scene_001", "idx": 1, "start_time": 0, "end_time": 4, "duration": 4, "narration": "Alerta importante?"},
            {"id": 2, "scene_id": "scene_002", "idx": 2, "start_time": 4, "end_time": 12, "duration": 8, "narration": "Mostre o exemplo"},
            {"id": 3, "scene_id": "scene_003", "idx": 3, "start_time": 12, "end_time": 16, "duration": 4, "narration": "fim"},
        ]
        selected = {
            2: {
                "source": "firecrawl",
                "asset_type": "video",
                "duration": 2,
                "width": 640,
                "height": 360,
                "license": "review_required",
            }
        }
        report = editorial_analysis.build_report(project, config, scenes, selected)
        plan = edit_plan.build_edit_plan(project, config, scenes, editorial_report=report)
        self.assertEqual(plan["editorial_mode"], "assisted_v3")
        self.assertGreater(plan["editorial_assist"]["risk_count"], 0)
        scene_two = next(s for s in plan["scenes"] if s["scene_id"] == "scene_002")
        self.assertIn("short_video_loop_risk", scene_two["asset_risks"])

    def test_packager_writes_source_manifest_and_editorial_report(self) -> None:
        project = {"name": "Manifest"}
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
                "asset_type": "image",
                "overlay_text": "",
                "avatar_safe_area": "right",
            }
        ]
        selected = {
            1: {
                "source": "firecrawl",
                "download_url": "https://cdn.example.com/a.jpg",
                "asset_type": "image",
                "keyword": "test",
                "license": "review_required",
                "discovery_provider": "firecrawl",
            }
        }

        def fake_download(_url, dest, _max_bytes):
            dest.write_bytes(b"fake-image")
            return True

        with patch.object(packager, "_download", side_effect=fake_download):
            zip_path = packager.build_zip(
                project,
                config,
                scenes,
                selected,
                [],
                self.root / "work",
                edit_plan={"version": 2, "scenes": []},
                editorial_report={"version": 1, "summary": {"risk_count": 1}},
            )

        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            manifest = json.loads(zf.read("metadata/source_manifest.json"))
            other = json.loads(zf.read("metadata/other_sources.json"))
        self.assertIn("editorial_report.json", names)
        self.assertEqual(manifest[0]["source"], "firecrawl")
        self.assertEqual(other[0]["discovery_provider"], "firecrawl")


if __name__ == "__main__":
    unittest.main()
