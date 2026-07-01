#!/usr/bin/env python3
"""
LÍDIA Bootstrap — Cria taxonomia cobrindo TODOS os arquivos.

Estratégia:
  1. Processa todos os arquivos em lotes de BATCH_SIZE
  2. Cada lote gera CATS_PER_BATCH categorias candidatas
  3. Consolidação final remove duplicatas e produz a taxonomia definitiva

Uso:
    python _lidia_bootstrap.py --origem /path --destino /path
    python _lidia_bootstrap.py  # pergunta interativamente
"""

import argparse
import json
import os
import random
import re
import requests
import subprocess
import sys
from pathlib import Path

OLLAMA_URL    = "http://localhost:11434/api/generate"
OLLAMA_MODEL  = "gemma4:e4b"
EXCERPT_CHARS  = 400   # chars por arquivo
CATS_PER_BATCH = 8     # categorias candidatas por lote
FINAL_CATS     = 35    # máximo de categorias na taxonomia final


def detectar_batch_size() -> int:
    """Calcula o lote ideal com base no hardware disponível."""
    # Tenta ler VRAM disponível (nvidia)
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            timeout=3, text=True
        ).strip().splitlines()[0]
        vram_free_mb = int(out.strip())
        # gemma4:e4b usa ~4GB; com 8GB+ livres, lotes maiores são seguros
        if vram_free_mb >= 6000:
            return 80
        elif vram_free_mb >= 3000:
            return 50
        else:
            return 30
    except Exception:
        pass

    # Fallback: RAM do sistema
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable"):
                    ram_free_kb = int(line.split()[1])
                    ram_free_gb = ram_free_kb / 1024 / 1024
                    if ram_free_gb >= 12:
                        return 60
                    elif ram_free_gb >= 6:
                        return 40
                    else:
                        return 25
    except Exception:
        pass

    return 40  # default conservador


BATCH_SIZE = detectar_batch_size()

SISTEMA = {
    "_sobre_.md", "POV.md", "_lidia_rules_compact.md",
    "_lidia_consolidate_rules.md", "_lidia_runner.py",
    "_lidia_consolidate.py", "_lidia_bootstrap.py",
    "_lidia_embed_classify.py", "_lidia_rename.py",
    "_lidia_links.py", "nomos_gui.py"
}


def log(msg: str):
    print(msg, flush=True)


def read_excerpt(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:EXCERPT_CHARS]
    except Exception:
        return ""


# ── Fase 1: candidatos por lote ───────────────────────────────────────────────

def gerar_candidatos(excerpts: list[tuple[str, str]], lote_num: int, total_lotes: int) -> list[dict]:
    amostras_txt = "\n\n".join(
        f"=== {nome} ===\n{texto}"
        for nome, texto in excerpts
    )

    prompt = f"""Você está analisando um lote de arquivos de um vault pessoal (lote {lote_num}/{total_lotes}).

{amostras_txt}

---

Com base nesses arquivos, identifique exatamente {CATS_PER_BATCH} temas distintos presentes no lote.
Cada tema deve ser uma "gaveta" com natureza única.

Responda APENAS com um array JSON. Sem explicações, sem markdown, sem texto extra.

[
  {{
    "nome": "Nome-Da-Pasta",
    "proposito": "Uma frase curta definindo o que esta pasta representa",
    "pertence": "Tipo de conteúdo que vai aqui",
    "nao_pertence": "O que NÃO vai aqui mesmo parecendo relacionado"
  }}
]"""

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2, "num_ctx": 16384}
    }
    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=120)
        resp.raise_for_status()
        raw = resp.json()["response"].strip()
        return extrair_json(raw)
    except Exception as e:
        log(f"    ⚠ Lote {lote_num}: {e}")
        return []


def extrair_json(raw: str) -> list[dict]:
    if "```" in raw:
        for parte in raw.split("```"):
            parte = parte.strip().lstrip("json").strip()
            try:
                r = json.loads(parte)
                if isinstance(r, list):
                    return r
            except json.JSONDecodeError:
                continue
    inicio = raw.find("[")
    fim    = raw.rfind("]") + 1
    if inicio >= 0 and fim > inicio:
        try:
            return json.loads(raw[inicio:fim])
        except Exception:
            pass
    return []


