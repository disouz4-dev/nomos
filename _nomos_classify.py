#!/usr/bin/env python3
"""
Nomos Classify — Classifica arquivos por similaridade de embeddings.
Fase 2 do pipeline. Requer que o bootstrap já tenha criado as pastas.

Uso:
    python _nomos_classify.py --origem /path --destino /path [--lote N]
    python _nomos_classify.py  # pergunta interativamente
"""

import argparse
import sqlite3
import sys
import requests
import numpy as np
from pathlib import Path
from datetime import datetime

POV_PATH      = Path(__file__).parent
DB_PATH       = POV_PATH / "_nomos_state.db"
EMBED_URL     = "http://localhost:11434/api/embeddings"
EMBED_MODEL   = "nomic-embed-text"
EMBED_CHARS   = 1200
DEFAULT_LOTE  = 100

SISTEMA = {
    "_sobre_.md", "POV.md", "_lidia_rules_compact.md",
    "_lidia_consolidate_rules.md", "_lidia_runner.py",
    "_nomos_consolidate.py", "_nomos_bootstrap.py",
    "_nomos_classify.py", "_nomos_rename.py", "nomos_gui.py"
}


def log(msg: str):
    print(msg, flush=True)


# ── Banco ─────────────────────────────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def db_init():
    conn = db_connect()
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS arquivos (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            nome_original    TEXT NOT NULL UNIQUE,
            status           TEXT NOT NULL DEFAULT 'pendente',
            tipo             TEXT,
            acao_tomada      TEXT,
            destino          TEXT,
            arquivos_gerados TEXT,
            bruto_excluido   INTEGER DEFAULT 0,
            links_criados    TEXT,
            motivo           TEXT,
            hash_md5         TEXT,
            duplicata_de     TEXT,
            sessao_inicio    TEXT,
            sessao_fim       TEXT,
            erro_descricao   TEXT
        );
        CREATE TABLE IF NOT EXISTS sessoes (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            inicio               TEXT NOT NULL,
            fim                  TEXT,
            arquivos_processados INTEGER DEFAULT 0,
            arquivos_pendentes   INTEGER DEFAULT 0,
            duplicatas_removidas INTEGER DEFAULT 0,
            observacoes          TEXT
        );
    """)
    conn.commit()
    conn.close()


def db_registrar_pendentes(origem: Path) -> int:
    conn = db_connect()
    cur = conn.cursor()
    inseridos = 0
    for f in origem.rglob("*.md"):
        if f.name.startswith("_") or f.name in SISTEMA:
            continue
        cur.execute("SELECT id FROM arquivos WHERE nome_original=?", (str(f),))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO arquivos (nome_original, status) VALUES (?, 'pendente')",
                (str(f),)
            )
            inseridos += 1
    conn.commit()
    conn.close()
    return inseridos


def db_pendentes(lote: int) -> list[str]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT nome_original FROM arquivos WHERE status='pendente' ORDER BY id LIMIT ?",
        (lote,)
    )
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows


def db_total_pendentes() -> int:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM arquivos WHERE status='pendente'")
    n = cur.fetchone()[0]
    conn.close()
    return n


def db_concluido(nome: str, destino: str, score: float):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        UPDATE arquivos SET
            status='concluido', acao_tomada='renomear_mover',
            destino=?, motivo=?, sessao_fim=?
        WHERE nome_original=?
    """, (destino, f"embed cosine={score:.4f}", datetime.now().isoformat(), nome))
    conn.commit()
    conn.close()


