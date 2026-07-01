#!/usr/bin/env python3
"""
Nomos Links — Cria wiki links com nó central por pasta (hub-and-spoke).

Para cada pasta classificada:
  1. Cria (ou atualiza) um nó central MOC cujo nome é o tema da pasta
     (extraído do _sobre_.md → linha de Propósito)
  2. O MOC lista todos os arquivos da pasta como [[links]]
  3. Cada arquivo da pasta recebe um link de volta para [[MOC]]
  4. Arquivos com similaridade ≥ threshold também se ligam entre si (spokes)

Uso:
    python _nomos_links.py --destino /path/vault [--threshold 0.75] [--max-links 5]
    python _nomos_links.py
"""

import argparse
import re
import sys
import requests
import numpy as np
from pathlib import Path

EMBED_URL   = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"
EMBED_CHARS = 1200

DEFAULT_THRESHOLD = 0.75
DEFAULT_MAX_LINKS = 5

SISTEMA = {
    "_sobre_.md", "POV.md", "_lidia_rules_compact.md",
    "_lidia_consolidate_rules.md", "_lidia_runner.py",
    "_nomos_consolidate.py", "_nomos_bootstrap.py",
    "_nomos_classify.py", "_nomos_rename.py",
    "_nomos_links.py", "nomos_gui.py"
}


def log(msg: str):
    print(msg, flush=True)


# ── Embedding ─────────────────────────────────────────────────────────────────

def embed(texto: str) -> np.ndarray | None:
    try:
        resp = requests.post(
            EMBED_URL,
            json={"model": EMBED_MODEL, "prompt": texto[:EMBED_CHARS]},
            timeout=30
        )
        resp.raise_for_status()
        return np.array(resp.json()["embedding"], dtype=np.float32)
    except Exception as e:
        log(f"    ✗ embed: {e}")
        return None


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


# ── MOC helpers ───────────────────────────────────────────────────────────────

def extrair_tema(pasta: Path) -> str:
    """
    Retorna o nome do tema usando o nome da pasta como fonte primária.
    O nome da pasta já é descritivo (criado pelo bootstrap).
    """
    nome = pasta.name
    # Remove prefixo numérico tipo "01-", "35-"
    nome = re.sub(r"^\d+-", "", nome).strip()
    # Se não tem espaço mas tem hifens, converte hifens em espaços
    if "-" in nome and " " not in nome:
        nome = nome.replace("-", " ")
    return nome


def nome_moc(tema: str) -> str:
    """Gera o nome do arquivo MOC a partir do tema."""
    # Remove caracteres inválidos, mantém legível
    limpo = re.sub(r'[<>:"/\\|?*]', "", tema).strip()
    return f"{limpo}.md"


def remover_bloco_links(conteudo: str) -> str:
    """Remove bloco de links gerado anteriormente."""
    marcadores = [
        "\n\n---\n\n## Ver também\n",
        "\n\n---\n\n## Contexto\n",
    ]
    for m in marcadores:
        idx = conteudo.find(m)
        if idx >= 0:
            return conteudo[:idx]
    return conteudo


def links_no_conteudo(conteudo: str) -> set[str]:
    return set(re.findall(r"\[\[(.+?)(?:\|.+?)?\]\]", conteudo))


# ── Por pasta ─────────────────────────────────────────────────────────────────

