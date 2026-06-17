"""Integração com Kaggle via CLI (kaggle>=1.5).

Usa subprocess + kaggle CLI em vez da API Python para evitar
incompatibilidades entre versões do pacote.

montador.py e embutido no runner via base64 para evitar falhas de
timing: datasets Kaggle sao processados assincronamente e o kernel
pode rodar antes de /kaggle/input/ estar populado.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import csv
import io
from pathlib import Path
from typing import Iterable

from . import api_usage

ROOT = Path(__file__).resolve().parent.parent
MONTADOR_FILENAME = "montador.py"
BASE_VIDEO_NAME = "video_broll_base.mp4"
BASE_VIDEO_ALIAS = "base_broll.mp4"
MASTER_VIDEO_NAME = "final_master.mp4"
KAGGLE_ARG_PATTERN = re.compile(r"^[\w./:@+\\\-=,]+$")


def _slug(text: str, max_len: int = 36) -> str:
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (text[:max_len] or "brolls").strip("-")


def dataset_slug(project_name: str, project_id: int | None = None) -> str:
    # project_id no slug evita que dois projetos com o mesmo nome
    # sobrescrevam o dataset/kernel um do outro no Kaggle.
    prefix = f"brolls-p{project_id}-" if project_id else "brolls-"
    return (prefix + _slug(project_name))[:50]


def kernel_slug(project_name: str, project_id: int | None = None) -> str:
    prefix = f"b-rolls-render-p{project_id}-" if project_id else "b-rolls-render-"
    return (prefix + _slug(project_name))[:50]


def _kernel_ref(username: str, k_slug: str) -> str:
    return f"{username}/{k_slug}"


def _extract_kernel_slug(push_output: str, username: str, fallback_slug: str) -> str:
    patterns = [
        rf"kaggle\.com/code/{re.escape(username)}/([a-z0-9][a-z0-9-]*)",
        rf"kaggle\.com/{re.escape(username)}/([a-z0-9][a-z0-9-]*)",
    ]
    for pattern in patterns:
        match = re.search(pattern, push_output or "", flags=re.IGNORECASE)
        if match:
            return match.group(1).lower()
    return fallback_slug


def _looks_like_missing_kernel_error(error: str) -> bool:
    low = (error or "").lower()
    return "404" in low or "not found" in low or "does not exist" in low


def _looks_like_auth_error(error: str) -> bool:
    low = (error or "").lower()
    return "401" in low or "403" in low or "unauthorized" in low or "forbidden" in low


def _output_basename(filename: str) -> str:
    name = (filename or "").lower().replace("\\", "/").rsplit("/", 1)[-1]
    return name


def _is_nested_asset_path(filename: str) -> bool:
    path = (filename or "").lower().replace("\\", "/")
    return path.startswith("assets/") or "/assets/" in path


def _is_video_output(filename: str) -> bool:
    if _is_nested_asset_path(filename):
        return False
    name = _output_basename(filename)
    return name in {BASE_VIDEO_NAME, BASE_VIDEO_ALIAS, MASTER_VIDEO_NAME} or name.endswith(".mp4")


def _video_output_priority(filename: str) -> int:
    name = _output_basename(filename)
    if name == MASTER_VIDEO_NAME:
        return 0
    if name in {BASE_VIDEO_NAME, BASE_VIDEO_ALIAS}:
        return 1
    if name.endswith(".mp4"):
        return 2
    return 99


def choose_preferred_video_path(paths: Iterable[Path]) -> Path | None:
    videos = [p for p in paths if _is_video_output(str(p))]
    if not videos:
        return None
    return min(videos, key=lambda p: (_video_output_priority(str(p)), -p.stat().st_mtime))


def _parse_kernel_files_csv(output: str) -> list[str]:
    files: list[str] = []
    try:
        rows = csv.DictReader(io.StringIO(output or ""))
        for row in rows:
            filename = row.get("fileName") or row.get("name") or row.get("FileName") or row.get("ref")
            if filename:
                files.append(filename.strip())
    except csv.Error:
        pass
    if files:
        return files
    for line in (output or "").splitlines():
        line = line.strip()
        header = line.split(",", 1)[0].strip().lower()
        if line and header not in {"filename", "name", "ref", "totalbytes"}:
            files.append(line.split(",", 1)[0].strip())
    return [f for f in files if f]


def list_kernel_files(k_slug: str, username: str, token: str) -> tuple[list[str], str]:
    kernel = _kernel_ref(username, k_slug)
    result = _run(["kernels", "files", kernel, "-v", "--page-size", "200"], username, token, timeout=60)
    output = (result.stdout or "") + (result.stderr or "")
    return _parse_kernel_files_csv(output), output


def kernel_exists(k_slug: str, username: str, token: str) -> tuple[bool, str]:
    """Confirma existencia sem chamar kernels status/GetKernelSessionStatus."""
    try:
        list_kernel_files(k_slug, username, token)
        return True, ""
    except RuntimeError as exc:
        err = str(exc)
        if _looks_like_missing_kernel_error(err) or _looks_like_auth_error(err):
            return False, err

    try:
        result = _run(
            ["kernels", "list", "--mine", "--search", k_slug, "--page-size", "20", "-v", "--sort-by", "dateRun"],
            username,
            token,
            timeout=60,
        )
        out = (result.stdout or "") + (result.stderr or "")
        return (k_slug.lower() in out.lower()), out
    except RuntimeError as exc:
        err = str(exc)
        if _looks_like_missing_kernel_error(err) or _looks_like_auth_error(err):
            return False, err
        return True, err


def kernel_status_hint(k_slug: str, username: str, token: str) -> str:
    """Consulta status apenas como hint; o fluxo principal nao depende dele."""
    kernel = _kernel_ref(username, k_slug)
    result = _run(["kernels", "status", kernel], username, token, timeout=60)
    return ((result.stdout or "") + (result.stderr or "")).strip()


def _validated_kaggle_args(args: list[str]) -> list[str]:
    safe_args: list[str] = []
    for arg in args:
        value = str(arg)
        if not value or "\x00" in value or not KAGGLE_ARG_PATTERN.match(value):
            raise RuntimeError("Argumento invalido para Kaggle CLI.")
        safe_args.append(value)
    return safe_args


def _run(args: list[str], username: str, token: str, **kwargs) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["KAGGLE_USERNAME"] = username
    env["KAGGLE_KEY"] = token
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    cmd = [sys.executable, "-m", "kaggle"] + _validated_kaggle_args(args)
    start = time.monotonic()
    operation = " ".join(args[:2]) if args else "cli"
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", **kwargs)
        api_usage.record(
            "kaggle",
            operation,
            status_code=result.returncode,
            ok=result.returncode == 0,
            latency_ms=api_usage.elapsed_ms(start),
        )
    except Exception as exc:
        api_usage.record(
            "kaggle",
            operation,
            ok=False,
            latency_ms=api_usage.elapsed_ms(start),
            detail=type(exc).__name__,
        )
        raise
    if result.returncode != 0:
        out = (result.stderr or "") + (result.stdout or "")
        raise RuntimeError((out or "erro desconhecido")[-800:])
    return result


# ------------------------------------------------------------------
# Upload do ZIP como dataset (inclui montador.py junto)
# ------------------------------------------------------------------
def upload_dataset(
    zip_path: Path,
    project_name: str,
    username: str,
    token: str,
    project_id: int | None = None,
) -> str:
    slug = dataset_slug(project_name, project_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        shutil.copy2(zip_path, tmp / zip_path.name)

        montador_src = ROOT / MONTADOR_FILENAME
        if montador_src.exists():
            shutil.copy2(montador_src, tmp / MONTADOR_FILENAME)

        metadata = {
            "title": f"B-rolls {project_name}"[:50],
            "id": f"{username}/{slug}",
            "subtitle": "Private render pack — see LICENSES.md for asset licenses.",
            "licenses": [{"name": "other"}],
        }
        (tmp / "dataset-metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )

        # tenta nova versão primeiro; se não existir, cria
        try:
            _run(
                ["datasets", "version", "-p", str(tmp), "-m", "update"],
                username, token, timeout=300,
            )
        except RuntimeError:
            _run(
                ["datasets", "create", "-p", str(tmp)],
                username, token, timeout=300,
            )

    _wait_dataset_ready(slug, zip_path.name, username, token)
    return slug


def _wait_dataset_ready(
    slug: str,
    expected_file: str,
    username: str,
    token: str,
    timeout: int = 180,
    poll: int = 10,
) -> None:
    """Aguarda o Kaggle terminar de processar o dataset antes de empurrar o kernel.

    Kaggle indexa datasets de forma assincrona; se o kernel comecar antes do
    processamento terminar, /kaggle/input/ aparece vazio.
    """
    deadline = time.time() + timeout
    print(f"[Kaggle] aguardando dataset {slug} ficar pronto (max {timeout}s)...")
    while time.time() < deadline:
        try:
            r = _run(
                ["datasets", "files", f"{username}/{slug}"],
                username, token, timeout=30,
            )
            output = (r.stdout or "") + (r.stderr or "")
            if expected_file in output:
                print(f"[Kaggle] dataset pronto ({expected_file} visivel)")
                return
        except Exception:
            pass
        time.sleep(poll)
    # Se timeout, tenta assim mesmo — pode funcionar dependendo do tamanho
    print("[Kaggle] timeout aguardando dataset; tentando kernel assim mesmo")


# ------------------------------------------------------------------
# Kernel — runner.py leve, montador vem do dataset
# ------------------------------------------------------------------
_RUNNER = """\
import json
import os
import select
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path


