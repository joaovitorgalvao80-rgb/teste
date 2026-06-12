"""Testes de qualidade de curadoria: relevância, scoring, diversidade e visão.

Complementa test_contracts.py (que cobre contratos/segurança) com cobertura do
que decide QUAL asset vai para cada cena — antes não testado.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ["APP_ENV"] = "dev"

import app as webapp  # noqa: E402
import database as db  # noqa: E402
from services import auto_select, groq_service, scoring, vision  # noqa: E402


def _scene(**over) -> dict:
    base = {
        "id": 1,
        "scene_id": "scene_001",
        "zone": "DESENVOLVIMENTO",
        "narration": "O mosquito da dengue se reproduz na agua parada do quintal",
        "visual_goal": "close up of a mosquito on stagnant water in a backyard",
        "keywords": ["mosquito close up", "stagnant water backyard", "aedes aegypti"],
        "must_show": ["mosquito"],
        "must_not_show": ["cartoon", "watermark"],
        "duration": 5.0,
    }
    base.update(over)
    return base


def _asset(aid: int, keyword: str, **over) -> dict:
    base = {
        "id": aid,
        "asset_type": "video",
        "keyword": keyword,
        "width": 1920,
        "height": 1080,
        "duration": 8.0,
        "source": "pexels",
        "source_id": str(aid),
        "author": f"author{aid}",
    }
    base.update(over)
    return base


CONFIG = {"resolution": "1920x1080", "asset_type_priority": "video"}


class ScoringTest(unittest.TestCase):
    def test_relevant_keyword_scores_higher_than_generic(self) -> None:
        scene = _scene()
        good = _asset(1, "mosquito close up")
        generic = _asset(2, "business background")
        self.assertGreater(
            scoring.keyword_relevance(scene, good),
            scoring.keyword_relevance(scene, generic),
        )
        self.assertEqual(scoring.keyword_relevance(scene, generic), 0.0)

    def test_must_not_show_collision_is_penalized(self) -> None:
        scene = _scene()
        offending = _asset(3, "funny cartoon mosquito")
        # 'cartoon' está em must_not_show -> relevância derrubada
        self.assertLess(scoring.keyword_relevance(scene, offending), 0.5)

    def test_generic_keyword_detection(self) -> None:
        self.assertTrue(scoring.is_generic_keyword("business background"))
        self.assertTrue(scoring.is_generic_keyword("abstract concept"))
        self.assertFalse(scoring.is_generic_keyword("mosquito close up"))
        self.assertTrue(scoring.is_generic_keyword(""))

    def test_relevance_label_bands(self) -> None:
        self.assertEqual(scoring.relevance_label(0.9), "alta")
        self.assertEqual(scoring.relevance_label(0.5), "média")
        self.assertEqual(scoring.relevance_label(0.1), "baixa")


class HeuristicScoreTest(unittest.TestCase):
    def test_relevant_hd_video_beats_irrelevant_lowres(self) -> None:
        scene = _scene()
        good = _asset(1, "mosquito close up", width=1920, duration=8)
        bad = _asset(2, "business background", width=640, height=480, duration=2)
        self.assertGreater(
            auto_select.heuristic_score(scene, good, CONFIG),
            auto_select.heuristic_score(scene, bad, CONFIG),
        )

    def test_auto_select_picks_most_relevant_without_ai(self) -> None:
        scene = _scene()
        candidates = [
            _asset(10, "business background", width=640, height=480, duration=2),
            _asset(11, "mosquito close up", width=1920, duration=8),
            _asset(12, "abstract texture", width=1280, duration=3),
        ]
        choices = auto_select.choose_best_takes(
            [scene], {scene["id"]: candidates}, CONFIG, groq_key=""
        )
        chosen_asset_id, score, reason = choices[scene["id"]]
        self.assertEqual(chosen_asset_id, 11)
        self.assertIn("relevância", reason)

    def test_diversity_penalizes_reused_asset_across_scenes(self) -> None:
        # Duas cenas idênticas; o mesmo asset (source_id) aparece nas duas.
        # A diversidade deve empurrar a 2ª cena para um asset diferente.
        s1 = _scene(id=1, scene_id="scene_001")
        s2 = _scene(id=2, scene_id="scene_002")
        shared = lambda aid: _asset(aid, "mosquito close up", source_id="SHARED", author="same")
        cand1 = [shared(100)]
        cand2 = [
            _asset(200, "mosquito close up", source_id="SHARED", author="same"),
            _asset(201, "stagnant water backyard", source_id="OTHER", author="other"),
        ]
        choices = auto_select.choose_best_takes(
            [s1, s2], {1: cand1, 2: cand2}, CONFIG, groq_key=""
        )
        self.assertEqual(choices[1][0], 100)
        # cena 2 deve evitar repetir o source_id SHARED já usado na cena 1
        self.assertEqual(choices[2][0], 201)


class VisionAdapterTest(unittest.TestCase):
    def test_heuristic_provider_flags_irrelevant_lowres(self) -> None:
        scene = _scene()
        bad = _asset(1, "business background", width=640, height=480, duration=2)
        result = vision.HeuristicVisionProvider().analyze(bad, scene, CONFIG)
        self.assertEqual(result.verdict, "descartar")
        self.assertIn("irrelevante", result.flags)
        self.assertLess(result.score, 30)

    def test_heuristic_provider_approves_relevant_hd(self) -> None:
        scene = _scene()
        good = _asset(1, "mosquito close up", width=1920, duration=8)
        result = vision.HeuristicVisionProvider().analyze(good, scene, CONFIG)
        self.assertIn(result.verdict, {"ótimo", "bom"})
        self.assertGreater(result.score, 60)
        self.assertTrue(result.reasons)

    def test_portrait_orientation_is_flagged(self) -> None:
        scene = _scene()
        vertical = _asset(1, "mosquito close up", width=720, height=1280)
        result = vision.HeuristicVisionProvider().analyze(vertical, scene, CONFIG)
        self.assertIn("retrato", result.flags)

    def test_get_provider_defaults_to_heuristic_without_key(self) -> None:
        self.assertIsInstance(vision.get_provider("llm"), vision.HeuristicVisionProvider)
        self.assertIsInstance(
            vision.get_provider("llm", api_key="x"), vision.LLMVisionProvider
        )

    def test_llm_provider_falls_back_to_heuristic_on_video(self) -> None:
        # vídeo não tem frame estático: cai no heurístico mesmo com chave
        scene = _scene()
        asset = _asset(1, "mosquito close up")
        provider = vision.LLMVisionProvider(api_key="fake-key")
        result = provider.analyze(asset, scene, CONFIG)
        self.assertEqual(result.provider, "heuristic")

    def test_analyze_candidates_returns_one_per_asset(self) -> None:
        scene = _scene()
        assets = [_asset(1, "mosquito close up"), _asset(2, "business background")]
        out = vision.analyze_candidates(scene, assets, CONFIG)
        self.assertEqual(set(out), {1, 2})


class KeywordFallbackTest(unittest.TestCase):
    def test_fallback_strips_portuguese_stopwords(self) -> None:
        scene = {"scene_id": "scene_001", "zone": "GANCHO",
                 "narration": "Voce sabia que quando isso acontece o problema fica pior?"}
        brief = groq_service.fallback_scene_brief(scene, "documentary", "right")
        joined = " ".join(brief["keywords"]).lower()
        # stopwords PT não devem virar termos de busca
        for stop in ("voce", "quando", "isso", "que"):
            self.assertNotIn(f" {stop} ", f" {joined} ")
        self.assertTrue(brief["keywords"])

    def test_fallback_uses_known_anchor_terms(self) -> None:
        scene = {"scene_id": "scene_001", "zone": "DESENVOLVIMENTO",
                 "narration": "O mosquito da dengue ataca no quintal"}
        brief = groq_service.fallback_scene_brief(scene, "documentary", "right")
        joined = " ".join(brief["keywords"]).lower()
        self.assertIn("mosquito", joined)

    def test_prompt_requests_multi_query_strategy(self) -> None:
        prompt = groq_service._build_prompt(
            [{"scene_id": "scene_001", "start_time": 0.0, "end_time": 4.0, "narration": "teste"}],
            "documentary", "right", 0.3,
        )
        self.assertIn("PRIMARY", prompt)
        self.assertIn("SEMANTIC ALTERNATIVE", prompt)
        self.assertIn("FALLBACK", prompt)
        self.assertIn("METAPHORICAL", prompt)


class PersistedVisionScoreTest(unittest.TestCase):
    def test_llm_vision_score_boosts_heuristic_score(self) -> None:
        scene = _scene()
        plain = _asset(1, "mosquito close up")
        praised = dict(plain)
        praised.update({"vision_provider": "llm-vision", "vision_score": 95, "vision_verdict": "ótimo"})
        self.assertGreater(
            auto_select.heuristic_score(scene, praised, CONFIG),
            auto_select.heuristic_score(scene, plain, CONFIG),
        )

    def test_discard_verdict_penalizes_score(self) -> None:
        scene = _scene()
        plain = _asset(1, "mosquito close up")
        discarded = dict(plain)
        discarded.update({"vision_provider": "llm-vision", "vision_score": 5, "vision_verdict": "descartar"})
        self.assertLess(
            auto_select.heuristic_score(scene, discarded, CONFIG),
            auto_select.heuristic_score(scene, plain, CONFIG),
        )

    def test_heuristic_provider_does_not_double_count(self) -> None:
        # provedor heurístico não deve somar de novo (seus sinais já estão no score)
        scene = _scene()
        plain = _asset(1, "mosquito close up")
        with_heur = dict(plain)
        with_heur.update({"vision_provider": "heuristic", "vision_score": 95})
        self.assertEqual(
            auto_select.heuristic_score(scene, plain, CONFIG),
            auto_select.heuristic_score(scene, with_heur, CONFIG),
        )


class VisionJobTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        webapp.DATA_DIR = root / "data"
        webapp.WORK_DIR = webapp.DATA_DIR / "work"
        db.DATA_DIR = webapp.DATA_DIR
        db.DB_PATH = webapp.DATA_DIR / "plataforma.db"
        db.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed(self) -> tuple[int, int]:
        user_id = db.create_user("u", "p")
        project_id = db.create_project(user_id, "proj", "roteiro", {})
        db.replace_scenes(project_id, [{
            "scene_id": "scene_001", "idx": 1, "start_time": 0, "end_time": 5, "duration": 5,
            "narration": "mosquito da dengue na agua parada",
            "visual_goal": "close up of mosquito on stagnant water",
            "keywords": ["mosquito close up", "stagnant water"],
        }])
        scene_db_id = db.list_scenes(project_id)[0]["id"]
        db.add_assets(scene_db_id, [
            {"source": "pexels", "source_id": "1", "asset_type": "video",
             "download_url": "http://x/1.mp4", "keyword": "mosquito close up",
             "width": 1920, "height": 1080, "duration": 8},
            {"source": "pixabay", "source_id": "2", "asset_type": "video",
             "download_url": "http://x/2.mp4", "keyword": "business background",
             "width": 640, "height": 480, "duration": 2},
        ])
        return user_id, project_id

    def test_vision_job_persists_and_is_idempotent(self) -> None:
        user_id, project_id = self._seed()
        job_id = db.create_job(user_id, "vision", project_id, "")
        # sem chave OpenRouter -> provedor heurístico offline
        webapp.run_vision_job(job_id, project_id, user_id, openrouter_key="")
        job = db.get_job(job_id, user_id)
        self.assertEqual(job["status"], "complete")
        self.assertEqual(job["result"]["analyzed"], 2)
        self.assertEqual(job["result"]["provider"], "heuristic")

        scene_db_id = db.list_scenes(project_id)[0]["id"]
        assets = db.list_assets(scene_db_id)
        self.assertTrue(all(a["vision_analyzed"] == 1 for a in assets))
        verdicts = {a["keyword"]: a["vision_verdict"] for a in assets}
        # o asset genérico/low-res deve ser marcado para descarte
        self.assertEqual(verdicts["business background"], "descartar")
        self.assertIn(verdicts["mosquito close up"], {"ótimo", "bom"})

        # segunda execução não reanalisa nada
        job_id2 = db.create_job(user_id, "vision", project_id, "")
        webapp.run_vision_job(job_id2, project_id, user_id, openrouter_key="")
        self.assertEqual(db.get_job(job_id2, user_id)["result"]["analyzed"], 0)

    def test_search_job_auto_analyzes_vision(self) -> None:
        from unittest.mock import patch

        user_id, project_id = self._seed()
        # a busca em si é mockada; queremos provar que a visão roda ao fim dela
        fake_results = [{
            "source": "pexels", "source_id": "9", "asset_type": "video",
            "download_url": "http://x/9.mp4", "keyword": "mosquito close up",
            "width": 1920, "height": 1080, "duration": 8,
        }]
        job_id = db.create_job(user_id, "search_assets", project_id, "")
        with patch.object(webapp.asset_search, "search_scene", return_value=fake_results):
            webapp.run_search_job(job_id, project_id, user_id, "pk", "xk", openrouter_key="")
        job = db.get_job(job_id, user_id)
        self.assertEqual(job["status"], "complete")
        self.assertGreaterEqual(job["result"]["vision_analyzed"], 1)
        scene_db_id = db.list_scenes(project_id)[0]["id"]
        self.assertTrue(all(a["vision_analyzed"] == 1 for a in db.list_assets(scene_db_id)))

    def test_gallery_sorts_chosen_and_best_first(self) -> None:
        user_id, project_id = self._seed()
        # roda a visão para popular scores
        webapp.run_vision_job(db.create_job(user_id, "vision", project_id, ""),
                              project_id, user_id, openrouter_key="")
        scene_db_id = db.list_scenes(project_id)[0]["id"]
        annotated = webapp.annotate_assets_with_vision(
            db.get_scene(scene_db_id),
            db.list_assets(scene_db_id),
            {"resolution": "1920x1080"},
        )
        annotated.sort(key=webapp._take_sort_key, reverse=True)
        # o asset relevante/HD deve vir antes do genérico/low-res
        self.assertEqual(annotated[0]["keyword"], "mosquito close up")


if __name__ == "__main__":
    unittest.main()