def db_erro(nome: str, descricao: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        UPDATE arquivos SET status='erro', erro_descricao=?, sessao_fim=?
        WHERE nome_original=?
    """, (descricao, datetime.now().isoformat(), nome))
    conn.commit()
    conn.close()


# ── Embeddings ────────────────────────────────────────────────────────────────

def embed(texto: str) -> np.ndarray:
    resp = requests.post(
        EMBED_URL,
        json={"model": EMBED_MODEL, "prompt": texto[:EMBED_CHARS]},
        timeout=30
    )
    resp.raise_for_status()
    return np.array(resp.json()["embedding"], dtype=np.float32)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def carregar_pastas(destino: Path) -> dict[str, tuple[Path, np.ndarray]]:
    """Retorna {nome_pasta: (path_pasta, embedding_sobre_md)}"""
    pastas: dict[str, tuple[Path, np.ndarray]] = {}
    for sobre in sorted(destino.rglob("_sobre_.md")):
        pasta = sobre.parent
        # Ignora POV e subpastas do sistema
        if pasta == POV_PATH or "POV" in pasta.parts:
            continue
        try:
            conteudo = sobre.read_text(encoding="utf-8", errors="ignore")
            emb = embed(conteudo)
            pastas[pasta.name] = (pasta, emb)
            log(f"  ✓ Pasta indexada: {pasta.name}")
        except Exception as e:
            log(f"  ✗ Erro ao indexar {pasta.name}: {e}")
    return pastas


def classificar(path: Path, pastas: dict) -> tuple[str, Path, float]:
    """Retorna (nome_pasta, path_pasta, score)."""
    lore_nome = "35-Lore-Geral"
    lore_path = pastas.get(lore_nome, (path.parent,))[0]

    try:
        conteudo = path.read_text(encoding="utf-8", errors="ignore")
        if not conteudo.strip():
            return lore_nome, lore_path, 0.0
        file_emb = embed(conteudo)
    except Exception:
        return lore_nome, lore_path, 0.0

    melhor_nome  = lore_nome
    melhor_path  = lore_path
    melhor_score = -1.0

    for nome, (pasta_path, pasta_emb) in pastas.items():
        score = cosine(file_emb, pasta_emb)
        if score > melhor_score:
            melhor_score = score
            melhor_nome  = nome
            melhor_path  = pasta_path

    return melhor_nome, melhor_path, melhor_score


# ── Pipeline ─────────────────────────────────────────────────────────────────

def run(origem: Path, destino: Path, lote: int):
    log("=" * 60)
    log("  Nomos Classify — Classificação por embeddings")
    log("=" * 60)

    db_init()

    inseridos = db_registrar_pendentes(origem)
    if inseridos:
        log(f"\n  ✓ {inseridos} novos arquivos registrados")

    log("\n  Indexando pastas (_sobre_.md)...")
    pastas = carregar_pastas(destino)
    if not pastas:
        log("  ✗ Nenhuma pasta com _sobre_.md encontrada.")
        log("  Execute primeiro: python _nomos_bootstrap.py")
        sys.exit(1)
    log(f"  ✓ {len(pastas)} pastas indexadas\n")

    pendentes = db_pendentes(lote)
    total_pendentes = db_total_pendentes()

    if not pendentes:
        log("  ✅ Nenhum arquivo pendente.")
        return

    log(f"  Classificando {len(pendentes)} de {total_pendentes} pendentes...\n")

    processados = 0
    erros = 0

    for nome_original in pendentes:
        path = Path(nome_original)

        # Tenta encontrar o arquivo se o caminho completo não bate
        if not path.exists():
            matches = list(origem.rglob(path.name))
            path = matches[0] if matches else None

        if not path or not path.exists():
            db_erro(nome_original, "Arquivo não encontrado")
            log(f"  ✗ Não encontrado: {Path(nome_original).name[:50]}")
            erros += 1
            continue

        pasta_nome, pasta_path, score = classificar(path, pastas)

        try:
            pasta_path.mkdir(parents=True, exist_ok=True)
            novo_path = pasta_path / path.name

            # Evita colisão de nomes
            if novo_path.exists() and novo_path != path:
                stem = path.stem
                novo_path = pasta_path / f"{stem}__{path.stat().st_size}.md"

            path.replace(novo_path)
            db_concluido(nome_original, str(pasta_path), score)

            label = "✓" if pasta_nome != "35-Lore-Geral" else "→"
            log(f"  {label} {path.name[:45]:45}  →  {pasta_nome} ({score:.2f})")
            processados += 1
        except Exception as e:
            db_erro(nome_original, str(e))
            log(f"  ✗ {path.name[:50]}: {e}")
            erros += 1

    restantes = total_pendentes - processados
    log(f"\n{'=' * 60}")
    log(f"  Processados : {processados}")
    log(f"  Erros       : {erros}")
    log(f"  Restantes   : {restantes}")
    if restantes > 0:
        log("  Execute novamente para continuar o próximo lote.")
    log("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Nomos Classify")
    parser.add_argument("--origem",  help="Pasta com arquivos brutos")
    parser.add_argument("--destino", help="Pasta de destino organizada")
    parser.add_argument("--lote", type=int, default=DEFAULT_LOTE,
                        help=f"Arquivos por execução (padrão: {DEFAULT_LOTE})")
    args = parser.parse_args()

    if args.origem and args.destino:
        origem  = Path(args.origem).expanduser().resolve()
        destino = Path(args.destino).expanduser().resolve()
    else:
        origem_str  = input("\n  📂 Pasta com os arquivos brutos?\n     Caminho: ").strip()
        destino_str = input("  📁 Pasta de destino (mesma do bootstrap)?\n     Caminho: ").strip()
        origem  = Path(origem_str).expanduser().resolve()
        destino = Path(destino_str).expanduser().resolve()

    if not origem.exists():
        log(f"  ✗ Origem não encontrada: {origem}")
        sys.exit(1)

    run(origem, destino, args.lote or DEFAULT_LOTE)


if __name__ == "__main__":
    main()