BASE_VIDEO_NAME = "video_broll_base.mp4"
MASTER_VIDEO_NAME = "final_master.mp4"
HYPERFRAMES_VERSION = (os.environ.get("PRODUCER_HYPERFRAMES_VERSION") or "0.6.93").strip() or "0.6.93"
HYPERFRAMES_PACKAGE = "hyperframes@" + HYPERFRAMES_VERSION
HYPERFRAMES_CMD = ["npx", "-y", "--package", "node@22", "--package", HYPERFRAMES_PACKAGE, "hyperframes"]
RUN_TIMINGS = []
VIDEO_FRAME_SAMPLES_DIR = Path("/kaggle/working/video_frame_samples")
VIDEO_FRAME_SAMPLES_MANIFEST = Path("/kaggle/working/metadata/video_frame_samples.json")
VIDEO_FRAME_SAMPLES = {}
MAX_FRAME_SAMPLED_VIDEOS = int(os.environ.get("PRODUCER_FRAME_SAMPLE_MAX_VIDEOS", "80") or "80")
CHROME_LIBS = [
    "libatk-bridge2.0-0",
    "libatk1.0-0",
    "libatspi2.0-0",
    "libcups2",
    "libdbus-1-3",
    "libdrm2",
    "libxkbcommon0",
    "libxcomposite1",
    "libxdamage1",
    "libxfixes3",
    "libxrandr2",
    "libgbm1",
    "libasound2",
    "libpango-1.0-0",
    "libcairo2",
    "libnss3",
    "libnspr4",
    "libxshmfence1",
    "libgtk-3-0",
    "libx11-xcb1",
    "fonts-liberation",
    "fonts-noto-color-emoji",
    "fonts-noto-core",
    "fontconfig",
]


def run_logged(cmd, cwd=None, timeout=None, env=None):
    print("$ " + " ".join(str(c) for c in cmd))
    started = time.monotonic()
    cmd_label = " ".join(str(c) for c in cmd[:4])
    process = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        bufsize=1,
    )
    chunks = []
    try:
        while True:
            if process.stdout:
                ready, _, _ = select.select([process.stdout], [], [], 1)
                if ready:
                    line = process.stdout.readline()
                    if line:
                        chunks.append(line)
                        print(line, end="", flush=True)
            if process.poll() is not None:
                if process.stdout:
                    rest = process.stdout.read()
                    if rest:
                        chunks.append(rest)
                        print(rest, end="", flush=True)
                break
            if timeout and (time.monotonic() - started) > timeout:
                process.kill()
                elapsed = round(time.monotonic() - started, 3)
                RUN_TIMINGS.append({"stage": "command_timeout", "command": cmd_label, "seconds": elapsed})
                raise TimeoutError("Comando excedeu timeout de " + str(timeout) + "s: " + " ".join(str(c) for c in cmd))
    finally:
        if process.stdout:
            process.stdout.close()
    output = "".join(chunks)
    result = subprocess.CompletedProcess(cmd, process.returncode, output, "")
    RUN_TIMINGS.append(
        {
            "stage": "command",
            "command": cmd_label,
            "seconds": round(time.monotonic() - started, 3),
            "returncode": result.returncode,
        }
    )
    if result.returncode != 0:
        raise RuntimeError("Comando falhou (exit " + str(result.returncode) + "): " + " ".join(str(c) for c in cmd))
    return result


def mark_timing(stage, started, **extra):
    item = {"stage": stage, "seconds": round(time.monotonic() - started, 3)}
    item.update(extra)
    RUN_TIMINGS.append(item)


def command_output(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "comando indisponivel").strip())
    return (result.stdout or result.stderr or "").strip()


def optional_command_output(cmd):
    try:
        return command_output(cmd)
    except Exception as exc:
        return "indisponivel: " + str(exc)


def ffprobe_duration(video_path):
    result = run_logged(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        timeout=120,
    )
    try:
        return max(float((result.stdout or "0").strip()), 0.1)
    except ValueError:
        return 1.0


def safe_sample_name(text):
    cleaned = "".join(c if c.isalnum() else "_" for c in str(text or ""))
    cleaned = cleaned.strip("_")[:64]
    return cleaned or "scene"


def _safe_selected_asset_path(selected_asset):
    rel = str(selected_asset or "").replace(chr(92), "/").lstrip("/")
    parts = [p for p in rel.split("/") if p]
    if len(parts) < 2 or parts[0] != "assets" or any(p in ("..", ".") for p in parts):
        return ""
    return "/".join(parts)


def load_guide_from_source(source):
    source = Path(source)
    if source.is_file():
        with zipfile.ZipFile(source) as zf:
            return json.loads(zf.read("guia_visual.json").decode("utf-8"))
    guide_path = source / "guia_visual.json"
    return json.loads(guide_path.read_text(encoding="utf-8"))


def extract_selected_asset(source, selected_asset, dest):
    rel = _safe_selected_asset_path(selected_asset)
    if not rel:
        return False
    source = Path(source)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.is_file():
        with zipfile.ZipFile(source) as zf:
            names = set(zf.namelist())
            if rel not in names:
                return False
            dest.write_bytes(zf.read(rel))
            return dest.exists() and dest.stat().st_size > 0
    src = source / rel
    if not src.exists() or not src.is_file():
        return False
    shutil.copy2(src, dest)
    return dest.exists() and dest.stat().st_size > 0


def frame_sample_offsets(duration, max_frames):
    max_frames = max(1, min(int(max_frames or 3), 3))
    duration = max(float(duration or 0), 0.1)
    if duration < 2.5 or max_frames == 1:
        return [max(0.1, duration * 0.5)]
    positions = [0.25, 0.5, 0.75][:max_frames]
    return [max(0.1, min(duration - 0.1, duration * pos)) for pos in positions]


def sample_finalist_video_frames(source):
    global VIDEO_FRAME_SAMPLES
    started = time.monotonic()
    manifest = {
        "status": "empty",
        "policy": {
            "scope": "selected_video_assets_only",
            "max_frames_per_video": 3,
            "positions": [0.25, 0.5, 0.75],
            "max_videos": MAX_FRAME_SAMPLED_VIDEOS,
        },
        "samples": [],
        "errors": [],
    }
    try:
        guide = load_guide_from_source(source)
    except Exception as exc:
        manifest["status"] = "error"
        manifest["errors"].append({"stage": "load_guide", "error": str(exc)[:300]})
        VIDEO_FRAME_SAMPLES_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        VIDEO_FRAME_SAMPLES_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        VIDEO_FRAME_SAMPLES = manifest
        return manifest

    VIDEO_FRAME_SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_FRAME_SAMPLES_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    sampled_videos = 0
    sampled_frames = 0
    for scene in guide.get("scenes") or []:
        selected_asset = scene.get("selected_asset")
        if not selected_asset or scene.get("asset_type") != "video":
            continue
        policy = scene.get("video_frame_sampling") or {}
        if policy and policy.get("enabled") is False:
            continue
        if sampled_videos >= MAX_FRAME_SAMPLED_VIDEOS:
            manifest["errors"].append({"stage": "cap", "error": "limite global de videos amostrados atingido"})
            break
        scene_id = safe_sample_name(scene.get("id") or ("scene_" + str(sampled_videos + 1)))
        source_ext = Path(str(selected_asset)).suffix.lower() or ".mp4"
        temp_video = VIDEO_FRAME_SAMPLES_DIR / ("_source_" + scene_id + source_ext)
        entry = {
            "scene_id": scene.get("id") or "",
            "selected_asset": selected_asset,
            "asset_type": "video",
            "video_frame_verdict": "fallback_thumbnail",
            "sampled_frames": 0,
            "frames": [],
            "reason": "",
        }
        try:
            if not extract_selected_asset(source, selected_asset, temp_video):
                entry["reason"] = "asset selecionado nao encontrado no pacote"
                manifest["samples"].append(entry)
                continue
            duration = ffprobe_duration(temp_video)
            offsets = frame_sample_offsets(duration, policy.get("max_frames") or 3)
            for frame_idx, offset in enumerate(offsets, start=1):
                frame_name = scene_id + "_frame_%02d.jpg" % frame_idx
                frame_path = VIDEO_FRAME_SAMPLES_DIR / frame_name
                try:
                    run_logged(
                        [
                            "ffmpeg",
                            "-y",
                            "-ss",
                            "%.3f" % offset,
                            "-i",
                            str(temp_video),
                            "-frames:v",
                            "1",
                            "-vf",
                            "scale=640:-2:force_original_aspect_ratio=decrease",
                            "-q:v",
                            "3",
                            str(frame_path),
                        ],
                        timeout=90,
                    )
                    if frame_path.exists() and frame_path.stat().st_size > 0:
                        rel_frame = "video_frame_samples/" + frame_name
                        entry["frames"].append({"time": round(offset, 3), "file": rel_frame})
                        sampled_frames += 1
                except Exception as frame_exc:
                    manifest["errors"].append(
                        {"stage": "frame", "scene_id": entry["scene_id"], "error": str(frame_exc)[:300]}
                    )
            entry["sampled_frames"] = len(entry["frames"])
            if entry["sampled_frames"]:
                entry["video_frame_verdict"] = "sampled"
                entry["reason"] = "frames reais extraidos do video finalista"
            else:
                entry["reason"] = entry["reason"] or "ffmpeg nao conseguiu extrair frames"
        except Exception as exc:
            entry["reason"] = str(exc)[:300]
            manifest["errors"].append({"stage": "video", "scene_id": entry["scene_id"], "error": str(exc)[:300]})
        finally:
            temp_video.unlink(missing_ok=True)
        sampled_videos += 1
        manifest["samples"].append(entry)

    if sampled_frames:
        manifest["status"] = "sampled"
    elif manifest["samples"]:
        manifest["status"] = "fallback_thumbnail"
    VIDEO_FRAME_SAMPLES_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    VIDEO_FRAME_SAMPLES = manifest
    mark_timing("video_frame_sampling", started, sampled_videos=sampled_videos, sampled_frames=sampled_frames)
    print("Video frame samples:", VIDEO_FRAME_SAMPLES_MANIFEST.read_text(encoding="utf-8"))
    return manifest


