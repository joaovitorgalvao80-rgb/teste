"""Smoke test: valida o pipeline HyperFrames dentro de um kernel real do Kaggle.

Reaproveita o _RUNNER de producao (services/kaggle_service.py), trocando apenas
a etapa do montador por um video de teste de 5s gerado com FFmpeg. Assim o
caminho de codigo validado e exatamente o que roda no fluxo real:

  assert_node_runtime -> install_chrome_libs -> write_hyperframes_project
  -> hyperframes lint -> hyperframes render (mp4; fallback png-sequence)

Credenciais via variaveis de ambiente KAGGLE_USERNAME / KAGGLE_KEY.

Uso:
  python tools/smoke_hyperframes_kaggle.py            # push + monitora + baixa
  python tools/smoke_hyperframes_kaggle.py --watch SLUG   # so monitora um kernel ja enviado
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import kaggle_service  # noqa: E402

SMOKE_SLUG = "nwrch-smoke-hyperframes"
SPLIT_MARKER = 'input_root = Path("/kaggle/input")'
POLL_SECONDS = 45
MAX_WAIT_SECONDS = 45 * 60

SMOKE_TAIL = '''\
print("=" * 60)
print("NWRCH SMOKE TEST 2 - HyperFrames com edit plan no Kaggle")
print("=" * 60)
print("Python:", sys.version.split()[0])
print("ffmpeg:", optional_command_output(["ffmpeg", "-version"]).splitlines()[0])

out = Path("/kaggle/working") / BASE_VIDEO_NAME
run_logged(
    [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=12",
        "-vf", "format=yuv420p",
        "-c:v", "libx264", "-preset", "fast",
        str(out),
    ],
    timeout=300,
)
print(f"Video base de teste: {out} ({out.stat().st_size/1024:.0f} KB)")

narration = Path("/kaggle/working/narration.wav")
run_logged(
    [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
        "-c:a", "pcm_s16le",
        str(narration),
    ],
    timeout=120,
)

smoke_plan = {
    "version": 1,
    "resolution": "1280x720",
    "caption_position": "left",
    "audio": {"src": "narration.wav", "volume": 1.0},
    "scenes": [
        {"start": 0, "duration": 4, "motion": "slow_push_in", "transition_out": "fade", "caption": "Cena um"},
        {"start": 4, "duration": 4, "motion": "slow_pull_out", "transition_out": "fade", "caption": "Cena dois"},
        {"start": 8, "duration": 4, "motion": "slow_push_in", "transition_out": "none", "caption": ""},
    ],
}

try:
    render_hyperframes_master(out, smoke_plan, narration, None)
    master = Path("/kaggle/working") / MASTER_VIDEO_NAME
    probe = run_logged(
        [
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=codec_type", "-of", "csv=p=0",
            str(master),
        ],
        timeout=120,
    )
    if "audio" not in (probe.stdout or ""):
        raise RuntimeError("master final sem trilha de audio")
    print("Master tem trilha de audio: OK")
    print("SMOKE_RESULT: SUCCESS")
except Exception as exc:
    write_status({"status": "error", "error": str(exc), "base_output": str(out)})
    print("SMOKE_RESULT: FAILED -", exc)
    raise
'''


def build_smoke_runner() -> str:
    runner = kaggle_service._RUNNER
    if SPLIT_MARKER not in runner:
        raise RuntimeError(
            "Marcador nao encontrado no _RUNNER; o runner de producao mudou. "
            "Atualize SPLIT_MARKER em tools/smoke_hyperframes_kaggle.py."
        )
    head = runner.split(SPLIT_MARKER, 1)[0]
    return head + SMOKE_TAIL


def push_smoke_kernel(username: str, token: str) -> str:
    source = build_smoke_runner()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "runner.py").write_text(source, encoding="utf-8")
        metadata = {
            "id": f"{username}/{SMOKE_SLUG}",
            "title": "NWRCH Smoke - HyperFrames",
            "code_file": "runner.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": True,
            "enable_gpu": False,
            "enable_internet": True,
            "dataset_sources": [],
            "competition_sources": [],
            "kernel_sources": [],
        }
        (tmp / "kernel-metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )
        r = kaggle_service._run(["kernels", "push", "-p", str(tmp)], username, token, timeout=120)
        push_out = ((r.stdout or "") + (r.stderr or "")).strip()
    print("PUSH:", push_out)
    return kaggle_service._extract_kernel_slug(push_out, username, SMOKE_SLUG)


def watch(slug: str, username: str, token: str) -> bool:
    """Retorna True se o smoke passou (final_master.mp4 publicado)."""
    deadline = time.time() + MAX_WAIT_SECONDS
    page = f"https://www.kaggle.com/code/{username}/{slug}"
    print(f"Monitorando {page}")
    while time.time() < deadline:
        try:
            hint = kaggle_service.kernel_status_hint(slug, username, token)
        except Exception as exc:
            hint = f"(status indisponivel: {exc})"
        try:
            files, _ = kaggle_service.list_kernel_files(slug, username, token)
        except Exception:
            files = []
        elapsed = int(MAX_WAIT_SECONDS - (deadline - time.time()))
        print(f"[{elapsed//60:02d}:{elapsed%60:02d}] status={hint!r} files={files}")
        low = hint.lower()
        done = "complete" in low or "error" in low or "failed" in low or "cancel" in low
        if done:
            has_master = any(
                f.lower().rsplit("/", 1)[-1] == kaggle_service.MASTER_VIDEO_NAME for f in files
            )
            return has_master and "complete" in low
        time.sleep(POLL_SECONDS)
    print("Tempo maximo de espera atingido.")
    return False


def pull_artifacts(slug: str, username: str, token: str) -> Path:
    out_dir = ROOT / "workdir" / "smoke_hyperframes"
    out_dir.mkdir(parents=True, exist_ok=True)
    kernel = kaggle_service._kernel_ref(username, slug)
    kaggle_service._run(
        ["kernels", "output", kernel, "-p", str(out_dir)], username, token, timeout=600
    )
    print("Artefatos baixados em:", out_dir)
    for item in sorted(out_dir.rglob("*")):
        if item.is_file():
            print(f"  {item.relative_to(out_dir)} ({item.stat().st_size} bytes)")
    status_file = out_dir / "hyperframes_status.json"
    if status_file.exists():
        print("hyperframes_status.json:")
        print(status_file.read_text(encoding="utf-8"))
    return out_dir


def main() -> int:
    username = os.environ.get("KAGGLE_USERNAME", "").strip()
    token = os.environ.get("KAGGLE_KEY", "").strip()
    if not username or not token:
        print("Defina KAGGLE_USERNAME e KAGGLE_KEY no ambiente.", file=sys.stderr)
        return 2

    if len(sys.argv) > 2 and sys.argv[1] == "--watch":
        slug = sys.argv[2]
    else:
        slug = push_smoke_kernel(username, token)
        print(f"Kernel enviado: {username}/{slug}")
        time.sleep(20)

    passed = watch(slug, username, token)
    pull_artifacts(slug, username, token)
    print("VEREDITO:", "SMOKE PASSOU" if passed else "SMOKE FALHOU")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
