#!/usr/bin/env python3
"""
Nomos Bootstrap — Taxonomia real a partir de 100% do conteúdo.

Estratégia:
  1. Lê o conteúdo COMPLETO de cada arquivo (sem truncar)
  2. Por lote (limitado pela VRAM), o LLM descreve cada arquivo em 1-2 frases
  3. Com TODAS as descrições acumuladas, o LLM gera a taxonomia final de uma vez
  4. Cria as pastas SOMENTE após a taxonomia estar pronta

Uso:
    python _nomos_bootstrap.py --origem /path --destino /path
"""

import argparse
import json
import re
import requests
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

from _nomos_convert_openai import converter_export_openai

OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "gemma4:e4b"

SISTEMA = {
    "_sobre_.md", "POV.md", "_lidia_rules_compact.md",
    "_lidia_consolidate_rules.md", "_lidia_runner.py",
    "_nomos_consolidate.py", "_nomos_bootstrap.py",
    "_nomos_classify.py", "_nomos_rename.py",
    "_nomos_links.py", "nomos_gui.py"
}


def log(msg: str):
    print(msg, flush=True)


def ler_arquivo(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return ""


def extrair_json(raw: str) -> list:
    # Tenta blocos de código markdown
    if "```" in raw:
        for parte in raw.split("```"):
            parte = parte.strip().lstrip("json").strip()
            try:
                r = json.loads(parte)
                if isinstance(r, list):
                    return r
            except json.JSONDecodeError:
                continue
    # Tenta qualquer array JSON na resposta
    inicio = raw.find("[")
    while inicio >= 0:
        fim = raw.rfind("]") + 1
        if fim > inicio:
            try:
                r = json.loads(raw[inicio:fim])
                if isinstance(r, list) and r:
                    return r
            except Exception:
                pass
        inicio = raw.find("[", inicio + 1)
    # Tenta objeto único {}
    inicio = raw.find("{")
    if inicio >= 0:
        fim = raw.rfind("}") + 1
        if fim > inicio:
            try:
                r = json.loads(raw[inicio:fim])
                if isinstance(r, dict):
                    return [r]
            except Exception:
                pass
    return []


def extrair_descricoes_texto(raw: str, nomes: list[str]) -> list[dict]:
    """Fallback: extrai descrições de texto livre quando o LLM não retornou JSON."""
    resultado = []
    linhas = raw.splitlines()
    for nome in nomes:
        stem = Path(nome).stem.lower()
        for linha in linhas:
            if stem in linha.lower() or nome.lower() in linha.lower():
                # Remove o nome do arquivo do início da linha se presente
                desc = re.sub(rf".*?{re.escape(stem)}[^:]*:\s*", "", linha, flags=re.IGNORECASE).strip()
                desc = re.sub(r"^[-*•\d.)\s]+", "", desc).strip()
                if len(desc) > 15:
                    resultado.append({"nome": nome, "descricao": desc})
                    break
    # Para arquivos sem match, usa a primeira linha não-vazia do bloco
    nomes_encontrados = {d["nome"] for d in resultado}
    for nome in nomes:
        if nome not in nomes_encontrados:
            resultado.append({"nome": nome, "descricao": f"Arquivo: {nome}"})
    return resultado


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


def descrever_lote(arquivos: list[tuple[str, str]], lote_num: int, total_lotes: int) -> list[dict]:
    """
    Recebe o conteúdo completo de N arquivos.
    Retorna lista de {nome, descricao} — 1 a 2 frases por arquivo.
    """
    blocos = []
    for nome, conteudo in arquivos:
        blocos.append(f"=== ARQUIVO: {nome} ===\n{conteudo}\n")
    corpus = "\n".join(blocos)

    prompt = f"""Você está analisando {len(arquivos)} arquivos de um vault pessoal (lote {lote_num}/{total_lotes}).

{corpus}

---

Para CADA arquivo listado acima, escreva UMA descrição de 1 a 2 frases sobre o que ele trata.
Seja específico — capture o assunto real, não generalize.

Responda APENAS com um array JSON. Sem explicações, sem markdown.

[
  {{"nome": "nome-do-arquivo.md", "descricao": "O arquivo trata de..."}}
]"""

    try:
        raw = _chamar_llm(prompt, timeout=180)
        resultado = extrair_json(raw)
        if resultado:
            return resultado
        # Fallback: tenta extrair pares nome/descrição de texto livre
        resultado = extrair_descricoes_texto(raw, [nome for nome, _ in arquivos])
        if resultado:
            return resultado
        log(f"    ⚠ Lote {lote_num}: resposta inválida. Primeiros 300 chars:")
        log(f"    {raw[:300]}")
    except Exception as e:
        log(f"    ⚠ Lote {lote_num}: {e}")
    return []


def gerar_taxonomia(descricoes: list[dict]) -> list[dict]:
    """
    Recebe todas as descrições de todos os arquivos.
    Retorna a taxonomia completa — tantas categorias quanto o conteúdo justificar.
    """
    lista = "\n".join(
        f"- [{d['nome']}] {d.get('descricao', '')}"
        for d in descricoes
    )

    prompt = f"""Você recebeu descrições de {len(descricoes)} arquivos de um vault pessoal.

{lista}

---

Com base no conteúdo REAL descrito acima, crie a taxonomia de pastas para organizar esse vault.

Regras absolutas:
- Crie TANTAS categorias quantas o conteúdo exigir — sem limite de quantidade
- Cada categoria deve representar um tema com volume real de arquivos
- NÃO funda temas distintos: "Saúde" e "Saúde Mental" podem ser separados; "Jogos" e "Entretenimento" são diferentes
- Mescle APENAS quando dois nomes forem literalmente o mesmo assunto
- Nomes de pasta: sem espaços, use hífens (ex: Saude-Mental, Jogos-RPG)

Responda APENAS com um array JSON. Sem explicações, sem markdown.

[
  {{
    "nome": "Nome-Da-Pasta",
    "proposito": "Uma frase curta definindo o que esta pasta representa",
    "pertence": "Tipo de conteúdo que vai aqui",
    "nao_pertence": "O que NÃO vai aqui mesmo parecendo relacionado"
  }}
]"""

    log("\n  ⟳ Gerando taxonomia a partir de todas as descrições...")
    try:
        raw = _chamar_llm(prompt, timeout=300)
        resultado = extrair_json(raw)
        if resultado:
            return resultado
        log("  ⚠ LLM não retornou JSON válido na taxonomia.")
    except Exception as e:
        log(f"  ⚠ Erro na taxonomia: {e}")
    return []


# ── Pastas fixas ───────────────────────────────────────────────────────────────

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


# ── Extração de arquivos compactados ──────────────────────────────────────────

ARCHIVE_EXTS = (".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")


def _is_archive(path: Path) -> bool:
    nome = path.name.lower()
    return any(nome.endswith(ext) for ext in ARCHIVE_EXTS)


def extrair_compactados(origem: Path) -> tuple[list[Path], Path | None]:
    """
    Varre origem buscando .md e arquivos compactados.
    Extrai compactados para um diretório temporário.
    Retorna (lista_de_mds, tmpdir_ou_None).
    """
    mds: list[Path] = [
        f for f in origem.rglob("*.md")
        if not f.name.startswith("_") and f.name not in SISTEMA
    ]

    archives = [f for f in origem.rglob("*") if f.is_file() and _is_archive(f)]

    if not archives:
        return mds, None

    tmpdir = Path(tempfile.mkdtemp(prefix="nomos_extract_"))
    log(f"\n  📦 {len(archives)} arquivo(s) compactado(s) encontrado(s)")

    for arc in archives:
        dest_arc = tmpdir / arc.stem.replace(".tar", "")
        dest_arc.mkdir(parents=True, exist_ok=True)
        try:
            nome_lower = arc.name.lower()
            n_conv = 0
            if nome_lower.endswith(".zip"):
                n_conv = converter_export_openai(arc, dest_arc)
                if n_conv > 0:
                    log(f"    ✓ {arc.name}  →  {n_conv} conversas ChatGPT convertidas para .md")
                else:
                    with zipfile.ZipFile(arc) as zf:
                        zf.extractall(dest_arc)
            elif any(nome_lower.endswith(e) for e in (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")):
                with tarfile.open(arc) as tf:
                    tf.extractall(dest_arc)
            else:
                continue
            extras = [
                f for f in dest_arc.rglob("*.md")
                if not f.name.startswith("_") and f.name not in SISTEMA
            ]
            mds.extend(extras)
            if n_conv == 0:
                log(f"    ✓ {arc.name}  →  {len(extras)} .md extraídos")
        except Exception as e:
            log(f"    ⚠ {arc.name}: {e}")

    return mds, tmpdir


# ── Hardware ───────────────────────────────────────────────────────────────────

def detectar_batch_size() -> int:
    """Quantos arquivos por lote para descrição — limitado pela VRAM disponível."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            timeout=3, text=True
        ).strip().splitlines()[0]
        vram_free_mb = int(out.strip())
        if vram_free_mb >= 8000:
            return 15
        elif vram_free_mb >= 5000:
            return 10
        elif vram_free_mb >= 3000:
            return 6
        else:
            return 4
    except Exception:
        pass
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable"):
                    gb = int(line.split()[1]) / 1024 / 1024
                    return 10 if gb >= 12 else 6 if gb >= 6 else 4
    except Exception:
        pass
    return 6


# ── Pipeline ───────────────────────────────────────────────────────────────────

def run(origem: Path, destino: Path):
    log("=" * 60)
    log("  Nomos Bootstrap — Classificação real e completa")
    log("=" * 60)

    todos, tmpdir = extrair_compactados(origem)

    if not todos:
        log("  ✗ Nenhum arquivo .md encontrado na origem (incluindo compactados).")
        sys.exit(1)

    total = len(todos)
    BATCH = detectar_batch_size()
    lotes = [todos[i:i + BATCH] for i in range(0, total, BATCH)]
    total_lotes = len(lotes)

    log(f"\n  {total} arquivos — {total_lotes} lotes de {BATCH} (detecção de hardware)")
    log(f"\n  Fase 1/2: lendo e descrevendo cada arquivo...\n")

    # ── FASE 1: LLM lê e descreve cada arquivo (conteúdo completo) ────────────
    todas_descricoes: list[dict] = []

    for i, lote in enumerate(lotes, 1):
        log(f"  [{i:02d}/{total_lotes}] Descrevendo {len(lote)} arquivos...")
        arquivos = [(f.name, ler_arquivo(f)) for f in lote]
        descricoes = descrever_lote(arquivos, i, total_lotes)
        todas_descricoes.extend(descricoes)
        log(f"         → {len(descricoes)} descrições  (total: {len(todas_descricoes)})")

    if not todas_descricoes:
        log("\n  ✗ Nenhuma descrição gerada. Verifique o Ollama.")
        sys.exit(1)

    log(f"\n  ✓ {len(todas_descricoes)} arquivos descritos")

    # ── FASE 2: Taxonomia única a partir de todas as descrições ───────────────
    log(f"\n  Fase 2/2: gerando taxonomia final...\n")
    categorias = gerar_taxonomia(todas_descricoes)

    if not categorias:
        log("\n  ✗ Taxonomia falhou. Verifique o Ollama.")
        sys.exit(1)

    log(f"\n  ✓ {len(categorias)} categorias na taxonomia\n")
    log("  ─── Taxonomia aprovada. Criando pastas agora... ───\n")

    # ── FASE 3: Cria pastas ────────────────────────────────────────────────────
    destino.mkdir(parents=True, exist_ok=True)
    criar_akai_ito(destino)
    criar_lore_geral(destino)

    for cat in categorias:
        nome = cat.get("nome", "").strip()
        if not nome:
            continue
        pasta = destino / nome
        pasta.mkdir(parents=True, exist_ok=True)
        criar_sobre_md(pasta, cat)

    if tmpdir and tmpdir.exists():
        shutil.rmtree(tmpdir, ignore_errors=True)

    log(f"\n{'=' * 60}")
    log(f"  Bootstrap concluído — {len(categorias) + 2} pastas criadas")
    log(f"  {total} arquivos analisados (100% do conteúdo)")
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
