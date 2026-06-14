"""Operational diagnostics for generated project artifacts."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from . import kaggle_service

VALIDATION_NAME = "output_validation.json"


def validation_path(project_work: Path) -> Path:
    return project_work / "kaggle_output" / VALIDATION_NAME


def read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_validation(project_work: Path) -> Optional[dict]:
    return read_json(validation_path(project_work))


def write_validation(project_work: Path, payload: dict) -> Path:
    path = validation_path(project_work)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _video_probe(path: Path) -> dict:
    if not shutil.which("ffprobe"):
        return {"probe_ok": False, "probe_error": "ffprobe indisponivel"}
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,width,height:format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"probe_ok": False, "probe_error": str(exc)[:300]}
    if result.returncode != 0:
        return {"probe_ok": False, "probe_error": (result.stderr or result.stdout or "").strip()[:300]}
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {"probe_ok": False, "probe_error": "ffprobe retornou JSON invalido"}
    streams = data.get("streams") or []
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "probe_ok": True,
        "duration": round(duration, 3),
        "has_video": bool(video_stream),
        "has_audio": bool(audio_stream),
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
    }


def inspect_video(path: Optional[Path]) -> dict:
    if not path:
        return {"exists": False}
    info = {
        "exists": path.exists(),
        "name": path.name,
        "path": str(path),
        "size_bytes": 0,
        "size_mb": 0.0,
    }
    if not path.exists():
        return info
    size = path.stat().st_size
    info["size_bytes"] = size
    info["size_mb"] = round(size / 1024 / 1024, 3)
    info.update(_video_probe(path))
    return info


def choose_outputs(project_work: Path) -> dict:
    outputs = {"base": None, "master": None}
    out_dir = project_work / "kaggle_output"
    if not out_dir.exists():
        return outputs
    for path in out_dir.rglob("*.mp4"):
        rel = str(path.relative_to(out_dir)).replace("\\", "/")
        if rel.startswith("assets/") or "/assets/" in rel:
            continue
        name = path.name.lower()
        if name == kaggle_service.MASTER_VIDEO_NAME:
            outputs["master"] = path
        elif name in {kaggle_service.BASE_VIDEO_NAME, kaggle_service.BASE_VIDEO_ALIAS}:
            outputs["base"] = path
    return outputs


def _add_duration_issue(issues: list[dict], label: str, info: dict, expected_duration: float) -> None:
    duration = float(info.get("duration") or 0)
    if not duration or not expected_duration:
        return
    tolerance = max(2.0, expected_duration * 0.18)
    if duration < expected_duration - tolerance:
        issues.append(
            {
                "level": "error",
                "message": f"{label} ficou curto demais ({duration:.1f}s vs esperado {expected_duration:.1f}s).",
            }
        )
    elif duration > expected_duration + tolerance:
        issues.append(
            {
                "level": "warn",
                "message": f"{label} passou da duracao esperada ({duration:.1f}s vs {expected_duration:.1f}s).",
            }
        )


def validate_outputs(project_work: Path, expected_duration: float = 0.0) -> dict:
    outputs = choose_outputs(project_work)
    base = inspect_video(outputs["base"])
    master = inspect_video(outputs["master"])
    hyperframes = read_json(project_work / "kaggle_output" / "hyperframes_status.json") or {}
    issues: list[dict] = []

    if not base["exists"] and not master["exists"]:
        issues.append({"level": "pending", "message": "Nenhum video local baixado ainda."})
    for label, info in [("base", base), ("master", master)]:
        if not info["exists"]:
            continue
        if info.get("size_bytes", 0) <= 0:
            issues.append({"level": "error", "message": f"Video {label} esta vazio."})
        if info.get("probe_ok") is False:
            issues.append({"level": "warn", "message": f"Video {label}: {info.get('probe_error', 'ffprobe falhou')}."})
        elif not info.get("has_video"):
            issues.append({"level": "error", "message": f"Video {label} nao possui stream de video."})
        _add_duration_issue(issues, f"Video {label}", info, expected_duration)

    if base["exists"] and not master["exists"]:
        if hyperframes.get("status") == "error":
            issues.append({"level": "warn", "message": "HyperFrames falhou, mas a base foi preservada."})
        else:
            issues.append({"level": "warn", "message": "Base pronta; master final ainda nao esta local."})

    requested_audio = bool(hyperframes.get("requested_audio"))
    requested_avatar = bool(hyperframes.get("requested_avatar"))
    if requested_audio and master["exists"] and master.get("probe_ok") and not master.get("has_audio"):
        issues.append({"level": "error", "message": "Master foi solicitado com narracao, mas saiu sem stream de audio."})
    if requested_audio and hyperframes and not hyperframes.get("audio"):
        issues.append({"level": "error", "message": "Pos-processamento nao confirmou a narracao no master."})
    if requested_avatar and hyperframes and not hyperframes.get("avatar"):
        issues.append({"level": "warn", "message": "Avatar foi solicitado, mas o status nao confirmou overlay no master."})

    levels = {item["level"] for item in issues}
    if "error" in levels:
        status = "error"
    elif "warn" in levels:
        status = "warn"
    elif "pending" in levels:
        status = "pending"
    else:
        status = "ok"

    payload = {
        "status": status,
        "checked_at": time.time(),
        "expected_duration": round(float(expected_duration or 0), 3),
        "outputs": {"base": base, "master": master},
        "hyperframes": hyperframes,
        "issues": issues,
        "files": {
            "validation": str(validation_path(project_work)),
            "render_log": str(project_work / "kaggle_output" / "log_render.txt"),
            "hyperframes_status": str(project_work / "kaggle_output" / "hyperframes_status.json"),
        },
    }
    write_validation(project_work, payload)
    return payload


def build_snapshot(
    project_work: Path,
    zip_path: Optional[Path],
    selected_count: int,
    scene_count: int,
    expected_duration: float,
    total_scene_count: Optional[int] = None,
) -> dict:
    cached_validation = read_validation(project_work)
    out_dir = project_work / "kaggle_output"
    return {
        "zip": {
            "exists": bool(zip_path and zip_path.exists()),
            "name": zip_path.name if zip_path else "",
            "size_mb": round(zip_path.stat().st_size / 1024 / 1024, 3) if zip_path and zip_path.exists() else 0.0,
        },
        "selection": {
            "selected": selected_count,
            "scenes": scene_count,
            "total_scenes": total_scene_count if total_scene_count is not None else scene_count,
            "complete": scene_count > 0 and selected_count == scene_count,
        },
        "outputs": {
            "folder_exists": out_dir.exists(),
            "validation": cached_validation,
        },
        "expected_duration": round(float(expected_duration or 0), 3),
        "logs": {
            "render_log": (out_dir / "log_render.txt").exists(),
            "hyperframes_status": (out_dir / "hyperframes_status.json").exists(),
            "validation": validation_path(project_work).exists(),
        },
    }
