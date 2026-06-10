"""Gera localmente o index.html da composicao HyperFrames para inspecao.

Executa o head do _RUNNER com ffprobe stubado (sem precisar de ffmpeg local)
e imprime a composicao que o Kaggle geraria para um edit plan de exemplo.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import kaggle_service  # noqa: E402
from tools.smoke_hyperframes_kaggle import SPLIT_MARKER  # noqa: E402

head = kaggle_service._RUNNER.split(SPLIT_MARKER, 1)[0]
ns: dict = {}
exec(compile(head, "runner_head.py", "exec"), ns)

ns["ffprobe_duration"] = lambda _p: 12.0  # stub: sem ffprobe local

plan = {
    "version": 1,
    "resolution": "1280x720",
    "caption_position": "left",
    "audio": {"src": "narration.wav", "volume": 1.0},
    "avatar": {"src": "avatar.webm", "position": "right", "scale": 0.3},
    "scenes": [
        {"start": 0, "duration": 4, "motion": "slow_push_in", "transition_out": "fade", "caption": "Cena <um> & teste"},
        {"start": 4, "duration": 4, "motion": "slow_pull_out", "transition_out": "fade", "caption": "Cena dois"},
        {"start": 8, "duration": 4, "motion": "slow_push_in", "transition_out": "none", "caption": ""},
    ],
}

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp) / "base.mp4"
    base.write_bytes(b"fake")
    narration = Path(tmp) / "narration.wav"
    narration.write_bytes(b"fake")
    avatar = Path(tmp) / "avatar.webm"
    avatar.write_bytes(b"fake")
    project_dir = Path(tmp) / "hf"
    duration = ns["write_hyperframes_project"](base, project_dir, plan, narration, avatar)
    print((project_dir / "index.html").read_text(encoding="utf-8"))
    print("--- duration:", duration)