# ── Fase 2: consolidação ──────────────────────────────────────────────────────

def consolidar(candidatos: list[dict]) -> list[dict]:
    if not candidatos:
        return []

    candidatos_txt = json.dumps(candidatos, ensure_ascii=False, indent=2)

    prompt = f"""Você recebeu uma lista de {len(candidatos)} categorias candidatas geradas de diferentes lotes de arquivos.
Muitas podem ser duplicatas, variações do mesmo tema ou se sobrepor.

{candidatos_txt}

---

Sua tarefa:
1. Mescle categorias APENAS quando forem essencialmente a mesma coisa (ex: "Política" e "Política Brasileira")
2. Mantenha categorias distintas separadas — prefira granularidade a fusão excessiva
3. O resultado final deve ter entre 20 e {FINAL_CATS} categorias únicas e bem definidas
4. Para cada categoria consolidada, reescreva o propósito, pertence e nao_pertence de forma clara

Responda APENAS com um array JSON final. Sem explicações, sem markdown.

[
  {{
    "nome": "Nome-Da-Pasta",
    "proposito": "Uma frase curta definindo o que esta pasta representa",
    "pertence": "Tipo de conteúdo que vai aqui",
    "nao_pertence": "O que NÃO vai aqui mesmo parecendo relacionado"
  }}
]"""

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1, "num_ctx": 32768}
    }
    log(f"\n  ⟳ Consolidando {len(candidatos)} candidatos em taxonomia final...")
    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=180)
        resp.raise_for_status()
        raw = resp.json()["response"].strip()
        resultado = extrair_json(raw)
        if resultado:
            return resultado
    except Exception as e:
        log(f"  ⚠ Erro na consolidação: {e}")

    # Fallback: deduplica por nome normalizado sem LLM
    vistos: set[str] = set()
    dedup: list[dict] = []
    for c in candidatos:
        chave = re.sub(r"[^a-z]", "", c.get("nome", "").lower())
        if chave not in vistos:
            vistos.add(chave)
            dedup.append(c)
    return dedup[:FINAL_CATS]


# ── Criação de pastas ─────────────────────────────────────────────────────────

def criar_sobre_md(pasta: Path, cat: dict):
    sobre = pasta / "_sobre_.md"
    if sobre.exists():
        log(f"  ⟳ Já existe: {pasta.name}/_sobre_.md")
        return
    sobre.write_text(
        f"# {cat['nome']}\n\n"
        f"## Propósito\n\n{cat.get('proposito', '')}\n\n"
        f"## O que pertence aqui\n\n{cat.get('pertence', '')}\n\n"
        f"## O que não pertence aqui\n\n{cat.get('nao_pertence', '')}\n",
        encoding="utf-8"
    )
    log(f"  ✓ {pasta.name}/_sobre_.md")


def criar_lore_geral(dest: Path):
    pasta = dest / "35-Lore-Geral"
    pasta.mkdir(parents=True, exist_ok=True)
    sobre = pasta / "_sobre_.md"
    if not sobre.exists():
        sobre.write_text(
            "# 35-Lore-Geral\n\n"
            "## Propósito\n\nRepositório para arquivos com domínio ambíguo.\n\n"
            "## O que pertence aqui\n\nArquivos sem categoria clara identificável.\n\n"
            "## O que não pertence aqui\n\nArquivos com domínio claramente identificado.\n",
            encoding="utf-8"
        )
        log("  ✓ 35-Lore-Geral/_sobre_.md")


def criar_akai_ito(dest: Path):
    pasta = dest / "00-Akai-Ito"
    pasta.mkdir(parents=True, exist_ok=True)
    sobre = pasta / "_sobre_.md"
    if not sobre.exists():
        sobre.write_text(
            "# Akai Ito\n\n"
            "## Propósito\n\nMemórias e registros de todas as mulheres com quem Diego teve"
            " algum vínculo afetivo, romântico ou sexual — de encontros casuais a relacionamentos.\n\n"
            "## O que pertence aqui\n\nConversas, diários, reflexões e memórias sobre mulheres"
            " com quem Diego ficou, namorou, teve relações sexuais ou foi casado."
            " Inclui sentimentos, saudades, conflitos e histórias de cada uma.\n\n"
            "## O que não pertence aqui\n\nAmizades femininas sem vínculo afetivo/romântico."
            " Relacionamentos profissionais. Memórias familiares.\n",
            encoding="utf-8"
        )
        log("  ✓ 00-Akai-Ito/_sobre_.md  [pasta fixa]")


