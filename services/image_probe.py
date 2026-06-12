"""Detecção de formato e dimensões de imagem por magic bytes.

Funções puras (bytes -> dados) extraídas de app.py. Não confiam no mimetype
enviado pelo browser: inspecionam o conteúdo real do arquivo.
"""
from __future__ import annotations


def jpeg_size(data: bytes) -> tuple[int, int]:
    """Extrai (width, height) dos marcadores SOF de um JPEG; (0,0) se falhar."""
    i = 2
    try:
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return (
                    int.from_bytes(data[i + 7:i + 9], "big"),
                    int.from_bytes(data[i + 5:i + 7], "big"),
                )
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            i += 2 + max(seg_len, 2)
    except Exception:  # noqa: BLE001 - dimensao e best-effort, nunca bloqueia o upload
        pass
    return 0, 0


def image_kind_and_size(data: bytes) -> tuple[str, int, int]:
    """Detecta o formato por magic bytes e le as dimensoes quando possivel.

    Retorna (extensao, width, height); width/height = 0 quando nao deu para ler.
    Levanta ValueError para formatos nao suportados.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        w = h = 0
        if len(data) >= 24:
            w = int.from_bytes(data[16:20], "big")
            h = int.from_bytes(data[20:24], "big")
        return ".png", w, h
    if data.startswith(b"\xff\xd8\xff"):
        w, h = jpeg_size(data)
        return ".jpg", w, h
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", 0, 0
    raise ValueError("Arquivo nao e PNG, JPEG ou WebP.")
