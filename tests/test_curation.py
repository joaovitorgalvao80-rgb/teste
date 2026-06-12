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

    def test_get_provider_nvidia(self) -> None:
        p = vision.get_provider("nvidia", api_key="nvapi-x")
        self.assertIsInstance(p, vision.NvidiaVisionProvider)
        self.assertEqual(p.name, "nvidia-vision")
        self.assertIn("nvidia", p.url)
        self.assertIn("vision", p.model)

    def test_get_provider_groq_uses_groq_endpoint(self) -> None:
        p = vision.get_provider("groq", api_key="gsk_x")
        self.assertIsInstance(p, vision.LLMVisionProvider)
        self.assertEqual(p.url, vision.GROQ_VISION_URL)
        self.assertEqual(p.name, "groq-vision")
        self.assertIn("llama-4", p.model)

    def test_low_vision_score_or_offtopic_flag_is_discard(self) -> None:
        self.assertEqual(vision._verdict_for(10, []), "descartar")
        self.assertEqual(vision._verdict_for(90, ["fora_do_tema"]), "descartar")
        self.assertEqual(vision._verdict_for(75, []), "ótimo")

    def test_get_provider_defaults_to_heuristic_without_key(self) -> None:
        self.assertIsInstance(vision.get_provider("llm"), vision.HeuristicVisionProvider)
        self.assertIsInstance(
            vision.get_provider("llm", api_key="x"), vision.LLMVisionProvider
        )

    def test_llm_provider_falls_back_when_no_thumbnail(self) -> None:
        # sem thumbnail analisável (preview_url ausente), cai no heurístico
        scene = _scene()
        asset = _asset(1, "mosquito close up")  # sem preview_url
        provider = vision.LLMVisionProvider(api_key="fake-key")
        result = provider.analyze(asset, scene, CONFIG)
        self.assertEqual(result.provider, "heuristic")

    def test_llm_provider_analyzes_video_poster(self) -> None:
        # vídeo COM poster (preview_url) agora é enviado ao modelo de visão
        from unittest.mock import patch
        scene = _scene()
        asset = _asset(1, "mosquito close up", preview_url="http://x/poster.jpg")

        class _R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content":
                    '{"score": 88, "reasons": ["combina"], "flags": []}'}}]}

        provider = vision.LLMVisionProvider(api_key="fake-key")
        with patch.object(vision.requests, "post", return_value=_R()):
            result = provider.analyze(asset, scene, CONFIG)
        self.assertEqual(result.provider, "llm-vision")
        self.assertEqual(result.score, 88.0)

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

    def test_fallback_uses_zone_fillers(self) -> None:
        # sem o dict de dominio removido, o fallback usa fillers por zona +
        # tokens da narracao, sempre devolvendo keywords nao vazias e em ingles.
        scene = {"scene_id": "scene_001", "zone": "CTA",
                 "narration": "Aplique o produto e proteja sua casa hoje"}
        brief = groq_service.fallback_scene_brief(scene, "documentary", "right")
        joined = " ".join(brief["keywords"]).lower()
        self.assertTrue(brief["keywords"])
        # algum filler de CTA deve aparecer
        self.assertTrue("action" in joined or "hands" in joined or "working" in joined)

    def test_prompt_requests_multi_query_strategy(self) -> None:
        prompt = groq_service._build_prompt(
            [{"scene_id": "scene_001", "start_time": 0.0, "end_time": 4.0, "narration": "teste"}],
            "documentary", "right", 0.3,
        )
        self.assertIn("PRIMARY", prompt)
        self.assertIn("SEMANTIC ALTERNATIVE", prompt)
        self.assertIn("FALLBACK", prompt)
        self.assertIn("METAPHORICAL", prompt)


def _load_runner_func(name: str):
    """Extrai e executa uma funcao do _RUNNER (codigo do kernel Kaggle, que vive
    como string e nao e importavel). Permite testar a logica real do render."""
    import re
    from services import kaggle_service
    src = kaggle_service._RUNNER
    ns: dict = {}
    gap = re.search(r"^BROLL_MERGE_MAX_GAP = ([\d.]+)", src, re.M)
    if gap:
        ns["BROLL_MERGE_MAX_GAP"] = float(gap.group(1))
    start = src.index(f"def {name}(")
    end = src.index("\ndef ", start + 1)
    exec(src[start:end], ns)
    return ns[name]


class BrollWindowTest(unittest.TestCase):
    """O avatar nao pode 'piscar' entre b-rolls consecutivos (pausas da narracao)."""

    def setUp(self) -> None:
        self.broll_windows = _load_runner_func("broll_windows")

    def test_consecutive_broll_scenes_merge_across_narration_gap(self) -> None:
        # duas cenas broll com pausa de 0.3s entre elas (fim 7.1 -> inicio 7.4)
        scenes = [
            {"start": 0.0, "duration": 7.1, "broll": True},
            {"start": 7.4, "duration": 6.2, "broll": True},
        ]
        wins = self.broll_windows(scenes, 20.0, 20.0)
        self.assertEqual(len(wins), 1, "cenas broll seguidas devem virar UMA janela continua")
        self.assertAlmostEqual(wins[0]["start"], 0.0, places=2)
        self.assertAlmostEqual(wins[0]["end"], 13.6, places=2)  # cobre a pausa

    def test_avatar_scene_between_brolls_splits_windows(self) -> None:
        scenes = [
            {"start": 0.0, "duration": 4.0, "broll": True},
            {"start": 4.0, "duration": 4.0, "broll": False},  # avatar
            {"start": 8.0, "duration": 4.0, "broll": True},
        ]
        wins = self.broll_windows(scenes, 20.0, 20.0)
        self.assertEqual(len(wins), 2, "cena de avatar no meio deve separar as janelas")


