"""Sistema 2 — Montador Automatico de B-roll.

Recebe o asset_pack.zip gerado pelo Sistema 1, le o guia_visual.json e monta
uma timeline 100% B-roll sincronizada, exportando video_broll_base.mp4.

Nao busca nada, nao usa Groq/Pexels/Pixabay. So edita.

Uso:
    python montador.py caminho/asset_pack.zip
    python montador.py caminho/asset_pack.zip --out saida.mp4
    python montador.py pasta_ja_descompactada/        (aceita ZIP ou pasta)
    python montador.py pack.zip --audio narracao.mp3  (audio guia opcional)
    python montador.py pack.zip --no-overlay          (base limpa para HyperFrames)

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

GUIA_VISUAL_NAME = "guia_visual.json"
ALLOWED_TOOLS = {"ffmpeg", "ffprobe"}


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
def _validated_cmd(cmd: list[str]) -> list[str]:
    if not cmd or not all(isinstance(part, str) and "\x00" not in part for part in cmd):
        raise RuntimeError("Comando invalido para render.")
    tool = Path(cmd[0]).name.lower()
    if tool not in ALLOWED_TOOLS:
        raise RuntimeError(f"Ferramenta nao permitida: {tool}")
    return list(cmd)


def run(cmd: list[str], timeout: Optional[int] = None, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    safe_cmd = _validated_cmd(cmd)
    safe_cwd = str(cwd.resolve()) if cwd else None
    return subprocess.run(safe_cmd, capture_output=True, text=True, timeout=timeout, cwd=safe_cwd)


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
    except (KeyError, ValueError):  # json.JSONDecodeError deriva de ValueError
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
        .replace("'", r"\'")
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
def safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    dest_root = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        try:
            target.relative_to(dest_root)
        except ValueError as exc:
            raise RuntimeError(f"ZIP contem caminho inseguro: {member.filename}") from exc
    zf.extractall(dest)


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
            safe_extract_zip(zf, extract_dir)
        # alguns zips tem uma subpasta unica; normaliza
        if not (extract_dir / GUIA_VISUAL_NAME).exists():
            for sub in extract_dir.iterdir():
                if sub.is_dir() and (sub / GUIA_VISUAL_NAME).exists():
                    return sub
        return extract_dir
    raise RuntimeError(f"Entrada invalida: {source} (esperado .zip ou pasta)")


def load_guide(pack_dir: Path) -> dict:
    guide_path = pack_dir / GUIA_VISUAL_NAME
    if not guide_path.exists():
        raise RuntimeError(f"guia_visual.json nao encontrado em {pack_dir}")
    return json.loads(guide_path.read_text(encoding="utf-8"))


def resolve_selected_asset(pack_dir: Path, selected: str) -> Path:
    selected = str(selected or "").replace("\\", "/").strip()
    if not selected:
        raise RuntimeError("Cena sem selected_asset.")
    if selected.startswith("/") or selected.startswith("../") or "/../" in selected:
        raise RuntimeError(f"selected_asset inseguro fora de assets/: {selected}")
    if not selected.startswith("assets/"):
        raise RuntimeError(f"selected_asset deve ficar dentro de assets/: {selected}")
    assets_root = (pack_dir / "assets").resolve()
    src = (pack_dir / selected).resolve()
    try:
        src.relative_to(assets_root)
    except ValueError as exc:
        raise RuntimeError(f"selected_asset fora de assets/: {selected}") from exc
    return src


def realign_scene_durations(scenes: list[dict]) -> None:
    """Alinha a base ao tempo REAL: cada clipe preenche ate o inicio da proxima
    cena (cobrindo as micro-pausas da narracao). In-place; assume scenes ja
    ordenadas por start_time.

    Sem isso a base fica 'gapless' (soma das duracoes) enquanto o edit_plan usa
    start_time real (com pausas). O descasamento acumula e, no fim de uma janela
    de b-roll, a base ja esta mostrando o clipe da PROXIMA cena (ex.: a do avatar)
    por ~1s antes do avatar aparecer — o 'flash' de b-roll errado. Estendendo
    cada clipe ate o proximo start, base e edit_plan ficam na mesma timeline.
    """
    for i, s in enumerate(scenes):
        own = float(s.get("duration") or 0)
        if own <= 0:
            own = max(0.1, float(s.get("end_time", 0)) - float(s.get("start_time", 0)))
        if i + 1 < len(scenes):
            span = float(scenes[i + 1].get("start_time", 0)) - float(s.get("start_time", 0))
            s["duration"] = round(max(own, span), 3)
        else:
            s["duration"] = round(own, 3)


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
    duration = float(scene.get("duration") or 0)
    if duration <= 0:
        duration = max(0.1, float(scene.get("end_time", 0)) - float(scene.get("start_time", 0)))
    out = (clips_dir / f"{scene['idx']:03d}_{scene['id']}.mp4").resolve()

    selected = scene.get("selected_asset")
    if not selected:
        # Cena avatar-only (sem b-roll): clipe PRETO so para preservar a timeline
        # da base. Na composicao o avatar cobre esse trecho, entao o preto nunca
        # aparece. Sem isso, cenas sem asset (nao buscadas de proposito) quebrariam
        # o montador.
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"color=c=black:s={width}x{height}:r={fps}",
            "-t", f"{duration:.3f}", "-an",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", str(out),
        ]
        log(f"  [clip] {scene['id']} <- (avatar-only, base preta) | {duration:.2f}s")
        result = run(cmd, timeout=600, cwd=render_cwd)
        if result.returncode != 0:
            log(f"  [ERRO ffmpeg] {scene['id']} (placeholder):\n{result.stderr[-800:]}")
            raise RuntimeError(f"FFmpeg falhou em {scene['id']} (placeholder preto)")
        return out

    src = resolve_selected_asset(pack_dir, selected)
    if not src.exists():
        raise RuntimeError(f"{scene['id']} arquivo nao encontrado: {selected}")
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
        # asset longo: usa o trecho central (offset = metade do excedente)
        src_dur = ffprobe_duration(src)
        seek = []
        if src_dur > duration + 0.3:
            offset = (src_dur - duration) * 0.5
            seek = ["-ss", f"{offset:.3f}"]
        # asset curto: loop infinito + corte por -t preenche a cena
        cmd = ["ffmpeg", "-y", "-stream_loop", "-1", *seek, "-i", str(src), *common_out]
    else:
        # imagem (fallback): frame estatico; o motion (zoom/pan) e aplicado
        # depois pelo HyperFrames. Zoom aqui causava movimento duplicado e
        # o "pulo" visual no inicio da cena.
        cmd = [
            "ffmpeg", "-y", "-loop", "1", "-i", str(src),
            "-t", f"{duration:.3f}", "-vf", ",".join(vf), "-an",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", str(out),
        ]

    log(f"  [clip] {scene['id']} <- {selected} | {duration:.2f}s | {'video' if is_video else 'imagem'}")
    result = run(cmd, timeout=1200, cwd=render_cwd)
    if result.returncode != 0:
        log(f"  [ERRO ffmpeg] {scene['id']}:\n{result.stderr[-800:]}")
        raise RuntimeError(f"FFmpeg falhou em {scene['id']}")
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
        video_dur = ffprobe_duration(silent)
        audio_dur = ffprobe_duration(audio)
        cmd = [
            "ffmpeg", "-y", "-i", str(silent), "-i", str(audio),
            "-map", "0:v:0", "-map", "1:a:0",
        ]
        if audio_dur > video_dur + 0.2:
            # o audio manda na duracao: congela o ultimo frame em vez de
            # cortar o final (o -shortest deixava o video incompleto)
            extra = audio_dur - video_dur
            log(f"Audio {audio_dur:.1f}s > video {video_dur:.1f}s; estendendo ultimo frame em {extra:.1f}s")
            cmd += [
                "-vf", f"tpad=stop_mode=clone:stop_duration={extra:.3f}",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            ]
        else:
            cmd += ["-c:v", "copy"]
        cmd += ["-c:a", "aac", "-movflags", "+faststart", str(out_video)]
        result = run(cmd, timeout=1800)
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
    overlay_enabled: bool = True,
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
        # base no MESMO tempo do edit_plan (cobre pausas da narracao)
        realign_scene_durations(scenes)

        log(f"Projeto: {guide.get('project_name','(sem nome)')}")
        log(f"Resolucao: {width}x{height} | fps: {fps} | cenas: {len(scenes)}")
        log(f"Avatar safe area: {guide.get('avatar_safe_area','right')}")
        log(f"Overlay de texto no video base: {'sim' if overlay_enabled else 'nao'}")

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
                overlay_enabled=overlay_enabled, log=log, crf=crf, preset=preset,
                font_file=font_file, render_cwd=workdir,
            )
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
    parser.add_argument("--no-overlay", action="store_true", help="nao desenhar textos no video base")
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    if not source.exists():
        print(f"[ERRO] entrada nao existe: {source}", file=sys.stderr)
        sys.exit(1)
    out_video = Path(args.out).expanduser().resolve()
    audio = Path(args.audio).expanduser().resolve() if args.audio else None

    try:
        montar(
            source,
            out_video,
            audio,
            args.fps,
            args.crf,
            args.preset,
            args.keep_workdir,
            overlay_enabled=not args.no_overlay,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\n[ERRO] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
