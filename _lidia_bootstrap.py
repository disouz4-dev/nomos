#!/usr/bin/env python3
"""
Nomos Bootstrap — Cria taxonomia a partir de TODOS os arquivos.

Estratégia:
  1. Lê TODOS os arquivos e acumula excerpts (sem chamar LLM ainda)
  2. Com tudo lido, chama o LLM UMA VEZ para gerar a taxonomia completa
     (se o volume for muito grande, extrai temas por grupo e consolida no final)
  3. Cria as pastas SOMENTE após a taxonomia estar completa

Uso:
    python _lidia_bootstrap.py --origem /path --destino /path
"""

import argparse
import json
import re
import requests
import subprocess
import sys
from pathlib import Path

OLLAMA_URL    = "http://localhost:11434/api/generate"
OLLAMA_MODEL  = "gemma4:e4b"
EXCERPT_CHARS = 80   # chars por arquivo para o resumo de conteúdo
MAX_CHARS_PER_CALL = 36_000  # limite seguro por chamada LLM (contexto 32K tokens)

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
        return path.read_text(encoding="utf-8", errors="ignore")[:EXCERPT_CHARS].replace("\n", " ").strip()
    except Exception:
        return ""


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


def _chamar_llm(prompt: str, timeout: int = 300) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.15, "num_ctx": 32768}
    }
    resp = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["response"].strip()


def extrair_temas_grupo(linhas: list[str], grupo: int, total_grupos: int) -> list[str]:
    """Extrai lista de temas (nomes simples) de um grupo de arquivos — saída compacta."""
    conteudo = "\n".join(linhas)
    prompt = f"""Você está analisando um grupo de arquivos de um vault pessoal ({grupo}/{total_grupos}).

{conteudo}

---

Liste os TEMAS DISTINTOS presentes nesses arquivos.
Cada tema = uma palavra ou expressão curta (ex: "Saúde Mental", "Jogos", "Política").
Não agrupe artificialmente — separe temas que são realmente diferentes.
Sem explicações. Responda apenas com uma lista, um tema por linha."""

    try:
        raw = _chamar_llm(prompt, timeout=120)
        temas = [l.strip().lstrip("-*•123456789. ") for l in raw.splitlines() if l.strip()]
        return [t for t in temas if t]
    except Exception as e:
        log(f"    ⚠ Grupo {grupo}: {e}")
        return []


def gerar_taxonomia_completa(linhas_todas: list[str]) -> list[dict]:
    """Gera taxonomia final com todos os excerpts em uma única chamada LLM."""
    conteudo = "\n".join(linhas_todas)
    prompt = f"""Você está analisando {len(linhas_todas)} arquivos de um vault pessoal.

{conteudo}

---

Analise TODOS os arquivos acima e gere a taxonomia completa de pastas para organizá-los.
Regras:
- Crie tantas categorias quantas forem necessárias — SEM limite de quantidade
- Não funda temas distintos: "Saúde Mental" e "Psicologia Clínica" são diferentes; "Jogos" e "Entretenimento" são diferentes
- Agrupe APENAS categorias que são literalmente a mesma coisa com nome diferente
- Cada pasta deve representar um volume real de arquivos

Responda APENAS com um array JSON. Sem explicações, sem markdown.

[
  {{
    "nome": "Nome-Da-Pasta",
    "proposito": "Uma frase curta definindo o que esta pasta representa",
    "pertence": "Tipo de conteúdo que vai aqui",
    "nao_pertence": "O que NÃO vai aqui mesmo parecendo relacionado"
  }}
]"""

    log("  ⟳ Gerando taxonomia a partir de todos os arquivos...")
    try:
        raw = _chamar_llm(prompt, timeout=300)
        resultado = extrair_json(raw)
        if resultado:
            return resultado
        log("  ⚠ LLM não retornou JSON válido.")
    except Exception as e:
        log(f"  ⚠ Erro: {e}")
    return []


def gerar_taxonomia_via_temas(temas_todos: list[str]) -> list[dict]:
    """Gera taxonomia a partir de lista de temas extraídos (quando conteúdo é muito grande)."""
    temas_txt = "\n".join(f"- {t}" for t in temas_todos)
    prompt = f"""Você recebeu uma lista de temas identificados em um vault pessoal com muitos arquivos:

{temas_txt}

---

Com base nesses temas, gere a taxonomia completa de pastas.
Regras:
- Crie tantas categorias quantas forem necessárias — SEM limite de quantidade
- Mescle APENAS temas que são literalmente o mesmo assunto com nome diferente
- Mantenha temas distintos separados — não generalize demais
- Para cada pasta, defina propósito, pertence e nao_pertence

Responda APENAS com um array JSON. Sem explicações, sem markdown.

[
  {{
    "nome": "Nome-Da-Pasta",
    "proposito": "Uma frase curta definindo o que esta pasta representa",
    "pertence": "Tipo de conteúdo que vai aqui",
    "nao_pertence": "O que NÃO vai aqui mesmo parecendo relacionado"
  }}
]"""

    log("  ⟳ Gerando taxonomia a partir dos temas consolidados...")
    try:
        raw = _chamar_llm(prompt, timeout=300)
        resultado = extrair_json(raw)
        if resultado:
            return resultado
    except Exception as e:
        log(f"  ⚠ Erro: {e}")
    return []


