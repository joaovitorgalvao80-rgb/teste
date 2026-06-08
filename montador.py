"""Sistema 2 — Montador Automatico de B-roll.

Recebe o asset_pack.zip gerado pelo Sistema 1, le o guia_visual.json e monta
uma timeline 100% B-roll sincronizada, exportando video_broll_base.mp4.

Nao busca nada, nao usa Groq/Pexels/Pixabay. So edita.

Uso:
    python montador.py caminho/asset_pack.zip
    python montador.py caminho/asset_pack.zip --out saida.mp4
    python montador.py pasta_ja_descompactada/        (aceita ZIP ou pasta)
    python montador.py pack.zip --audio narracao.mp3  (audio guia opcional)

Saidas (na pasta de saida):
    video_broll_base.mp4
    guia_execucao_final.json
    log_render.txt

Sincronizacao (conforme PLANO_SISTEMA_COMPLETO.md):
    - cada cena comeca no start_time e termina no end_time do guia;
    - asset curto  -> loop (-stream_loop) para preencher a duracao;
    - asset longo  -> corta trecho central (ponto inicial controlado);
    - sem audio do asset (-an); audio guia opcional mixado no final.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional


# ----------------------------------------------------------------------------
# Logger simples (console + arquivo)
# ----------------------------------------------------------------------------
class Logger:
    def __init__(self, path: Path):
        self.lines: list[str] = []
        self.path = path

    def __call__(self, msg: str) -> None:
        stamped = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(stamped)
        self.lines.append(stamped)

    def flush(self) -> None:
        self.path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------
# FFmpeg helpers
# ----------------------------------------------------------------------------
def run(cmd: list[str], timeout: Optional[int] = None, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(cwd) if cwd else None)


def require_tool(name: str) -> None:
    try:
        result = run([name, "-version"], timeout=15)
    except FileNotFoundError as exc:
        raise RuntimeError(f"'{name}' nao encontrado no PATH. Instale o FFmpeg.") from exc
    if result.returncode != 0:
        raise RuntimeError(f"'{name}' falhou ao iniciar.")


def ffprobe_duration(path: Path) -> float:
    result = run([
        "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
        "-of", "json", str(path),
    ], timeout=30)
    if result.returncode != 0:
        return 0.0
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return 0.0


def ffprobe_is_video(path: Path) -> bool:
    result = run([
        "ffprobe", "-v", "quiet", "-select_streams", "v:0",
        "-show_entries", "stream=codec_type", "-of", "json", str(path),
    ], timeout=30)
    if result.returncode != 0:
        return False
    try:
        streams = json.loads(result.stdout).get("streams", [])
        return bool(streams)
    except json.JSONDecodeError:
        return False


def font_path() -> Optional[str]:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/Arial.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def drawtext_escape(text: str) -> str:
    return (
        str(text or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace(":", "\\:")
        .replace("'", "’")
        .replace("\n", " ")
        .replace(",", "\\,")
    )


def prepare_font(workdir: Path) -> Optional[str]:
    """Copia a fonte escolhida para o workdir e devolve o nome relativo.

    Referenciar a fonte por um nome relativo (sem 'C:') evita o bug do parser
    do filtergraph do FFmpeg no Windows, onde o ':' da unidade quebra a sintaxe.
    O FFmpeg dessas cenas roda com cwd=workdir.
    """
    src = font_path()
    if not src:
        return None
    dest = workdir / "render_font.ttf"
    try:
        shutil.copy2(src, dest)
        return dest.name
    except OSError:
        return None


def drawtext_filter(scene: dict, duration: float, font_file: Optional[str]) -> Optional[str]:
    """Monta o filtro drawtext do texto overlay, ou None se nao houver."""
    overlay = scene.get("overlay_text")
    if not overlay or not font_file:
        return None
    pos = scene.get("overlay_position", "left")
    if pos == "left":
        x = "w*0.06"
    elif pos == "right":
        x = "(w-text_w-w*0.06)"
    else:
        x = "(w-text_w)/2"
    ov_start = float(scene.get("overlay_start", 0.3))
    ov_end = float(scene.get("overlay_end", min(max(duration - 0.2, ov_start + 0.1), 2.8)))
    return (
        "drawtext="
        f"fontfile={font_file}"
        f":text='{drawtext_escape(overlay)}'"
        ":fontcolor=white:fontsize=h*0.058:borderw=4:bordercolor=black@0.82"
        f":x={x}:y=h*0.76"
        f":enable='between(t,{ov_start},{ov_end})'"
    )


# ----------------------------------------------------------------------------
# Entrada: aceita ZIP ou pasta ja descompactada
# ----------------------------------------------------------------------------
def prepare_input(source: Path, workdir: Path, log: Logger) -> Path:
    """Devolve a pasta que contem guia_visual.json + assets/."""
    if source.is_dir():
        log(f"Entrada e pasta: {source}")
        return source
    if source.suffix.lower() == ".zip":
        extract_dir = workdir / "unpacked"
        extract_dir.mkdir(parents=True, exist_ok=True)
        log(f"Descompactando {source.name} -> {extract_dir}")
        with zipfile.ZipFile(source, "r") as zf:
            zf.extractall(extract_dir)
        # alguns zips tem uma subpasta unica; normaliza
        if not (extract_dir / "guia_visual.json").exists():
            for sub in extract_dir.iterdir():
                if sub.is_dir() and (sub / "guia_visual.json").exists():
                    return sub
        return extract_dir
    raise RuntimeError(f"Entrada invalida: {source} (esperado .zip ou pasta)")


def load_guide(pack_dir: Path) -> dict:
    guide_path = pack_dir / "guia_visual.json"
    if not guide_path.exists():
        raise RuntimeError(f"guia_visual.json nao encontrado em {pack_dir}")
    return json.loads(guide_path.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------
# Render de uma cena (clipe normalizado para a timeline)
# ----------------------------------------------------------------------------
def build_clip(
    scene: dict,
    pack_dir: Path,
    clips_dir: Path,
    width: int,
    height: int,
    fps: int,
    overlay_enabled: bool,
    log: Logger,
    crf: int,
    preset: str,
    font_file: Optional[str],
    render_cwd: Path,
) -> Optional[Path]:
    selected = scene.get("selected_asset")
    if not selected:
        log(f"  [skip] {scene['id']} sem selected_asset")
        return None
    src = (pack_dir / selected).resolve()
    if not src.exists():
        log(f"  [skip] {scene['id']} arquivo nao encontrado: {selected}")
        return None

    duration = float(scene.get("duration") or 0)
    if duration <= 0:
        duration = max(0.1, float(scene.get("end_time", 0)) - float(scene.get("start_time", 0)))

    out = (clips_dir / f"{scene['idx']:03d}_{scene['id']}.mp4").resolve()
    is_video = src.suffix.lower() in {".mp4", ".mov", ".webm"} and ffprobe_is_video(src)

    # cadeia de filtros: cobre 16:9 com crop central + fps + sar
    vf = [
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
        f"fps={fps}",
        "setsar=1",
    ]

    # texto overlay opcional, do lado oposto ao avatar
    overlay_filter = drawtext_filter(scene, duration, font_file) if overlay_enabled else None
    if overlay_filter:
        vf.append(overlay_filter)

    common_out = [
        "-t", f"{duration:.3f}",
        "-vf", ",".join(vf),
        "-an",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        str(out),
    ]

    if is_video:
        # asset longo: comeca em offset controlado (10% do excedente) para evitar abertura morta
        src_dur = ffprobe_duration(src)
        seek = []
        if src_dur > duration + 0.3:
            offset = min((src_dur - duration) * 0.5, max(0.0, src_dur - duration))
            seek = ["-ss", f"{offset:.3f}"]
        # asset curto: loop infinito + corte por -t preenche a cena
        cmd = ["ffmpeg", "-y", "-stream_loop", "-1", *seek, "-i", str(src), *common_out]
    else:
        # imagem (fallback): Ken Burns leve (zoom lento) durante a cena
        zoom = (
            f"scale={width*2}:{height*2}:force_original_aspect_ratio=increase,"
            f"crop={width*2}:{height*2},"
            f"zoompan=z='min(zoom+0.0006,1.12)':d={int(duration*fps)}:s={width}x{height}:fps={fps},"
            "setsar=1"
        )
        vf_img = zoom
        if overlay_filter:
            # reanexa o drawtext no fim da cadeia da imagem
            vf_img = zoom + "," + overlay_filter
        cmd = [
            "ffmpeg", "-y", "-loop", "1", "-i", str(src),
            "-t", f"{duration:.3f}", "-vf", vf_img, "-an",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", str(out),
        ]

    log(f"  [clip] {scene['id']} <- {selected} | {duration:.2f}s | {'video' if is_video else 'imagem'}")
    result = run(cmd, timeout=1200, cwd=render_cwd)
    if result.returncode != 0:
        log(f"  [ERRO ffmpeg] {scene['id']}:\n{result.stderr[-800:]}")
        return None
    return out


def concat_and_finalize(
    clips: list[Path], out_video: Path, workdir: Path, audio: Optional[Path], log: Logger
) -> None:
    concat_file = workdir / "concat.txt"
    concat_file.write_text(
        "\n".join(f"file '{c.resolve().as_posix()}'" for c in clips), encoding="utf-8"
    )
    silent = workdir / "timeline_sem_audio.mp4"

    log("Concatenando clipes...")
    result = run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_file), "-c", "copy", str(silent),
    ], timeout=1200)
    if result.returncode != 0:
        raise RuntimeError(f"Concat falhou:\n{result.stderr[-1000:]}")

    out_video.parent.mkdir(parents=True, exist_ok=True)
    if audio and audio.exists():
        log(f"Mixando audio guia: {audio.name}")
        result = run([
            "ffmpeg", "-y", "-i", str(silent), "-i", str(audio),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-shortest",
            "-movflags", "+faststart", str(out_video),
        ], timeout=1200)
        if result.returncode != 0:
            raise RuntimeError(f"Mix de audio falhou:\n{result.stderr[-1000:]}")
    else:
        shutil.copy2(silent, out_video)


# ----------------------------------------------------------------------------
# Pipeline principal
# ----------------------------------------------------------------------------
def montar(
    source: Path,
    out_video: Path,
    audio: Optional[Path],
    fps: int,
    crf: int,
    preset: str,
    keep_workdir: bool,
) -> None:
    out_dir = out_video.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    log = Logger(out_dir / "log_render.txt")

    require_tool("ffmpeg")
    require_tool("ffprobe")

    workdir = Path(tempfile.mkdtemp(prefix="montador_"))
    try:
        pack_dir = prepare_input(source, workdir, log)
        guide = load_guide(pack_dir)

        res = (guide.get("resolution") or "1920x1080").lower().replace(" ", "")
        try:
            width, height = (int(x) for x in res.split("x"))
        except ValueError:
            width, height = 1920, 1080

        scenes = guide.get("scenes", [])
        # garante idx e ordena por start_time
        for i, s in enumerate(scenes, 1):
            s.setdefault("idx", i)
        scenes.sort(key=lambda s: (s.get("start_time", 0), s["idx"]))

        log(f"Projeto: {guide.get('project_name','(sem nome)')}")
        log(f"Resolucao: {width}x{height} | fps: {fps} | cenas: {len(scenes)}")
        log(f"Avatar safe area: {guide.get('avatar_safe_area','right')}")

        clips_dir = workdir / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)

        font_file = prepare_font(workdir)
        if not font_file:
            log("Aviso: nenhuma fonte encontrada; textos overlay serao ignorados.")

        clips: list[Path] = []
        execution = {
            "project_name": guide.get("project_name", ""),
            "resolution": f"{width}x{height}",
            "fps": fps,
            "scenes": [],
        }
        for scene in scenes:
            clip = build_clip(
                scene, pack_dir, clips_dir, width, height, fps,
                overlay_enabled=True, log=log, crf=crf, preset=preset,
                font_file=font_file, render_cwd=workdir,
            )
            if clip is None:
                continue
            clips.append(clip)
            execution["scenes"].append({
                "id": scene["id"],
                "start_time": scene.get("start_time"),
                "end_time": scene.get("end_time"),
                "duration": scene.get("duration"),
                "selected_asset": scene.get("selected_asset"),
                "overlay_text": scene.get("overlay_text", ""),
                "clip": clip.name,
            })

        if not clips:
            raise RuntimeError(
                "Nenhuma cena renderizada. Verifique se o guia tem selected_asset "
                "e se os arquivos existem em assets/."
            )

        concat_and_finalize(clips, out_video, workdir, audio, log)

        final_dur = ffprobe_duration(out_video)
        execution["output"] = str(out_video)
        execution["final_duration"] = round(final_dur, 3)
        (out_dir / "guia_execucao_final.json").write_text(
            json.dumps(execution, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        log(f"OK -> {out_video} ({final_dur:.1f}s, {len(clips)} cenas)")
        log(f"Guia de execucao: {out_dir / 'guia_execucao_final.json'}")
    finally:
        log.flush()
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            log(f"workdir mantido: {workdir}")
            log.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="Monta video 100% B-roll a partir do asset_pack do Sistema 1.")
    parser.add_argument("source", help="asset_pack.zip ou pasta ja descompactada")
    parser.add_argument("--out", default="output/video_broll_base.mp4", help="caminho do MP4 de saida")
    parser.add_argument("--audio", default=None, help="audio guia opcional (mp3/wav) para mixar")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--crf", type=int, default=20)
    parser.add_argument("--preset", default="medium")
    parser.add_argument("--keep-workdir", action="store_true", help="nao apagar pasta temporaria")
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    if not source.exists():
        print(f"[ERRO] entrada nao existe: {source}", file=sys.stderr)
        sys.exit(1)
    out_video = Path(args.out).expanduser().resolve()
    audio = Path(args.audio).expanduser().resolve() if args.audio else None

    try:
        montar(source, out_video, audio, args.fps, args.crf, args.preset, args.keep_workdir)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[ERRO] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