class DiscardExclusionTest(unittest.TestCase):
    def test_auto_select_skips_vision_discarded_when_alternative_exists(self) -> None:
        scene = _scene()
        good = _asset(1, "mosquito close up")
        good["vision_verdict"] = "bom"
        # asset bem pontuado pela heuristica, mas reprovado pela visao
        discarded = _asset(2, "mosquito close up", width=4000)
        discarded["vision_verdict"] = "descartar"
        choices = auto_select.choose_best_takes(
            [scene], {scene["id"]: [discarded, good]}, CONFIG, groq_key=""
        )
        self.assertEqual(choices[scene["id"]][0], 1, "nao deve escolher o take reprovado pela visao")


class PexelsRateLimitTest(unittest.TestCase):
    """A Pexels devolve 401 em rajada; o cliente serializa e tenta de novo."""

    class _Resp:
        def __init__(self, code: int) -> None:
            self.status_code = code
            self.text = ""

        def json(self) -> dict:
            return {"videos": [], "photos": []}

    def test_pexels_get_retries_on_401_then_succeeds(self) -> None:
        from unittest.mock import patch
        from services import asset_search

        seq = [self._Resp(401), self._Resp(200)]
        calls = {"n": 0}

        def fake_get(url, headers=None, params=None, timeout=None):
            r = seq[min(calls["n"], len(seq) - 1)]
            calls["n"] += 1
            return r

        with patch.object(asset_search.requests, "get", side_effect=fake_get), \
             patch.object(asset_search.time, "sleep", lambda *_a, **_k: None):
            resp = asset_search._pexels_get("http://x", headers={}, params={})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calls["n"], 2)  # uma falha + um retry bem-sucedido

    def test_pexels_video_degrades_gracefully_when_always_401(self) -> None:
        from unittest.mock import patch
        from services import asset_search

        def always_401(url, headers=None, params=None, timeout=None):
            return self._Resp(401)

        with patch.object(asset_search.requests, "get", side_effect=always_401), \
             patch.object(asset_search.time, "sleep", lambda *_a, **_k: None):
            out = asset_search.search_pexels_videos("rain", "key", max_w=1280, per_page=3)
        self.assertEqual(out, [])  # cai para [] sem estourar excecao


class KeywordRoleTest(unittest.TestCase):
    def test_assign_roles_by_position(self) -> None:
        self.assertEqual(
            scoring.assign_roles(["a", "b", "c", "d"]),
            ["primary", "alternative", "fallback", "fallback"],
        )
        self.assertEqual(scoring.assign_roles([]), [])

    def test_keyword_role_uses_persisted_roles(self) -> None:
        scene = {"keywords": ["x", "y", "z"],
                 "keyword_roles": ["primary", "alternative", "fallback"]}
        self.assertEqual(scoring.keyword_role(scene, "x"), "primary")
        self.assertEqual(scoring.keyword_role(scene, "z"), "fallback")
        self.assertEqual(scoring.keyword_role(scene, "desconhecida"), "fallback")

    def test_primary_match_scores_above_fallback_match(self) -> None:
        asset = {"keyword": "alpha beta"}
        primary_scene = {"keywords": ["alpha beta"], "keyword_roles": ["primary"]}
        fallback_scene = {"keywords": ["alpha beta"], "keyword_roles": ["fallback"]}
        self.assertGreater(
            scoring.keyword_relevance(primary_scene, asset),
            scoring.keyword_relevance(fallback_scene, asset),
        )


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
        webapp.run_vision_job(job_id, project_id, user_id, groq_key="", openrouter_key="")
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
        webapp.run_vision_job(job_id2, project_id, user_id, groq_key="", openrouter_key="")
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

    def test_keyword_roles_persist_round_trip(self) -> None:
        user_id = db.create_user("ru", "p")
        project_id = db.create_project(user_id, "rp", "x", {})
        db.replace_scenes(project_id, [{
            "scene_id": "scene_001", "idx": 1, "start_time": 0, "end_time": 4, "duration": 4,
            "keywords": ["main shot", "other angle", "broad theme"],
        }])
        scene = db.list_scenes(project_id)[0]
        # roles derivados por posição na ausência de roles explícitos
        self.assertEqual(scene["keyword_roles"], ["primary", "alternative", "fallback"])
        # update_scene_keywords também atualiza os papéis
        db.update_scene_keywords(scene["id"], ["novo principal", "reserva ampla"])
        scene2 = db.get_scene(scene["id"])
        self.assertEqual(scene2["keyword_roles"], ["primary", "alternative"])

    def test_preview_page_shows_chosen_take_and_warnings(self) -> None:
        from unittest.mock import patch
        from fastapi.testclient import TestClient

        user_id, project_id = self._seed()
        scene_db_id = db.list_scenes(project_id)[0]["id"]
        # marca o asset genérico (low-res) como escolhido -> preview deve alertar
        generic = next(a for a in db.list_assets(scene_db_id) if a["keyword"] == "business background")
        db.set_asset_state(generic["id"], "selected")

        client = TestClient(webapp.app)
        with patch.object(webapp, "require_user", return_value={"id": user_id, "username": "u"}):
            r = client.get(f"/projects/{project_id}/preview")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Preview da montagem", r.text)
        self.assertIn("scene_001", r.text)
        # baixa relevância / descarte do take genérico aparece como alerta
        self.assertIn("relevância baixa", r.text)

    def test_gallery_sorts_chosen_and_best_first(self) -> None:
        user_id, project_id = self._seed()
        # roda a visão para popular scores
        webapp.run_vision_job(db.create_job(user_id, "vision", project_id, ""),
                              project_id, user_id, groq_key="", openrouter_key="")
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