def write_status(payload):
    status_path = Path("/kaggle/working/hyperframes_status.json")
    status_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("HyperFrames status:", status_path.read_text(encoding="utf-8"))


def hyperframes_render_args(output_path, render_format="mp4"):
    args = HYPERFRAMES_CMD + [
        "render",
        "--output",
        str(output_path),
        "--quality",
        "standard",
        "--fps",
        str(RENDER_FPS),
        "--workers",
        str(RENDER_WORKERS),
        "--low-memory-mode",
        "--no-browser-gpu",
        "--protocol-timeout",
        "900000",
        "--browser-timeout",
        "180",
        "--player-ready-timeout",
        "180000",
    ]
    if render_format != "mp4":
        args.extend(["--format", render_format])
    return args


def encode_png_sequence(frames_dir, master_out):
    pngs = sorted(frames_dir.rglob("*.png"))
    if not pngs:
        raise RuntimeError("HyperFrames png-sequence nao gerou frames em " + str(frames_dir))
    if master_out.exists():
        master_out.unlink()
    direct_pattern = frames_dir / "*.png"
    if list(frames_dir.glob("*.png")):
        run_logged(
            [
                "ffmpeg",
                "-y",
                "-framerate",
                str(RENDER_FPS),
                "-pattern_type",
                "glob",
                "-i",
                str(direct_pattern),
                "-vf",
                "format=yuv420p",
                "-r",
                str(OUTPUT_FPS),
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-movflags",
                "+faststart",
                str(master_out),
            ],
            timeout=1200,
        )
    else:
        manifest = Path("/kaggle/working/hyperframes_frames.txt")
        lines = []
        for frame in pngs:
            escaped = str(frame).replace("'", "'\\''")
            lines.append("file '" + escaped + "'")
        manifest.write_text("\\n".join(lines) + "\\n", encoding="utf-8")
        run_logged(
            [
                "ffmpeg",
                "-y",
                "-r",
                str(RENDER_FPS),
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(manifest),
                "-vf",
                "format=yuv420p",
                "-r",
                str(OUTPUT_FPS),
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-movflags",
                "+faststart",
                str(master_out),
            ],
            timeout=1200,
        )
    return len(pngs)


def timeout_from_env(name, default_seconds):
    raw = os.environ.get(name)
    if not raw:
        return int(default_seconds)
    try:
        return max(60, int(float(raw)))
    except ValueError:
        print("Valor invalido para", name + ":", raw, "usando", default_seconds)
        return int(default_seconds)


def hyperframes_timeout(duration, mode):
    if mode == "mp4":
        default_seconds = max(300, min(900, int(duration * 8 + 180)))
        return timeout_from_env("PRODUCER_HF_MP4_TIMEOUT_SECONDS", default_seconds)
    default_seconds = max(900, min(3600, int(duration * 20 + 300)))
    return timeout_from_env("PRODUCER_HF_PNG_TIMEOUT_SECONDS", default_seconds)