def processar_pasta(pasta: Path, threshold: float, max_links: int) -> int:
    """
    Cria o nó central (MOC) da pasta e atualiza os links de todos os arquivos.
    Retorna número de arquivos modificados.
    """
    sobre = pasta / "_sobre_.md"

    # Arquivos de conteúdo (exclui sistema e MOC gerado anteriormente)
    arquivos = [
        f for f in pasta.iterdir()
        if f.is_file() and f.suffix == ".md"
        and not f.name.startswith("_")
        and f.name not in SISTEMA
    ]

    if not arquivos:
        return 0

    # ── 1. Determina o tema e cria/atualiza o MOC ─────────────────────────────
    tema = extrair_tema(pasta)
    moc_nome = nome_moc(tema)
    moc_path = pasta / moc_nome

    # Remove arquivos que são o próprio MOC da lista de conteúdo
    arquivos = [f for f in arquivos if f.name != moc_nome]
    if not arquivos:
        return 0

    log(f"\n  📁 {pasta.name}  →  nó central: [[{Path(moc_nome).stem}]]  ({len(arquivos)} arquivos)")

    # Conteúdo do MOC
    lista_links = "\n".join(f"- [[{f.stem}]]" for f in sorted(arquivos, key=lambda f: f.name))
    moc_conteudo = (
        f"# {tema}\n\n"
        f"> Mapa de conteúdo · {len(arquivos)} arquivo(s)\n\n"
        f"---\n\n"
        f"## Arquivos nesta pasta\n\n"
        f"{lista_links}\n"
    )

    try:
        moc_path.write_text(moc_conteudo, encoding="utf-8")
        log(f"    ✓ MOC criado/atualizado: {moc_nome}")
    except Exception as e:
        log(f"    ✗ Erro ao criar MOC: {e}")
        return 0

    # ── 2. Gera embeddings para links entre spokes ─────────────────────────────
    dados: list[tuple[Path, str, np.ndarray | None]] = []
    for arq in arquivos:
        try:
            conteudo = arq.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        emb = embed(conteudo) if len(arquivos) > 1 else None
        dados.append((arq, conteudo, emb))

    modificados = 0
    moc_stem = moc_path.stem

    for i, (path_i, conteudo_i, emb_i) in enumerate(dados):
        # Links entre spokes por similaridade
        spokes: list[tuple[float, str]] = []
        if emb_i is not None:
            for j, (path_j, _, emb_j) in enumerate(dados):
                if i == j or emb_j is None:
                    continue
                score = cosine(emb_i, emb_j)
                if score >= threshold:
                    spokes.append((score, path_j.stem))
            spokes.sort(reverse=True)
            spokes = spokes[:max_links]

        # ── Bloco "Ver também" ─────────────────────────────────────────────────
        # Nó central sempre primeiro, depois os spokes similares
        linhas_links = [f"- [[{moc_stem}]]  ← tema desta pasta"]
        for _, nome in spokes:
            linhas_links.append(f"- [[{nome}]]")

        bloco = (
            "\n\n---\n\n"
            "## Ver também\n\n"
            + "\n".join(linhas_links)
        )

        base = remover_bloco_links(conteudo_i)

        # Verifica se já está idêntico para evitar writes desnecessários
        novo_conteudo = base + bloco
        if novo_conteudo == conteudo_i:
            continue

        try:
            path_i.write_text(novo_conteudo, encoding="utf-8")
            spoke_info = f" + {len(spokes)} spoke(s)" if spokes else ""
            log(f"    ✓ {path_i.name[:50]}  →  [[{moc_stem}]]{spoke_info}")
            modificados += 1
        except Exception as e:
            log(f"    ✗ {path_i.name}: {e}")

    return modificados + 1  # +1 pelo próprio MOC


# ── Pipeline ──────────────────────────────────────────────────────────────────

def run(destino: Path, threshold: float, max_links: int):
    log("=" * 60)
    log("  Nomos Links — Hub-and-spoke por pasta")
    log(f"  Threshold: {threshold}  |  Máx. spokes: {max_links}")
    log("=" * 60)

    pastas_ignoradas = {"POV", "Lídia Memory"}

    pastas = sorted([
        d for d in destino.iterdir()
        if d.is_dir()
        and not d.name.startswith(".")
        and d.name not in pastas_ignoradas
    ])

    if not pastas:
        log("\n  ✗ Nenhuma pasta temática encontrada.")
        log("  Execute bootstrap + classify primeiro.")
        return

    total = 0
    for pasta in pastas:
        n = processar_pasta(pasta, threshold, max_links)
        total += n

        # Subpastas (ex: 01-Akai-Ito/Carol/)
        for sub in sorted(pasta.iterdir()):
            if sub.is_dir() and not sub.name.startswith("_"):
                n2 = processar_pasta(sub, threshold, max_links)
                total += n2

    log(f"\n{'=' * 60}")
    log(f"  Arquivos criados/atualizados: {total}")
    log("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Nomos Links")
    parser.add_argument("--destino",   help="Pasta raiz do vault organizado")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--max-links", type=int,   default=DEFAULT_MAX_LINKS)
    args = parser.parse_args()

    if args.destino:
        destino = Path(args.destino).expanduser().resolve()
    else:
        s = input("\n  📁 Pasta raiz do vault organizado?\n     Caminho: ").strip()
        destino = Path(s).expanduser().resolve()

    if not destino.exists():
        log(f"  ✗ Não encontrado: {destino}")
        sys.exit(1)

    run(destino, args.threshold, args.max_links)


if __name__ == "__main__":
    main()
