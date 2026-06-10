"""Integração com Kaggle via CLI (kaggle>=1.5).

Usa subprocess + kaggle CLI em vez da API Python para evitar
incompatibilidades entre versões do pacote.

O montador.py é enviado junto no dataset — sem embutir base64 no kernel.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import csv
import io
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
BASE_VIDEO_NAME = "video_broll_base.mp4"
BASE_VIDEO_ALIAS = "base_broll.mp4"
MASTER_VIDEO_NAME = "final_master.mp4"


def _slug(text: str, max_len: int = 36) -> str:
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (text[:max_len] or "brolls").strip("-")


def dataset_slug(project_name: str) -> str:
    return ("brolls-" + _slug(project_name))[:50]


def kernel_slug(project_name: str) -> str:
    return ("b-rolls-render-" + _slug(project_name))[:50]


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
    return sorted(videos, key=lambda p: (_video_output_priority(str(p)), -p.stat().st_mtime))[0]


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


def _run(args: list[str], username: str, token: str, **kwargs) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["KAGGLE_USERNAME"] = username
    env["KAGGLE_KEY"] = token
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    cmd = [sys.executable, "-m", "kaggle"] + args
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", **kwargs)
    if result.returncode != 0:
        out = (result.stderr or "") + (result.stdout or "")
        raise RuntimeError((out or "erro desconhecido")[-800:])
    return result


# ------------------------------------------------------------------
# Upload do ZIP como dataset (inclui montador.py junto)
# ------------------------------------------------------------------
def upload_dataset(zip_path: Path, project_name: str, username: str, token: str) -> str:
    slug = dataset_slug(project_name)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        shutil.copy2(zip_path, tmp / zip_path.name)

        montador_src = ROOT / "montador.py"
        if montador_src.exists():
            shutil.copy2(montador_src, tmp / "montador.py")

        metadata = {
            "title": f"B-rolls {project_name}"[:50],
            "id": f"{username}/{slug}",
            "licenses": [{"name": "CC0-1.0"}],
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

    return slug


# ------------------------------------------------------------------
# Kernel — runner.py leve, montador vem do dataset
# ------------------------------------------------------------------
_RUNNER = """\
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


BASE_VIDEO_NAME = "video_broll_base.mp4"
MASTER_VIDEO_NAME = "final_master.mp4"
HYPERFRAMES_CMD = ["npx", "-y", "--package", "node@22", "--package", "hyperframes", "hyperframes"]
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
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if result.stdout:
        print(result.stdout[-4000:])
    if result.stderr:
        print("STDERR:", result.stderr[-3000:])
    if result.returncode != 0:
        raise RuntimeError("Comando falhou (exit " + str(result.returncode) + "): " + " ".join(str(c) for c in cmd))
    return result


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
        "30",
        "--workers",
        "1",
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
                "30",
                "-pattern_type",
                "glob",
                "-i",
                str(direct_pattern),
                "-vf",
                "format=yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
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
                "30",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(manifest),
                "-vf",
                "format=yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                str(master_out),
            ],
            timeout=1200,
        )
    return len(pngs)


def assert_node_runtime():
    print("Node do sistema:", optional_command_output(["node", "--version"]))
    print("npm:", optional_command_output(["npm", "--version"]))
    print("npx:", command_output(["npx", "--version"]))
    node_version = command_output(["npx", "-y", "--package", "node@22", "node", "--version"])
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


def install_chrome_libs():
    if not Path("/usr/bin/apt-get").exists():
        print("apt-get indisponivel; nao da para instalar bibliotecas do Chrome.")
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