def env_enabled(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def assert_node_runtime():
    print("Node do sistema:", optional_command_output(["node", "--version"]))
    print("npm:", optional_command_output(["npm", "--version"]))
    print("npx:", command_output(["npx", "--version"]))
    # Pre-warm node@22 + hyperframes em paralelo para evitar download duplo no lint/render
    node_version = command_output(["npx", "-y", "--package", "node@22", "--package", HYPERFRAMES_PACKAGE, "node", "--version"])
    major = int(node_version.strip().lstrip("v").split(".", 1)[0])
    print("Node para HyperFrames:", node_version)
    if major < 22:
        raise RuntimeError("HyperFrames precisa de Node.js 22+; npx retornou " + node_version)


def find_system_chrome():
    for candidate in ["/usr/bin/chromium", "/usr/bin/google-chrome"]:
        path = Path(candidate)
        if path.exists():
            return str(path)
    for name in ["chromium", "google-chrome"]:
        result = subprocess.run(["which", name], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()[0]
    return ""


def _chrome_libs_installed():
    result = subprocess.run(["dpkg", "-l", "libatk1.0-0"], capture_output=True)
    return result.returncode == 0


def install_chrome_libs():
    if not Path("/usr/bin/apt-get").exists():
        print("apt-get indisponivel; nao da para instalar bibliotecas do Chrome.")
        return
    if _chrome_libs_installed():
        print("Bibliotecas do Chrome ja instaladas; pulando apt-get.")
        return
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    try:
        print("Instalando bibliotecas necessarias para Chrome/headless...")
        try:
            run_logged(["apt-get", "install", "-y", "--no-install-recommends", *CHROME_LIBS], timeout=360, env=env)
        except Exception as first_exc:
            print("Instalacao direta das libs falhou; tentando apt-get update antes de repetir:", first_exc)
            run_logged(["apt-get", "update"], timeout=180, env=env)
            run_logged(["apt-get", "install", "-y", "--no-install-recommends", *CHROME_LIBS], timeout=360, env=env)
    except Exception as exc:
        print("Aviso: nao foi possivel instalar bibliotecas do Chrome:", exc)


def ensure_system_chrome():
    existing = find_system_chrome()
    if existing:
        print("Chrome/Chromium do sistema:", existing)
        return existing
    print("Nenhum Chrome/Chromium de sistema confiavel; usando chrome-headless-shell do HyperFrames.")
    return find_system_chrome()


EDIT_PLAN_NAME = "edit_plan.json"
PACK_EXTRAS_DIR = Path("/kaggle/working/pack_extras")
NL = chr(10)

RENDER_FPS = 15
OUTPUT_FPS = 30
RENDER_WORKERS = 1

COMPOSITION_CSS = (
    "html, body { margin: 0; width: 100%; height: 100%; background: #050708; overflow: hidden; }"
    " #root { position: relative; background: #050708; overflow: hidden; }"
    " #motion-wrap { position: absolute; inset: 0; will-change: transform; }"
    " .base-video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }"
    " .avatar-base { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }"
    " .broll-clip { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover;"
    " opacity: 0; will-change: transform, opacity; }"
    " .fadeov { position: absolute; inset: 0; background: #000; opacity: 0; pointer-events: none; }"
    " .caption { position: absolute; bottom: 92px; max-width: 40%; padding: 14px 22px;"
    " background: rgba(5, 8, 10, 0.72); border-left: 4px solid #34d2b2; color: #f2f7f5;"
    " font-family: Arial, Helvetica, sans-serif; font-size: 34px; line-height: 1.22;"
    " font-weight: 650; border-radius: 8px; opacity: 0; }"
    " .caption.pos-left { left: 72px; } .caption.pos-right { right: 72px; }"
    " .avatar-clip { position: absolute; bottom: 0; object-fit: contain; }"
    " .avatar-clip.pos-right { right: 24px; } .avatar-clip.pos-left { left: 24px; }"
)

OVERLAY_ONLY_CSS = (
    "html, body { margin: 0; width: 100%; height: 100%; background: #00FFFF; overflow: hidden; }"
    " #root { position: relative; background: #00FFFF; overflow: hidden; }"
    " .fadeov { position: absolute; inset: 0; background: #000; opacity: 0; pointer-events: none; }"
    " .caption { position: absolute; bottom: 92px; max-width: 40%; padding: 14px 22px;"
    " background: rgba(5, 8, 10, 0.72); border-left: 4px solid #34d2b2; color: #f2f7f5;"
    " font-family: Arial, Helvetica, sans-serif; font-size: 34px; line-height: 1.22;"
    " font-weight: 650; border-radius: 8px; opacity: 0; }"
    " .caption.pos-left { left: 72px; } .caption.pos-right { right: 72px; }"
)
OVERLAY_CHROMA_KEY = "0x00FFFF"

NARRATION_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")
AVATAR_EXTS = (".webm", ".mov", ".mp4")


def classify_pack_extra(filename):
    base = str(filename).lower().replace(chr(92), "/").rsplit("/", 1)[-1]
    stem, _, ext = base.rpartition(".")
    ext = "." + ext if ext else ""
    if base == EDIT_PLAN_NAME:
        return "edit_plan"
    if stem == "narration" and ext in NARRATION_EXTS:
        return "narration"
    if stem == "avatar" and ext in AVATAR_EXTS:
        return "avatar"
    return ""


def find_pack_extras(input_root):
    # Localiza edit_plan.json, narracao e avatar no input (pasta ou dentro de ZIPs).
    import zipfile
    extras = {"edit_plan": None, "narration": None, "avatar": None}
    for path in input_root.rglob("*"):
        if path.is_file():
            kind = classify_pack_extra(path.name)
            if kind and not extras[kind]:
                extras[kind] = path
    if not all(extras.values()):
        for z in input_root.rglob("*.zip"):
            try:
                with zipfile.ZipFile(z) as zf:
                    for member in zf.namelist():
                        kind = classify_pack_extra(member)
                        if kind and not extras[kind]:
                            PACK_EXTRAS_DIR.mkdir(parents=True, exist_ok=True)
                            target = PACK_EXTRAS_DIR / Path(member).name
                            with zf.open(member) as src, open(target, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            extras[kind] = target
            except Exception as exc:
                print("Aviso: falha lendo zip para extras:", exc)
    plan = None
    if extras["edit_plan"]:
        try:
            plan = json.loads(Path(extras["edit_plan"]).read_text(encoding="utf-8"))
        except Exception as exc:
            print("Aviso: edit_plan.json invalido, refinando sem plano:", exc)
    return plan, extras["narration"], extras["avatar"]


def avatar_requested(edit_plan, avatar):
    if edit_plan and "avatar_required" in edit_plan:
        return bool(edit_plan.get("avatar_required"))
    return bool(avatar)


def ensure_avatar_contract(edit_plan, avatar):
    if not avatar_requested(edit_plan, avatar):
        return
    if not avatar:
        raise RuntimeError("avatar obrigatorio no edit_plan, mas avatar.* nao foi encontrado no pacote")
    plan_avatar = (edit_plan or {}).get("avatar") or {}
    if edit_plan and "avatar_required" in edit_plan and not plan_avatar.get("src"):
        raise RuntimeError("avatar obrigatorio sem edit_plan.avatar.src")
    if plan_avatar.get("src") and Path(str(plan_avatar.get("src"))).name != Path(avatar).name:
        raise RuntimeError("edit_plan.avatar.src nao confere com o arquivo de avatar do pacote")


def assert_avatar_satisfied(postprocess):
    if postprocess.get("requested_avatar") and not postprocess.get("avatar"):
        raise RuntimeError("avatar solicitado, mas o master nao confirmou avatar")


def plan_resolution(edit_plan):
    raw = str((edit_plan or {}).get("resolution") or "1280x720")
    try:
        w, h = raw.lower().split("x", 1)
        w, h = max(int(w), 16), max(int(h), 16)
        return min(w, 1280), min(h, 720)
    except ValueError:
        return 1280, 720


def plan_scenes_within(edit_plan, duration):
    cleaned = []
    for s in (edit_plan or {}).get("scenes") or []:
        try:
            start = max(float(s.get("start", 0)), 0.0)
            dur = float(s.get("duration", 0))
        except (TypeError, ValueError):
            continue
        if dur <= 0 or start >= duration:
            continue
        dur = min(dur, duration - start)
        try:
            cap_start = float(s.get("caption_start") if s.get("caption_start") is not None else start)
        except (TypeError, ValueError):
            cap_start = start
        try:
            cap_duration = float(s.get("caption_duration") or 0)
        except (TypeError, ValueError):
            cap_duration = 0
        cap_start = min(max(cap_start, start), start + max(dur - 0.2, 0.0))
        if cap_duration <= 0:
            cap_duration = max(min(dur - (cap_start - start) - 0.2, 2.2), 0.0)
        cap_duration = max(min(cap_duration, duration - cap_start), 0.0)
        cleaned.append(
            {
                "start": start,
                "duration": dur,
                "motion": str(s.get("motion") or "hold"),
                "transition_out": str(s.get("transition_out") or "none"),
                "caption": str(s.get("caption") or "").strip(),
                "caption_start": cap_start,
                "caption_duration": cap_duration,
                "broll": bool(s.get("broll")),
            }
        )
    cleaned.sort(key=lambda s: s["start"])
    return cleaned


MAX_AVATAR_SOLO_SECONDS = 30.0
BROLL_FADE_SECONDS = 0.3
# Pausas de narracao entre cenas broll consecutivas que NAO devem revelar o
# avatar: a janela e estendida para cobri-las (sequencia de b-roll continua).
BROLL_MERGE_MAX_GAP = 1.2


def broll_windows(scenes, master_duration, base_duration):
    # Junta cenas broll consecutivas em janelas continuas de overlay.
    #
    # A narracao tem micro-pausas (~0.3s) entre cenas; cada cena tem
    # start=start_time real. Se nao mesclassemos atravessando essas pausas, duas
    # cenas broll seguidas viravam DUAS janelas, cada uma com fade-out -> avatar
    # -> fade-in, causando o "piscar" do avatar entre b-rolls. Mesclamos quando o
    # gap ate a proxima cena broll e pequeno (<= BROLL_MERGE_MAX_GAP), estendendo
    # a janela para cobrir a pausa: a sequencia de b-roll fica continua e sem
    # fade interno (o fade fica so na borda avatar<->broll).
    windows = []
    current = None
    for s in scenes:
        if not s.get("broll"):
            if current:
                windows.append(current)
                current = None
            continue
        s_end = s["start"] + s["duration"]
        if current and (s["start"] - current["end"]) <= BROLL_MERGE_MAX_GAP:
            current["end"] = s_end
            current["scenes"].append(s)
        else:
            if current:
                windows.append(current)
            current = {"start": s["start"], "end": s_end, "scenes": [s]}
    if current:
        windows.append(current)
    cleaned = []
    for w in windows:
        start = max(min(w["start"], master_duration), 0.0)
        end = min(w["end"], master_duration, base_duration)
        if end - start < 0.4:
            continue
        cleaned.append({"start": start, "end": end, "duration": end - start, "scenes": w["scenes"]})
    return cleaned


def enforce_avatar_solo_guard(windows, master_duration, base_duration):
    # Rede de seguranca: se mesmo assim sobrar um trecho de avatar sozinho
    # maior que 30s, abre uma janela de b-roll no meio do trecho.
    guarded = sorted(windows, key=lambda w: w["start"])
    result = []
    cursor = 0.0
    for w in guarded + [{"start": master_duration, "end": master_duration, "duration": 0, "scenes": []}]:
        gap = w["start"] - cursor
        if gap > MAX_AVATAR_SOLO_SECONDS and base_duration > 1.0:
            mid = cursor + gap / 2
            ins_dur = min(8.0, gap - MAX_AVATAR_SOLO_SECONDS + 8.0, base_duration)
            ins_start = max(cursor + 1.0, mid - ins_dur / 2)
            ins_end = min(ins_start + ins_dur, w["start"] - 0.5, base_duration)
            if ins_end - ins_start >= 1.0:
                result.append(
                    {
                        "start": ins_start,
                        "end": ins_end,
                        "duration": ins_end - ins_start,
                        "scenes": [{"start": ins_start, "duration": ins_end - ins_start, "motion": "slow_push_in"}],
                        "auto_guard": True,
                    }
                )
        if w["duration"] > 0:
            result.append(w)
        cursor = max(cursor, w["end"])
    return sorted(result, key=lambda w: w["start"])


def motion_tweens(target, scenes, base_scale=1.0):
    # immediateRender: false evita o "pulo duplo" no inicio de cada cena:
    # sem ele o GSAP pre-renderiza o estado from do ultimo fromTo do alvo,
    # fazendo a imagem aparecer reenquadrada duas vezes seguidas.
    tweens = []
    for s in scenes:
        s_start = "%.3f" % s["start"]
        s_dur = "%.3f" % s["duration"]
        motion = s.get("motion") or "hold"
        push = "%.3f" % (base_scale * 1.05)
        drift = "%.3f" % (base_scale * 1.035)
        base = "%.3f" % base_scale
        if motion == "slow_push_in":
            tweens.append(
                'tl.fromTo("' + target + '", { scale: ' + base + ', xPercent: 0 }, { scale: ' + push
                + ', xPercent: 0, duration: ' + s_dur + ', ease: "none", immediateRender: false }, ' + s_start + ");"
            )
        elif motion == "slow_pull_out":
            tweens.append(
                'tl.fromTo("' + target + '", { scale: ' + push + ', xPercent: 0 }, { scale: ' + base
                + ', xPercent: 0, duration: ' + s_dur + ', ease: "none", immediateRender: false }, ' + s_start + ");"
            )
        elif motion == "drift_left":
            tweens.append(
                'tl.fromTo("' + target + '", { scale: ' + drift + ', xPercent: 0.8 }, { scale: ' + drift
                + ', xPercent: -0.8, duration: ' + s_dur + ', ease: "none", immediateRender: false }, ' + s_start + ");"
            )
        elif motion == "drift_right":
            tweens.append(
                'tl.fromTo("' + target + '", { scale: ' + drift + ', xPercent: -0.8 }, { scale: ' + drift
                + ', xPercent: 0.8, duration: ' + s_dur + ', ease: "none", immediateRender: false }, ' + s_start + ");"
            )
        elif motion == "hold":
            tweens.append(
                'tl.set("' + target + '", { scale: ' + base + ', xPercent: 0 }, ' + s_start + ");"
            )
    return tweens


_GSAP_CDN = "https://cdn.jsdelivr.net/npm/gsap@3/dist/gsap.min.js"
_GSAP_CACHE = Path("/kaggle/working/.gsap_cache.js")


def _gsap_script_tag(assets_dir):
    # Serve GSAP como arquivo externo (src=) para o linter nao escanear o fonte
    # e disparar o erro non_deterministic_code pelo Math.random() interno do GSAP.
    if not _GSAP_CACHE.exists():
        try:
            import urllib.request
            urllib.request.urlretrieve(_GSAP_CDN, str(_GSAP_CACHE))
        except Exception as exc:
            print("Aviso: nao foi possivel baixar GSAP; usando CDN:", exc)
            return '<script src="' + _GSAP_CDN + '"></script>'
    shutil.copy2(str(_GSAP_CACHE), str(assets_dir / "gsap.min.js"))
    return '<script src="./assets/gsap.min.js"></script>'


def avatar_corner_filter(width, height, edit_plan):
    plan_avatar = (edit_plan or {}).get("avatar") or {}
    pos = "left" if str(plan_avatar.get("position") or "right") == "left" else "right"
    try:
        scale = float(plan_avatar.get("scale") or 0.30)
    except (TypeError, ValueError):
        scale = 0.30
    scale = min(max(scale, 0.10), 0.60)
    avatar_w = int(width * scale)
    x = "24" if pos == "left" else "W-w-24"
    y = "H-h"
    return avatar_w, x, y


def ffmpeg_compose_base_layers(base_video, avatar, windows, out_path, duration, width, height):
    # FFmpeg: avatar full-screen + b-roll windows com fade. Substitui video-in-browser.
    inputs = ["-y"]
    filter_parts = []
    inputs += ["-stream_loop", "-1", "-t", "%.3f" % duration, "-i", str(avatar)]
    filter_parts.append(
        "[0:v]scale=" + str(width) + ":" + str(height)
        + ":force_original_aspect_ratio=increase,crop=" + str(width) + ":" + str(height)
        + ",setsar=1[base]"
    )
    current = "base"
    for k, w in enumerate(windows):
        in_idx = k + 1
        w_start = w["start"]
        w_dur = w["duration"]
        fade = min(BROLL_FADE_SECONDS, w_dur / 4)
        inputs += ["-ss", "%.3f" % w_start, "-t", "%.3f" % w_dur, "-i", str(base_video)]
        br = "br" + str(k)
        out = "c" + str(k)
        filter_parts.append(
            "[" + str(in_idx) + ":v]"
            "scale=" + str(width) + ":" + str(height)
            + ":force_original_aspect_ratio=increase,crop=" + str(width) + ":" + str(height)
            + ",setsar=1,format=rgba"
            + ",fade=t=in:st=0:d=%.3f:alpha=1" % fade
            + ",fade=t=out:st=%.3f:d=%.3f:alpha=1" % (max(w_dur - fade, 0.01), fade)
            + ",setpts=PTS+%.3f/TB" % w_start
            + "[" + br + "]"
        )
        filter_parts.append(
            "[" + current + "][" + br + "]"
            "overlay=format=auto:x=0:y=0"
            ":enable='between(t,%.3f,%.3f)'" % (w_start, w_start + w_dur)
            + "[" + out + "]"
        )
        current = out
    cmd = ["ffmpeg"] + inputs + [
        "-filter_complex", ";".join(filter_parts),
        "-map", "[" + current + "]",
        "-r", str(RENDER_FPS),
        "-t", "%.3f" % duration,
        "-an",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    run_logged(cmd, timeout=1200)


def ffmpeg_compose_corner_layers(base_video, avatar, out_path, duration, width, height, edit_plan):
    inputs = ["-y", "-stream_loop", "-1", "-t", "%.3f" % duration, "-i", str(base_video)]
    filters = [
        "[0:v]scale=" + str(width) + ":" + str(height)
        + ":force_original_aspect_ratio=increase,crop=" + str(width) + ":" + str(height)
        + ",setsar=1[base]"
    ]
    current = "base"
    if avatar:
        avatar_w, x, y = avatar_corner_filter(width, height, edit_plan)
        inputs += ["-stream_loop", "-1", "-t", "%.3f" % duration, "-i", str(avatar)]
        filters.append("[1:v]scale=" + str(avatar_w) + ":-2,format=rgba[avatar]")
        filters.append("[base][avatar]overlay=" + x + ":" + y + ":eof_action=pass:shortest=0[out]")
        current = "out"
    cmd = ["ffmpeg"] + inputs + [
        "-filter_complex", ";".join(filters),
        "-map", "[" + current + "]",
        "-r", str(OUTPUT_FPS),
        "-t", "%.3f" % duration,
        "-an",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out_path),
    ]
    run_logged(cmd, timeout=1200)


def ffmpeg_overlay_captions(base_video, frames_dir, out_path):
    # Overlay de captions (chroma-keyed PNG sequence) sobre o video base.
    pngs = sorted(frames_dir.rglob("*.png"))
    if not pngs:
        print("Aviso: sem frames de caption; copiando base diretamente.")
        shutil.copy2(str(base_video), str(out_path))
        return
    if list(frames_dir.glob("*.png")):
        cap_input = ["-pattern_type", "glob", "-framerate", str(RENDER_FPS), "-i", str(frames_dir / "*.png")]
    else:
        manifest = Path("/kaggle/working/caption_frames.txt")
        lines = ["file '" + str(f).replace("'", "'\\''") + "'" for f in pngs]
        manifest.write_text("\\n".join(lines) + "\\n", encoding="utf-8")
        cap_input = ["-r", str(RENDER_FPS), "-f", "concat", "-safe", "0", "-i", str(manifest)]
    run_logged(
        [
            "ffmpeg", "-y",
            "-i", str(base_video),
        ] + cap_input + [
            "-filter_complex",
            "[1:v]colorkey=" + OVERLAY_CHROMA_KEY + ":0.15:0.05[keyed];"
            "[0:v][keyed]overlay=format=auto:x=0:y=0:shortest=1[out]",
            "-map", "[out]",
            "-an",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
            str(out_path),
        ],
        timeout=1200,
    )


def _caption_fontfile():
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]:
        if os.path.exists(p):
            return p
    return ""


def build_drawtext_filter(caps, pos, width, height, fontfile, cap_dir):
    # Monta o filtergraph de legendas (lower-third) com fade in/out via alpha.
    # Usa textfile= para nao precisar escapar o texto da legenda.
    fontsize = max(28, int(height * 0.05))
    margin = max(48, int(width * 0.05))
    bottom = max(60, int(height * 0.10))
    parts = []
    for i, (text, start, dur) in enumerate(caps):
        tf = Path(cap_dir) / ("cap_%d.txt" % i)
        tf.write_text(text, encoding="utf-8")
        end = start + dur
        fin = min(0.30, dur / 3.0)
        fout = min(0.30, dur / 3.0)
        x = ("w-tw-%d" % margin) if pos == "right" else ("%d" % margin)
        y = "h-th-%d" % bottom
        alpha = ("if(lt(t,%g),(t-%g)/%g,if(gt(t,%g),(%g-t)/%g,1))"
                 % (start + fin, start, fin, end - fout, end, fout))
        parts.append(
            "drawtext=fontfile=" + fontfile + ":textfile=" + str(tf)
            + ":fontsize=" + str(fontsize)
            + ":fontcolor=white:box=1:boxcolor=0x050708@0.72:boxborderw=16:line_spacing=6"
            + ":x=" + x + ":y=" + y
            + ":alpha='" + alpha + "':enable='between(t," + ("%g" % start) + "," + ("%g" % end) + ")'"
        )
    return ",".join(parts)


def ffmpeg_drawtext_captions(base_video, edit_plan, duration, out_path):
    # Legendas/lower-thirds desenhadas direto pelo FFmpeg (sem Chrome/HyperFrames):
    # deterministico, rapido e sem risco de pretar o video. Substitui o overlay
    # por png-sequence + chroma-key, que era fragil.
    scenes = plan_scenes_within(edit_plan, duration)
    caps = []
    for s in scenes:
        if s.get("caption"):
            caps.append((str(s["caption"]), float(s.get("caption_start") or 0),
                         max(float(s.get("caption_duration") or 0), 0.4)))
    if not caps:
        raise RuntimeError("sem captions para drawtext")
    fontfile = _caption_fontfile()
    if not fontfile:
        raise RuntimeError("nenhuma fonte TTF encontrada para drawtext")
    pos = "right" if str((edit_plan or {}).get("caption_position") or "left") == "right" else "left"
    width, height = plan_resolution(edit_plan)
    cap_dir = Path("/kaggle/working/captions")
    if cap_dir.exists():
        shutil.rmtree(cap_dir)
    cap_dir.mkdir(parents=True, exist_ok=True)
    vf = build_drawtext_filter(caps, pos, width, height, fontfile, cap_dir)
    if out_path.exists():
        out_path.unlink()
    run_logged(
        [
            "ffmpeg", "-y", "-i", str(base_video),
            "-vf", vf,
            "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            str(out_path),
        ],
        timeout=1800,
    )
    print("Legendas drawtext aplicadas: %d" % len(caps))


def write_hyperframes_project(base_video, project_dir, edit_plan=None, narration=None, avatar=None, avatar_mode="none", text_overlay_only=False):
    import html as html_escape_mod
    if project_dir.exists():
        shutil.rmtree(project_dir)
    assets_dir = project_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(base_video, assets_dir / BASE_VIDEO_NAME)
    narration_name = ""
    if narration:
        narration_name = Path(narration).name
        shutil.copy2(narration, assets_dir / narration_name)
    avatar_name = ""
    if avatar:
        avatar_name = Path(avatar).name
        shutil.copy2(avatar, assets_dir / avatar_name)
    if not avatar_name:
        avatar_mode = "none"

    base_dur = ffprobe_duration(base_video)
    # No modo base o avatar comanda a duracao do master: nada do material
    # original e cortado no final (a base de b-roll pode ser mais curta).
    if avatar_mode == "base":
        duration = ffprobe_duration(avatar)
    else:
        duration = base_dur
    width, height = plan_resolution(edit_plan)
    scenes = plan_scenes_within(edit_plan, duration)
    caption_pos = "right" if str((edit_plan or {}).get("caption_position") or "left") == "right" else "left"
    dur_s = "%.3f" % duration

    (project_dir / "meta.json").write_text(
        json.dumps({"name": "nwrch-studio-master", "id": "nwrch-studio-master"}, indent=2),
        encoding="utf-8",
    )
    (project_dir / "package.json").write_text(
        json.dumps({"private": True, "scripts": {"render": "hyperframes render --output ../final_master.mp4"}}, indent=2),
        encoding="utf-8",
    )

    body = []
    tl = []
    windows = []

    if avatar_mode == "base":
        # Calcula janelas de b-roll (usadas pelo FFmpeg no modo text_overlay_only,
        # ou pelo HyperFrames no modo legado com video elements)
        windows = enforce_avatar_solo_guard(
            broll_windows(scenes, duration, base_dur), duration, base_dur
        )
        if not text_overlay_only:
            # Modo legado: video elements no browser (lento)
            body.append(
                '<video id="avatar-base" class="clip avatar-base" src="./assets/' + avatar_name + '"'
                + ' data-start="0" data-duration="' + dur_s + '" data-track-index="0"'
                + ' data-media-start="0" data-volume="0" data-has-audio="false" muted playsinline preload="auto"></video>'
            )
            for k, w in enumerate(windows, 1):
                wid = "broll-" + str(k)
                w_start = "%.3f" % w["start"]
                w_dur = "%.3f" % w["duration"]
                fade = min(BROLL_FADE_SECONDS, w["duration"] / 4)
                body.append(
                    '<video id="' + wid + '" class="clip broll-clip" src="./assets/' + BASE_VIDEO_NAME + '"'
                    + ' data-start="' + w_start + '" data-duration="' + w_dur + '" data-track-index="1"'
                    + ' data-media-start="' + w_start + '" data-volume="0" data-has-audio="false"'
                    + ' muted playsinline preload="auto"></video>'
                )
                tl.append(
                    'tl.fromTo("#' + wid + '", { opacity: 0 }, { opacity: 1, duration: ' + ("%.3f" % fade)
                    + ', ease: "power2.out", immediateRender: false }, ' + w_start + ");"
                )
                tl.append(
                    'tl.to("#' + wid + '", { opacity: 0, duration: ' + ("%.3f" % fade)
                    + ', ease: "power2.in" }, ' + ("%.3f" % max(w["start"] + w["duration"] - fade, w["start"])) + ");"
                )
                tl.extend(motion_tweens("#" + wid, w.get("scenes") or []))
    elif text_overlay_only:
        pass
    else:
        # camada 0: video base dentro do wrapper de motion
        body.append('<div id="motion-wrap">')
        body.append(
            '<video id="base-broll" class="clip base-video" src="./assets/' + BASE_VIDEO_NAME + '"'
            + ' data-start="0" data-duration="' + dur_s + '" data-track-index="0"'
            + ' data-media-start="0" data-volume="0" data-has-audio="false" muted playsinline preload="auto"></video>'
        )
        body.append("</div>")
        tl.extend(motion_tweens("#motion-wrap", scenes))

    # captions
    cap_idx = 0
    for s in scenes:
        if not s["caption"]:
            continue
        cap_idx += 1
        cid = "cap-" + str(cap_idx)
        cap_start = "%.3f" % s["caption_start"]
        cap_dur = "%.3f" % max(s["caption_duration"], 0.4)
        body.append(
            '<div id="' + cid + '" class="clip caption pos-' + caption_pos + '" data-start="' + cap_start
            + '" data-duration="' + cap_dur + '" data-track-index="2">'
            + html_escape_mod.escape(s["caption"]) + "</div>"
        )
        tl.append(
            'tl.fromTo("#' + cid + '", { opacity: 0, y: 16 }, { opacity: 1, y: 0, duration: 0.35,'
            + ' ease: "power2.out", immediateRender: false }, ' + cap_start + ");"
        )
        # saida animada (slide+fade) -> motion design leve, em vez de corte seco
        cap_end = s["caption_start"] + max(s["caption_duration"], 0.4)
        cap_out = "%.3f" % max(cap_end - 0.3, s["caption_start"] + 0.1)
        tl.append(
            'tl.to("#' + cid + '", { opacity: 0, y: -10, duration: 0.3,'
            + ' ease: "power2.in" }, ' + cap_out + ");"
        )

    # avatar de canto (modo legado, sem avatar como base)
    if avatar_name and avatar_mode == "corner" and not text_overlay_only:
        plan_avatar = (edit_plan or {}).get("avatar") or {}
        pos = "left" if str(plan_avatar.get("position") or "right") == "left" else "right"
        try:
            scale = float(plan_avatar.get("scale") or 0.30)
        except (TypeError, ValueError):
            scale = 0.30
        scale = min(max(scale, 0.10), 0.60)
        avatar_w = int(width * scale)
        body.append(
            '<video id="avatar" class="clip avatar-clip pos-' + pos + '" src="./assets/' + avatar_name + '"'
            + ' style="width:' + str(avatar_w) + 'px" data-start="0" data-duration="' + dur_s + '"'
            + ' data-track-index="3" data-media-start="0" data-volume="0" data-has-audio="false"'
            + ' muted playsinline preload="auto"></video>'
        )

    # fades de transicao entre cenas
    fade_idx = 0
    if avatar_mode != "base" or text_overlay_only:
        for s in scenes:
            if s["transition_out"] != "fade":
                continue
            boundary = s["start"] + s["duration"]
            if boundary >= duration - 0.05:
                continue
            half = 0.25
            f_start = max(boundary - half, 0.0)
            f_dur = min(2 * half, duration - f_start)
            fade_idx += 1
            fid = "fade-" + str(fade_idx)
            body.append(
                '<div id="' + fid + '" class="clip fadeov" data-start="' + ("%.3f" % f_start)
                + '" data-duration="' + ("%.3f" % f_dur) + '" data-track-index="4"></div>'
            )
            tl.append(
                'tl.fromTo("#' + fid + '", { opacity: 0 }, { opacity: 1, duration: ' + ("%.3f" % (f_dur / 2))
                + ', ease: "power2.in", immediateRender: false }, ' + ("%.3f" % f_start) + ");"
            )
            tl.append(
                'tl.to("#' + fid + '", { opacity: 0, duration: ' + ("%.3f" % (f_dur / 2))
                + ', ease: "power2.out" }, ' + ("%.3f" % (f_start + f_dur / 2)) + ");"
            )

    # narracao
    if narration_name:
        plan_audio = (edit_plan or {}).get("audio") or {}
        try:
            volume = float(plan_audio.get("volume") or 1.0)
        except (TypeError, ValueError):
            volume = 1.0
        volume = min(max(volume, 0.0), 1.0)
        body.append(
            '<audio id="narration" class="clip" src="./assets/' + narration_name + '" data-start="0"'
            + ' data-duration="' + dur_s + '" data-track-index="5" data-media-start="0"'
            + ' data-volume="' + ("%.2f" % volume) + '" data-has-audio="true" preload="auto"></audio>'
        )

    (project_dir / "variables.json").write_text(
        json.dumps(
            {
                "version": 2,
                "base_video": "assets/" + BASE_VIDEO_NAME,
                "duration": round(duration, 3),
                "base_duration": round(base_dur, 3),
                "output": MASTER_VIDEO_NAME,
                "avatar_mode": avatar_mode,
                "scenes": len(scenes),
                "broll_windows": [
                    {"start": round(w["start"], 3), "end": round(w["end"], 3)} for w in windows
                ],
                "captions": cap_idx,
                "fades": fade_idx,
                "audio": bool(narration_name),
                "avatar": bool(avatar_name),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    css = OVERLAY_ONLY_CSS if text_overlay_only else COMPOSITION_CSS
    head = [
        "<!doctype html>",
        "<html>",
        "<head>",
        '<meta charset="utf-8">',
        "<style>" + css + "</style>",
        "</head>",
        "<body>",
        '<div id="root" data-composition-id="nwrch-master" data-start="0" data-duration="' + dur_s
        + '" data-width="' + str(width) + '" data-height="' + str(height)
        + '" style="width:' + str(width) + "px;height:" + str(height) + 'px">',
    ]
    gsap_tag = _gsap_script_tag(assets_dir)
    tail = [
        gsap_tag,
        "<script>",
        "const tl = gsap.timeline({ paused: true });",
    ] + tl + [
        "window.__timelines = window.__timelines || {};",
        'window.__timelines["nwrch-master"] = tl;',
        "</script>",
        "</div>",
        "</body>",
        "</html>",
    ]
    (project_dir / "index.html").write_text(NL.join(head + body + tail), encoding="utf-8")
    print(
        "Composicao: modo avatar=" + avatar_mode + ("(overlay)" if text_overlay_only else "") + ", "
        + str(len(scenes)) + " cenas, " + str(len(windows)) + " janelas de b-roll, "
        + str(cap_idx) + " captions, " + str(fade_idx) + " fades"
    )
    if text_overlay_only:
        return {"duration": duration, "windows": windows, "base_dur": base_dur, "cap_idx": cap_idx, "fade_idx": fade_idx}
    return duration


def has_audio_stream(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and "audio" in (result.stdout or "")


def apply_master_postprocess(master_out, narration=None, avatar=None, edit_plan=None, avatar_mode="none"):
    # Garante narracao/avatar no MP4 final fora do browser renderer.
    avatar_in_composition = avatar_mode in ("base", "corner")
    requested_avatar = avatar_requested(edit_plan, avatar)
    result = {
        "audio": False,
        "avatar": False,
        "requested_audio": bool(narration) or (avatar_mode == "base" and bool(avatar)),
        "requested_avatar": requested_avatar,
        "avatar_mode": avatar_mode,
        "method": [],
    }
    current = Path(master_out)
    duration = ffprobe_duration(current)

    if avatar and avatar_in_composition:
        # avatar ja renderizado dentro da composicao HyperFrames
        result["avatar"] = True
        result["method"].append("hyperframes_composition")

    if avatar and not avatar_in_composition:
        width, _height = plan_resolution(edit_plan)
        plan_avatar = (edit_plan or {}).get("avatar") or {}
        pos = "left" if str(plan_avatar.get("position") or "right") == "left" else "right"
        try:
            scale = float(plan_avatar.get("scale") or 0.30)
        except (TypeError, ValueError):
            scale = 0.30
        scale = min(max(scale, 0.10), 0.60)
        avatar_w = max(int(width * scale), 120)
        x = "24" if pos == "left" else "W-w-24"
        y = "H-h"
        tmp_avatar = current.with_name(current.stem + "_avatar.mp4")
        filtergraph = (
            "[1:v]scale=" + str(avatar_w) + ":-2,format=rgba[avatar];"
            "[0:v][avatar]overlay=" + x + ":" + y + ":eof_action=pass:shortest=0[v]"
        )
        run_logged(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(current),
                "-stream_loop",
                "-1",
                "-i",
                str(avatar),
                "-filter_complex",
                filtergraph,
                "-map",
                "[v]",
                "-t",
                "%.3f" % duration,
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                str(tmp_avatar),
            ],
            timeout=1200,
        )
        shutil.move(str(tmp_avatar), str(current))
        result["avatar"] = True
        result["method"].append("ffmpeg_overlay")

    # Sem narracao, o audio original do avatar vira a trilha do master.
    audio_source = narration
    if not audio_source and avatar_mode == "base" and avatar and has_audio_stream(avatar):
        audio_source = avatar
        result["method"].append("avatar_audio")

    if audio_source:
        video_dur = ffprobe_duration(current)
        audio_dur = ffprobe_duration(audio_source)
        tmp_audio = current.with_name(current.stem + "_audio.mp4")
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(current),
            "-i",
            str(audio_source),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
        ]
        if audio_dur > video_dur + 0.2:
            # nunca corta o final do audio: congela o ultimo frame do video
            # ate o audio terminar (antes o -shortest cortava o master)
            extra = audio_dur - video_dur
            cmd += [
                "-vf",
                "tpad=stop_mode=clone:stop_duration=%.3f" % extra,
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
            ]
            result["method"].append("video_extended_to_audio")
        else:
            cmd += ["-c:v", "copy"]
        cmd += [
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(tmp_audio),
        ]
        run_logged(cmd, timeout=1800)
        shutil.move(str(tmp_audio), str(current))
        result["audio"] = True
        result["method"].append("ffmpeg_audio_mix")

    return result


def plan_avatar_mode(edit_plan, avatar):
    ensure_avatar_contract(edit_plan, avatar)
    if not avatar:
        return "none"
    plan_avatar = (edit_plan or {}).get("avatar") or {}
    return "corner" if str(plan_avatar.get("mode") or "base") == "corner" else "base"


def _make_hyperframes_env():
    env = os.environ.copy()
    env["CI"] = "1"
    env["HYPERFRAMES_NO_UPDATE_CHECK"] = "1"
    env["PUPPETEER_SKIP_CHROMIUM_DOWNLOAD"] = "true"
    env["PRODUCER_LOW_MEMORY_MODE"] = "1"
    env["PRODUCER_PUPPETEER_LAUNCH_TIMEOUT_MS"] = "180000"
    env["PRODUCER_PUPPETEER_PROTOCOL_TIMEOUT_MS"] = "900000"
    env["PRODUCER_PLAYER_READY_TIMEOUT_MS"] = "180000"
    return env


def render_hyperframes_master(base_video, edit_plan=None, narration=None, avatar=None):
    master_out = Path("/kaggle/working") / MASTER_VIDEO_NAME
    project_dir = Path("/kaggle/working/hyperframes_master")
    frames_dir = Path("/kaggle/working/hyperframes_frames")
    ensure_avatar_contract(edit_plan, avatar)
    avatar_mode = plan_avatar_mode(edit_plan, avatar)
    width, height = plan_resolution(edit_plan)

    if avatar_mode in ("base", "corner") and avatar:
        # Pipeline rapido: FFmpeg compoe video, HyperFrames renderiza so texto
        stage_start = time.monotonic()
        result = write_hyperframes_project(
            base_video, project_dir, edit_plan, None, avatar,
            avatar_mode=avatar_mode, text_overlay_only=True
        )
        duration = result["duration"]
        windows = result["windows"]
        has_overlays = result["cap_idx"] > 0 or result["fade_idx"] > 0
        mark_timing("write_hyperframes_overlay_project", stage_start, avatar_mode=avatar_mode)

        # Etapa 1: FFmpeg compoe avatar + b-rolls (rapido, sem browser)
        composed_base = Path("/kaggle/working/composed_base.mp4")
        compose_start = time.monotonic()
        if avatar_mode == "base":
            print("FFmpeg: compondo base (avatar + %d b-rolls)..." % len(windows))
            ffmpeg_compose_base_layers(base_video, avatar, windows, composed_base, duration, width, height)
        else:
            print("FFmpeg: compondo base (b-roll + avatar corner)...")
            ffmpeg_compose_corner_layers(base_video, avatar, composed_base, duration, width, height, edit_plan)
        mark_timing("ffmpeg_compose_base", compose_start, avatar_mode=avatar_mode, broll_windows=len(windows))
        print("FFmpeg base pronto: " + str(composed_base))

        render_mode = "ffmpeg-compose"
        png_count = 0
        # Legendas via FFmpeg drawtext (sem Chrome/HyperFrames). OFF por padrao
        # por ora (qualidade visual das legendas ainda nao aprovada); liga com
        # PRODUCER_HF_ENABLE_CAPTIONS=1. O codigo fica pronto, so nao roda.
        captions_enabled = env_enabled("PRODUCER_HF_ENABLE_CAPTIONS", False)

        def _copy_base_fallback(reason):
            import shutil as _sh
            copy_start = time.monotonic()
            _sh.copy2(str(composed_base), str(master_out))
            mark_timing(
                "copy_composed_base",
                copy_start,
                overlays_available=has_overlays,
                captions_enabled=captions_enabled,
                reason=reason,
            )

        cap_scenes = [s for s in plan_scenes_within(edit_plan, duration) if s.get("caption")]
        if cap_scenes and captions_enabled:
            # Envolto em try/except: qualquer falha do drawtext cai na base
            # composta, entao o master nunca fica sem video.
            try:
                cap_start = time.monotonic()
                ffmpeg_drawtext_captions(composed_base, edit_plan, duration, master_out)
                mark_timing("ffmpeg_drawtext_captions", cap_start, captions=len(cap_scenes))
                render_mode = "ffmpeg-compose+drawtext"
            except Exception as cap_exc:
                print("Legendas drawtext falharam; usando base composta:", cap_exc)
                if master_out.exists():
                    master_out.unlink()
                _copy_base_fallback("captions_failed")
                render_mode = "ffmpeg-compose (caption-fallback)"
        else:
            _copy_base_fallback("captions_disabled" if not captions_enabled else "no_captions")

        if not master_out.exists():
            raise RuntimeError("Pipeline nao gerou " + str(master_out))
        post_start = time.monotonic()
        postprocess = apply_master_postprocess(master_out, narration, avatar, edit_plan, avatar_mode=avatar_mode)
        assert_avatar_satisfied(postprocess)
        mark_timing("ffmpeg_postprocess", post_start, avatar_mode=avatar_mode)
    else:
        # Modo legado (sem avatar): HyperFrames renderiza tudo
        stage_start = time.monotonic()
        assert_node_runtime()
        duration = write_hyperframes_project(
            base_video, project_dir, edit_plan, None, avatar, avatar_mode=avatar_mode
        )
        mark_timing("write_hyperframes_full_project", stage_start, avatar_mode=avatar_mode)
        env = _make_hyperframes_env()
        install_chrome_libs()
        system_chrome = ensure_system_chrome()
        if system_chrome:
            env["HYPERFRAMES_BROWSER_PATH"] = system_chrome
            env["PUPPETEER_EXECUTABLE_PATH"] = system_chrome
        print("HyperFrames project: " + str(project_dir) + " (%.2fs)" % duration)
        try:
            run_logged(HYPERFRAMES_CMD + ["lint", "."], cwd=project_dir, timeout=600, env=env)
        except Exception as lint_exc:
            print("Aviso: lint:", lint_exc)
        preferred_mode = os.environ.get("PRODUCER_HF_RENDER_MODE", "png-sequence").strip().lower()
        render_mode = "png-sequence+ffmpeg"
        png_count = 0
        if preferred_mode == "mp4":
            render_mode = "mp4"
            try:
                mp4_start = time.monotonic()
                run_logged(
                    hyperframes_render_args(master_out),
                    cwd=project_dir,
                    timeout=hyperframes_timeout(duration, "mp4"),
                    env=env,
                )
                mark_timing("hyperframes_mp4", mp4_start)
            except Exception as first_exc:
                print("HyperFrames MP4 falhou; tentando png-sequence:", first_exc)
                render_mode = "png-sequence+ffmpeg"
        if render_mode == "png-sequence+ffmpeg":
            if frames_dir.exists():
                shutil.rmtree(frames_dir)
            png_start = time.monotonic()
            run_logged(
                hyperframes_render_args(frames_dir, "png-sequence"),
                cwd=project_dir, timeout=hyperframes_timeout(duration, "png-sequence"), env=env,
            )
            png_count = encode_png_sequence(frames_dir, master_out)
            mark_timing("hyperframes_png_sequence_encode", png_start, png_frames=png_count)
        if not master_out.exists():
            raise RuntimeError("HyperFrames terminou sem gerar " + str(master_out))
        post_start = time.monotonic()
        postprocess = apply_master_postprocess(master_out, narration, avatar, edit_plan, avatar_mode=avatar_mode)
        assert_avatar_satisfied(postprocess)
        mark_timing("ffmpeg_postprocess", post_start, avatar_mode=avatar_mode)

    write_status(
        {
            "status": "complete",
            "output": str(master_out),
            "duration": round(duration, 3),
            "render_mode": render_mode,
            "avatar_mode": avatar_mode,
            "png_frames": png_count,
            "scenes": len((edit_plan or {}).get("scenes") or []),
            "broll_policy": (edit_plan or {}).get("broll_policy") or {},
            "audio": postprocess["audio"],
            "avatar": postprocess["avatar"],
            "requested_audio": postprocess["requested_audio"],
            "requested_avatar": postprocess["requested_avatar"],
            "postprocess": postprocess,
            "video_frame_samples": VIDEO_FRAME_SAMPLES,
            "performance": RUN_TIMINGS,
            "hyperframes": {
                "package": HYPERFRAMES_PACKAGE,
                "fps": RENDER_FPS,
                "output_fps": OUTPUT_FPS,
                "workers": RENDER_WORKERS,
                "low_memory": True,
                "captions_enabled": env_enabled("PRODUCER_HF_ENABLE_CAPTIONS", False),
            },
        }
    )
    print(f"Master: {master_out} ({master_out.stat().st_size/1024/1024:.1f} MB)")


input_root = Path("/kaggle/input")
montadores = list(input_root.rglob("montador.py"))
if montadores:
    montador = montadores[0]
else:
    import base64 as _b64
    montador = Path("/kaggle/working/_montador.py")
    montador.write_bytes(_b64.b64decode(_MONTADOR_B64))
    print("montador.py embutido no runner (dataset nao disponivel ainda)")

zips = list(input_root.rglob("*.zip"))
if zips:
    source = zips[0]
else:
    guides = list(input_root.rglob("guia_visual.json"))
    if not guides:
        raise RuntimeError("Nem ZIP nem guia_visual.json encontrados em " + str(input_root))
    source = guides[0].parent

out = Path("/kaggle/working") / BASE_VIDEO_NAME
print(f"Fonte: {source}")

sample_finalist_video_frames(source)

run_logged([sys.executable, str(montador), str(source), "--out", str(out), "--preset", "fast", "--no-overlay"], timeout=1800)
print(f"Video: {out} ({out.stat().st_size/1024/1024:.1f} MB)")

edit_plan, narration_file, avatar_file = find_pack_extras(input_root)
try:
    render_hyperframes_master(out, edit_plan, narration_file, avatar_file)
except Exception as exc:
    print("HyperFrames falhou; tentando master fallback com FFmpeg:", exc)
    fallback_master = Path("/kaggle/working") / MASTER_VIDEO_NAME
    try:
        fallback_mode = plan_avatar_mode(edit_plan, avatar_file)
        if fallback_mode == "base":
            # mesmo sem HyperFrames o avatar segue como base do video
            fb_w, fb_h = plan_resolution(edit_plan)
            run_logged(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(avatar_file),
                    "-vf",
                    "scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,fps=30" % (fb_w, fb_h, fb_w, fb_h),
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    str(fallback_master),
                ],
                timeout=1800,
            )
            postprocess = apply_master_postprocess(
                fallback_master, narration_file, avatar_file, edit_plan, avatar_mode="base"
            )
            assert_avatar_satisfied(postprocess)
        else:
            shutil.copy2(out, fallback_master)
            postprocess = apply_master_postprocess(fallback_master, narration_file, avatar_file, edit_plan)
            assert_avatar_satisfied(postprocess)
        write_status(
            {
                "status": "fallback_complete",
                "error": str(exc),
                "base_output": str(out),
                "output": str(fallback_master),
                "avatar_mode": fallback_mode,
                "audio": postprocess["audio"],
                "avatar": postprocess["avatar"],
                "requested_audio": postprocess["requested_audio"],
                "requested_avatar": postprocess["requested_avatar"],
                "postprocess": postprocess,
                "video_frame_samples": VIDEO_FRAME_SAMPLES,
            }
        )
        print("Master fallback pronto:", fallback_master)
    except Exception as fallback_exc:
        write_status({"status": "error", "error": str(exc), "fallback_error": str(fallback_exc), "base_output": str(out), "video_frame_samples": VIDEO_FRAME_SAMPLES})
        print("HyperFrames falhou, e o fallback tambem falhou:", fallback_exc)
"""


def push_kernel(
    ds_slug: str,
    project_name: str,
    username: str,
    token: str,
    project_id: int | None = None,
) -> tuple[str, str]:
    """Retorna (k_slug, push_output) para debug."""
    slug = kernel_slug(project_name, project_id)

    montador_src = ROOT / "montador.py"
    montador_b64 = base64.b64encode(montador_src.read_bytes()).decode("ascii")
    runner_src = f'_MONTADOR_B64 = "{montador_b64}"\n' + _RUNNER

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "runner.py").write_text(runner_src, encoding="utf-8")

        metadata = {
            "id": f"{username}/{slug}",
            "title": slug,
            "code_file": "runner.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": True,
            "enable_gpu": True,
            "machine_shape": "NvidiaTeslaT4",
            "enable_internet": True,
            "dataset_sources": [f"{username}/{ds_slug}"],
            "competition_sources": [],
            "kernel_sources": [],
        }
        (tmp / "kernel-metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )

        r = _run(["kernels", "push", "--accelerator", "NvidiaTeslaT4", "-p", str(tmp)], username, token, timeout=60)
        push_out = (r.stdout or "") + (r.stderr or "")

    return _extract_kernel_slug(push_out, username, slug), push_out.strip()


# ------------------------------------------------------------------
# Status
# ------------------------------------------------------------------
def get_status(k_slug: str, username: str, token: str) -> dict:
    kernel = _kernel_ref(username, k_slug)
    page_url = f"https://www.kaggle.com/code/{kernel}"
    try:
        video_url = get_video_url(k_slug, username, token)
    except Exception:
        video_url = ""

    if video_url:
        return {"status": "complete", "url": page_url, "video_url": video_url, "error": ""}

    try:
        files, _detail = list_kernel_files(k_slug, username, token)
        if any(_is_video_output(name) for name in files):
            try:
                status_hint = kernel_status_hint(k_slug, username, token).lower()
                if "running" in status_hint or "queued" in status_hint:
                    return {
                        "status": "queued",
                        "url": page_url,
                        "video_url": "",
                        "error": "Kernel ainda em execucao; aguardando o Kaggle publicar o output atual.",
                    }
                if "error" in status_hint or "failed" in status_hint:
                    return {"status": "error", "url": page_url, "video_url": "", "error": status_hint[:400]}
            except RuntimeError:
                # O endpoint de status pode falhar no Kaggle; nesse caso preserva
                # o fallback por listagem de arquivos que ja salvou o fluxo antes.
                pass
            return {
                "status": "complete",
                "url": page_url,
                "video_url": "",
                "error": "Video pronto no Kaggle; preparando download local.",
            }
    except RuntimeError as exc:
        detail = str(exc)
        if _looks_like_missing_kernel_error(detail) or _looks_like_auth_error(detail):
            return {"status": "error", "url": page_url, "video_url": "", "error": detail[:400]}
        # Erro transitorio do Kaggle: nao transforme em falha final.

    return {
        "status": "queued",
        "url": page_url,
        "video_url": "",
        "error": "Render enviado; aguardando o Kaggle disponibilizar o video.",
    }


def get_video_url(k_slug: str, username: str, token: str) -> str:
    import requests as req
    from requests.auth import HTTPBasicAuth
    resp = req.get(
        "https://www.kaggle.com/api/v1/kernels/output",
        auth=HTTPBasicAuth(username, token),
        params={"userName": username, "kernelSlug": k_slug},
        timeout=30,
    )
    if not resp.ok:
        return ""
    candidates = []
    for f in resp.json().get("files", []):
        filename = f.get("fileName", "")
        url = f.get("url", "")
        if url and _is_video_output(filename):
            candidates.append((filename, url))
    if candidates:
        candidates.sort(key=lambda item: _video_output_priority(item[0]))
        return candidates[0][1]
    return ""


def pull_output_video(k_slug: str, username: str, token: str, out_dir: Path) -> Path | None:
    """Baixa videos, status e logs do output do Kaggle."""
    kernel = _kernel_ref(username, k_slug)
    out_dir.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "kernels",
            "output",
            kernel,
            "-p",
            str(out_dir),
            "-o",
            "--file-pattern",
            r"(final_master\.mp4|video_broll_base\.mp4|base_broll\.mp4|hyperframes_status\.json|log_render\.txt|guia_execucao_final\.json|metadata[/\\]video_frame_samples\.json|video_frame_samples[/\\].*\.jpg)$",
        ],
        username,
        token,
        timeout=600,
    )
    return choose_preferred_video_path(out_dir.rglob("*.mp4"))