# ── Criação de pastas ──────────────────────────────────────────────────────────

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
    pasta = dest / "Lore-Geral"
    pasta.mkdir(parents=True, exist_ok=True)
    sobre = pasta / "_sobre_.md"
    if not sobre.exists():
        sobre.write_text(
            "# Lore-Geral\n\n"
            "## Propósito\n\nRepositório para arquivos com domínio ambíguo.\n\n"
            "## O que pertence aqui\n\nArquivos sem categoria clara identificável.\n\n"
            "## O que não pertence aqui\n\nArquivos com domínio claramente identificado.\n",
            encoding="utf-8"
        )
        log("  ✓ Lore-Geral/_sobre_.md")


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


# ── Pipeline principal ─────────────────────────────────────────────────────────

def detectar_batch_size() -> int:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            timeout=3, text=True
        ).strip().splitlines()[0]
        vram_free_mb = int(out.strip())
        if vram_free_mb >= 6000:
            return 80
        elif vram_free_mb >= 3000:
            return 50
        else:
            return 30
    except Exception:
        pass
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
    return 40


def run(origem: Path, destino: Path):
    log("=" * 60)
    log("  Nomos Bootstrap — Taxonomia total do vault")
    log("=" * 60)

    todos = [
        f for f in origem.rglob("*.md")
        if not f.name.startswith("_") and f.name not in SISTEMA
    ]

    if not todos:
        log("  ✗ Nenhum arquivo .md encontrado na origem.")
        sys.exit(1)

    total = len(todos)
    log(f"\n  {total} arquivos encontrados")
    log(f"  Fase 1/2: lendo todos os arquivos...\n")

    # ── FASE 1: Lê TODOS os arquivos, acumula excerpts ─────────────────────────
    BATCH_RD = detectar_batch_size()
    linhas: list[str] = []
    for i, f in enumerate(todos):
        excerpt = read_excerpt(f)
        linhas.append(f"[{f.name}] {excerpt}")
        if (i + 1) % BATCH_RD == 0 or (i + 1) == total:
            log(f"  [{i + 1:04d}/{total:04d}] lidos...")

    log(f"\n  ✓ {total} arquivos lidos")

    # ── FASE 2: Taxonomia — uma chamada ou duas se o volume for grande ─────────
    total_chars = sum(len(l) for l in linhas)
    log(f"  Volume de conteúdo: {total_chars:,} chars")
    log(f"\n  Fase 2/2: gerando taxonomia completa...\n")

    if total_chars <= MAX_CHARS_PER_CALL:
        # Tudo cabe em uma chamada — taxonomia direta
        categorias = gerar_taxonomia_completa(linhas)
    else:
        # Conteúdo grande: extrai temas por grupo, depois uma taxonomia dos temas
        tamanho_grupo = MAX_CHARS_PER_CALL // (EXCERPT_CHARS + 60)
        grupos = [linhas[i:i + tamanho_grupo] for i in range(0, total, tamanho_grupo)]
        total_grupos = len(grupos)
        log(f"  Volume grande — extraindo temas em {total_grupos} grupos antes de taxonomizar\n")

        temas_todos: list[str] = []
        for gi, grupo in enumerate(grupos, 1):
            log(f"  [{gi:02d}/{total_grupos}] Extraindo temas de {len(grupo)} arquivos...")
            temas = extrair_temas_grupo(grupo, gi, total_grupos)
            temas_todos.extend(temas)
            log(f"         → {len(temas)} temas  (acumulado: {len(temas_todos)})")

        # Remove duplicatas óbvias por normalização
        vistos: set[str] = set()
        temas_unicos: list[str] = []
        for t in temas_todos:
            chave = re.sub(r"[^a-z]", "", t.lower())
            if chave not in vistos:
                vistos.add(chave)
                temas_unicos.append(t)

        log(f"\n  {len(temas_unicos)} temas únicos após dedup básico")
        categorias = gerar_taxonomia_via_temas(temas_unicos)

    if not categorias:
        log("\n  ✗ Taxonomia falhou. Verifique o Ollama.")
        sys.exit(1)

    log(f"\n  ✓ {len(categorias)} categorias geradas\n")
    log("  ─── Taxonomia aprovada. Criando pastas agora... ───\n")

    # ── FASE 3: Cria pastas ────────────────────────────────────────────────────
    destino.mkdir(parents=True, exist_ok=True)
    criar_akai_ito(destino)
    criar_lore_geral(destino)

    for i, cat in enumerate(categorias, 1):
        nome = cat.get("nome", "").strip()
        if not nome:
            continue
        pasta = destino / nome
        pasta.mkdir(parents=True, exist_ok=True)
        criar_sobre_md(pasta, cat)

    log(f"\n{'=' * 60}")
    log(f"  Bootstrap concluído — {len(categorias) + 2} pastas criadas")
    log(f"  Cobertura: {total} arquivos analisados (100%)")
    log(f"  Próximo: Classify")
    log("=" * 60)


def main():
    global OLLAMA_MODEL
    parser = argparse.ArgumentParser(description="Nomos Bootstrap")
    parser.add_argument("--origem",  help="Pasta com arquivos brutos")
    parser.add_argument("--destino", help="Pasta de destino organizada")
    parser.add_argument("--modelo",  help="Modelo Ollama a usar")
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