def write_hyperframes_project(base_video, project_dir):
    if project_dir.exists():
        shutil.rmtree(project_dir)
    assets_dir = project_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(base_video, assets_dir / BASE_VIDEO_NAME)

    duration = ffprobe_duration(base_video)
    (project_dir / "meta.json").write_text(
        json.dumps({"name": "nwrch-studio-master", "id": "nwrch-studio-master"}, indent=2),
        encoding="utf-8",
    )
    (project_dir / "package.json").write_text(
        json.dumps({"private": True, "scripts": {"render": "hyperframes render --output ../final_master.mp4"}}, indent=2),
        encoding="utf-8",
    )
    (project_dir / "variables.json").write_text(
        json.dumps(
            {
                "version": 1,
                "base_video": "assets/" + BASE_VIDEO_NAME,
                "duration": round(duration, 3),
                "output": MASTER_VIDEO_NAME,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (project_dir / "index.html").write_text(
        f'''<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    html, body {{
      margin: 0;
      width: 100%;
      height: 100%;
      background: #050708;
      overflow: hidden;
    }}
    #root {{
      position: relative;
      width: 1920px;
      height: 1080px;
      background: #050708;
      overflow: hidden;
    }}
    .base-video {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      object-fit: cover;
    }}
  </style>
</head>
<body>
  <div
    id="root"
    data-composition-id="nwrch-master"
    data-start="0"
    data-duration="{duration:.3f}"
    data-width="1920"
    data-height="1080">
    <video
      id="base-broll"
      class="clip base-video"
      src="./assets/{BASE_VIDEO_NAME}"
      data-start="0"
      data-duration="{duration:.3f}"
      data-track-index="0"
      data-media-start="0"
      data-volume="0"
      data-has-audio="false"
      muted
      playsinline
      preload="auto"></video>
    <script src="https://cdn.jsdelivr.net/npm/gsap@3/dist/gsap.min.js"></script>
    <script>
      const tl = gsap.timeline({{ paused: true }});
      window.__timelines = window.__timelines || {{}};
      window.__timelines["nwrch-master"] = tl;
    </script>
  </div>
</body>
</html>
''',
        encoding="utf-8",
    )
    return duration


def render_hyperframes_master(base_video):
    project_dir = Path("/kaggle/working/hyperframes_master")
    master_out = Path("/kaggle/working") / MASTER_VIDEO_NAME
    frames_dir = Path("/kaggle/working/hyperframes_frames")
    assert_node_runtime()
    duration = write_hyperframes_project(base_video, project_dir)
    env = os.environ.copy()
    env["CI"] = "1"
    env["HYPERFRAMES_NO_UPDATE_CHECK"] = "1"
    env["PUPPETEER_SKIP_CHROMIUM_DOWNLOAD"] = "true"
    install_chrome_libs()
    system_chrome = ensure_system_chrome()
    if system_chrome:
        env["HYPERFRAMES_BROWSER_PATH"] = system_chrome
        env["PUPPETEER_EXECUTABLE_PATH"] = system_chrome
    env["PRODUCER_PUPPETEER_LAUNCH_TIMEOUT_MS"] = "180000"
    env["PRODUCER_PUPPETEER_PROTOCOL_TIMEOUT_MS"] = "900000"
    env["PRODUCER_PLAYER_READY_TIMEOUT_MS"] = "180000"
    env["PRODUCER_LOW_MEMORY_MODE"] = "1"
    print(f"HyperFrames project: {project_dir} ({duration:.2f}s)")
    run_logged(HYPERFRAMES_CMD + ["lint", "."], cwd=project_dir, timeout=600, env=env)
    render_mode = "mp4"
    png_count = 0
    try:
        run_logged(hyperframes_render_args(master_out), cwd=project_dir, timeout=3600, env=env)
    except Exception as first_exc:
        print("HyperFrames MP4 falhou; tentando png-sequence + ffmpeg:", first_exc)
        if frames_dir.exists():
            shutil.rmtree(frames_dir)
        run_logged(
            hyperframes_render_args(frames_dir, "png-sequence"),
            cwd=project_dir,
            timeout=3600,
            env=env,
        )
        png_count = encode_png_sequence(frames_dir, master_out)
        render_mode = "png-sequence+ffmpeg"
    if not master_out.exists():
        raise RuntimeError("HyperFrames terminou sem gerar " + str(master_out))
    write_status(
        {
            "status": "complete",
            "output": str(master_out),
            "duration": round(duration, 3),
            "render_mode": render_mode,
            "png_frames": png_count,
        }
    )
    print(f"Master: {master_out} ({master_out.stat().st_size/1024/1024:.1f} MB)")


input_root = Path("/kaggle/input")
montadores = list(input_root.rglob("montador.py"))
if not montadores:
    raise RuntimeError("montador.py nao encontrado em " + str(input_root))
montador = montadores[0]

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

run_logged([sys.executable, str(montador), str(source), "--out", str(out), "--preset", "fast"], timeout=1800)
print(f"Video: {out} ({out.stat().st_size/1024/1024:.1f} MB)")

try:
    render_hyperframes_master(out)
except Exception as exc:
    write_status({"status": "error", "error": str(exc), "base_output": str(out)})
    print("HyperFrames falhou, mas o video base foi preservado:", exc)
"""


def push_kernel(ds_slug: str, project_name: str, username: str, token: str) -> tuple[str, str]:
    """Retorna (k_slug, push_output) para debug."""
    slug = kernel_slug(project_name)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "runner.py").write_text(_RUNNER, encoding="utf-8")

        metadata = {
            "id": f"{username}/{slug}",
            "title": f"B-rolls Render - {project_name}"[:50],
            "code_file": "runner.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": True,
            "enable_gpu": False,
            "enable_internet": True,
            "dataset_sources": [f"{username}/{ds_slug}"],
            "competition_sources": [],
            "kernel_sources": [],
        }
        (tmp / "kernel-metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )

        r = _run(["kernels", "push", "-p", str(tmp)], username, token, timeout=60)
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
    """Baixa o MP4 do output do Kaggle para o servidor quando nao ha URL direta."""
    kernel = _kernel_ref(username, k_slug)
    out_dir.mkdir(parents=True, exist_ok=True)
    _run(
        ["kernels", "output", kernel, "-p", str(out_dir), "-o", "--file-pattern", r".*\.mp4$"],
        username,
        token,
        timeout=600,
    )
    return choose_preferred_video_path(out_dir.rglob("*.mp4"))