# ── Pipeline principal ────────────────────────────────────────────────────────

def run(origem: Path, destino: Path):
    log("=" * 60)
    log("  LÍDIA Bootstrap — Cobertura total do vault")
    log("=" * 60)

    todos = [
        f for f in origem.rglob("*.md")
        if not f.name.startswith("_") and f.name not in SISTEMA
    ]

    if not todos:
        log("  ✗ Nenhum arquivo .md encontrado na origem.")
        sys.exit(1)

    random.shuffle(todos)
    total = len(todos)
    lotes = [todos[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    total_lotes = len(lotes)

    log(f"\n  {total} arquivos encontrados")
    log(f"  Lote automático: {BATCH_SIZE} arquivos/lote (detectado pelo hardware)")
    log(f"  {total_lotes} lotes no total · {CATS_PER_BATCH} candidatos por lote → consolidação final\n")

    todos_candidatos: list[dict] = []

    for i, lote in enumerate(lotes, 1):
        log(f"  [{i:02d}/{total_lotes}] Analisando {len(lote)} arquivos...")
        excerpts = [(f.name, read_excerpt(f)) for f in lote]
        candidatos = gerar_candidatos(excerpts, i, total_lotes)
        todos_candidatos.extend(candidatos)
        log(f"         → {len(candidatos)} candidatos  (acumulado: {len(todos_candidatos)})")

    if not todos_candidatos:
        log("\n  ✗ Nenhum candidato gerado. Verifique o Ollama.")
        sys.exit(1)

    categorias = consolidar(todos_candidatos)

    if not categorias:
        log("\n  ✗ Consolidação falhou.")
        sys.exit(1)

    log(f"  ✓ {len(categorias)} categorias na taxonomia final\n")
    log("  ─── Taxonomia aprovada. Criando pastas agora... ───\n")

    destino.mkdir(parents=True, exist_ok=True)
    criar_akai_ito(destino)
    criar_lore_geral(destino)

    for i, cat in enumerate(categorias, 1):
        nome = cat.get("nome", "").strip()
        if not nome:
            continue
        # Prefixo numérico para ordenação, pula o 35 reservado pro Lore
        prefixo = i if i < 35 else i + 1
        pasta = destino / f"{prefixo:02d}-{nome}"
        pasta.mkdir(parents=True, exist_ok=True)
        criar_sobre_md(pasta, cat)

    log(f"\n{'=' * 60}")
    log(f"  Bootstrap concluído — {len(categorias) + 1} pastas criadas")
    log(f"  Cobertura: {total} arquivos analisados (100%)")
    log(f"  Próximo: python _lidia_embed_classify.py")
    log("=" * 60)


def main():
    global OLLAMA_MODEL
    parser = argparse.ArgumentParser(description="LÍDIA Bootstrap")
    parser.add_argument("--origem",  help="Pasta com arquivos brutos")
    parser.add_argument("--destino", help="Pasta de destino organizada")
    parser.add_argument("--modelo",  help="Modelo Ollama a usar (ex: gemma3:4b)")
    args = parser.parse_args()

    if args.modelo:
        OLLAMA_MODEL = args.modelo

    if args.origem and args.destino:
        origem  = Path(args.origem).expanduser().resolve()
        destino = Path(args.destino).expanduser().resolve()
    else:
        origem_str  = input("\n  📂 Pasta com os arquivos brutos?\n     Caminho: ").strip()
        destino_str = input("  📁 Pasta de destino?\n     Caminho: ").strip()
        origem  = Path(origem_str).expanduser().resolve()
        destino = Path(destino_str).expanduser().resolve()

    if not origem.exists():
        log(f"  ✗ Origem não encontrada: {origem}")
        sys.exit(1)

    run(origem, destino)


if __name__ == "__main__":
    main()
