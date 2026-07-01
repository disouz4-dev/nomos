#!/usr/bin/env python3
"""
LÍDIA Rename — Renomeia arquivos com títulos descritivos via modelo local.
Fase 3 (opcional) do pipeline. Funciona sobre arquivos já classificados.

Uso:
    python _lidia_rename.py --destino /path [--lote N]
    python _lidia_rename.py  # pergunta interativamente
"""

import argparse
import re
import requests
import sqlite3
import sys
from pathlib import Path
from datetime import datetime

POV_PATH      = Path(__file__).parent
DB_PATH       = POV_PATH / "_lidia_state.db"
OLLAMA_URL    = "http://localhost:11434/api/generate"
OLLAMA_MODEL  = "gemma4:e4b"  # sobrescrito por --modelo
READ_CHARS    = 1800
DEFAULT_LOTE  = 20

NOME_GENERICO = re.compile(
    r"Conversa\s*#?\d*|sem título|Help request|sem t[ií]tulo|\(\d+\)",
    re.IGNORECASE
)
INVALIDOS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def log(msg: str):
    print(msg, flush=True)


def db_connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def gerar_titulo(conteudo: str, nome_original: str) -> str | None:
    prompt = f"""Arquivo: {nome_original}

Início do conteúdo:
{conteudo[:READ_CHARS]}

---
Leia o trecho acima e responda APENAS com um título de arquivo no formato:
AAAA-MM-DD — Título descritivo do assunto principal em português

Se não houver data identificável no conteúdo, escreva apenas o título:
Título descritivo do assunto principal em português

Regras:
- O título deve descrever o assunto real, não ser genérico
- Máximo de 80 caracteres no total
- Não use aspas, asteriscos ou qualquer marcação
- Responda SOMENTE com o título, sem mais nada"""

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1, "num_ctx": 8192}
    }
    resp = requests.post(OLLAMA_URL, json=payload, timeout=60)
    resp.raise_for_status()
    titulo = resp.json()["response"].strip()

    # Limpa o título
    titulo = re.sub(r"^[`\"'*#\-\s]+", "", titulo)
    titulo = re.sub(r"[`\"'*#]+$", "", titulo).strip()

    # Remove .md do final se o modelo adicionou
    titulo = re.sub(r"\.md$", "", titulo, flags=re.IGNORECASE).strip()

    if len(titulo) < 5:
        return None
    return titulo


def sanitize(nome: str) -> str:
    nome = INVALIDOS.sub("", nome)
    return nome[:180].strip()


def run(destino: Path, lote: int):
    log("=" * 60)
    log("  LÍDIA Rename — Renomeando arquivos")
    log("=" * 60)

    # Lista arquivos com nome genérico em toda a pasta destino
    candidatos = [
        f for f in destino.rglob("*.md")
        if not f.name.startswith("_") and NOME_GENERICO.search(f.name)
    ]

    log(f"\n  {len(candidatos)} arquivo(s) com nome genérico")
    log(f"  Processando lote de {lote}...\n")

    processados = 0
    erros = 0

    for path in candidatos[:lote]:
        try:
            conteudo = path.read_text(encoding="utf-8", errors="ignore")
            titulo = gerar_titulo(conteudo, path.name)

            if not titulo:
                log(f"  ⟳ Sem título útil: {path.name[:50]}")
                continue

            novo_nome = sanitize(titulo) + ".md"
            novo_path = path.parent / novo_nome

            # Evita colisão
            if novo_path.exists() and novo_path != path:
                stem = sanitize(titulo)
                novo_path = path.parent / f"{stem} (2).md"

            path.replace(novo_path)

            # Atualiza banco se possível
            conn = db_connect()
            cur = conn.cursor()
            cur.execute("""
                UPDATE arquivos SET arquivos_gerados=?, sessao_fim=?
                WHERE nome_original=?
            """, (novo_nome, datetime.now().isoformat(), str(path)))
            conn.commit()
            conn.close()

            log(f"  ✓ {path.name[:40]}")
            log(f"    → {novo_nome[:70]}")
            processados += 1

        except Exception as e:
            log(f"  ✗ {path.name[:50]}: {e}")
            erros += 1

    restantes = max(0, len(candidatos) - lote)
    log(f"\n{'=' * 60}")
    log(f"  Renomeados  : {processados}")
    log(f"  Erros       : {erros}")
    log(f"  Restantes   : {restantes}")
    if restantes > 0:
        log("  Execute novamente para continuar.")
    log("=" * 60)


def main():
    global OLLAMA_MODEL
    parser = argparse.ArgumentParser(description="LÍDIA Rename")
    parser.add_argument("--destino", help="Pasta com os arquivos organizados")
    parser.add_argument("--lote",   type=int, default=DEFAULT_LOTE)
    parser.add_argument("--modelo", help="Modelo Ollama a usar (ex: gemma3:4b)")
    args = parser.parse_args()

    if args.modelo:
        OLLAMA_MODEL = args.modelo

    if args.destino:
        destino = Path(args.destino).expanduser().resolve()
    else:
        destino_str = input("\n  📁 Pasta com os arquivos organizados?\n     Caminho: ").strip()
        destino = Path(destino_str).expanduser().resolve()

    if not destino.exists():
        log(f"  ✗ Destino não encontrado: {destino}")
        sys.exit(1)

    run(destino, args.lote or DEFAULT_LOTE)


if __name__ == "__main__":
    main()
