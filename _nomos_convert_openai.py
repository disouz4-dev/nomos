#!/usr/bin/env python3
"""
Nomos — Conversor de export OpenAI/ChatGPT para .md

Detecta exports do ChatGPT (zip contendo conversations-*.json)
e converte cada conversa em um arquivo .md.

Uso interno: chamado pelo bootstrap quando detecta o formato.
"""

import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def _texto_da_mensagem(msg: dict) -> str:
    """Extrai texto de uma mensagem, ignorando tipos não-texto."""
    content = msg.get("content", {})
    if content.get("content_type") != "text":
        return ""
    parts = content.get("parts", [])
    textos = [p for p in parts if isinstance(p, str) and p.strip()]
    return "\n".join(textos).strip()


def _ordenar_mensagens(mapping: dict) -> list[dict]:
    """Ordena mensagens pelo grafo de parent/children (ordem real da conversa)."""
    # Encontra o nó raiz (sem parent ou parent None)
    raiz = None
    for node_id, node in mapping.items():
        if node.get("parent") is None:
            raiz = node_id
            break

    if raiz is None:
        return []

    # BFS pela estrutura de children
    ordem = []
    fila = [raiz]
    while fila:
        atual_id = fila.pop(0)
        node = mapping.get(atual_id, {})
        msg = node.get("message")
        if msg:
            role = msg.get("author", {}).get("role", "")
            texto = _texto_da_mensagem(msg)
            if texto and role in ("user", "assistant", "system"):
                ordem.append({"role": role, "texto": texto})
        for child in node.get("children", []):
            fila.append(child)

    return ordem


def _sanitize_nome(titulo: str) -> str:
    invalidos = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
    nome = invalidos.sub("", titulo).strip()
    return nome[:120] or "Conversa"


def conversa_para_md(conv: dict) -> tuple[str, str]:
    """
    Converte um objeto de conversa para (nome_arquivo.md, conteudo).
    """
    titulo = conv.get("title") or "Conversa sem título"
    ts = conv.get("create_time") or conv.get("update_time") or 0
    try:
        data = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        data = "0000-00-00"

    mapping = conv.get("mapping", {})
    mensagens = _ordenar_mensagens(mapping)

    linhas = [f"# {titulo}", f"\n**Data:** {data}\n", "---\n"]
    for m in mensagens:
        prefixo = "**Você:**" if m["role"] == "user" else "**ChatGPT:**"
        linhas.append(f"{prefixo}\n\n{m['texto']}\n")

    conteudo = "\n".join(linhas)
    nome = f"{data} — {_sanitize_nome(titulo)}.md"
    return nome, conteudo


def _is_openai_zip(inner_zip: zipfile.ZipFile) -> bool:
    """Verifica se o zip interno tem o formato de export do ChatGPT."""
    nomes = inner_zip.namelist()
    return any(n.startswith("conversations-") and n.endswith(".json") for n in nomes)


def converter_export_openai(zip_path: Path, destino: Path) -> int:
    """
    Extrai e converte um export do ChatGPT para .md em `destino`.
    Suporta o formato aninhado (zip dentro de zip) e zip simples.
    Retorna número de arquivos .md criados.
    """
    destino.mkdir(parents=True, exist_ok=True)
    criados = 0

    def processar_inner(inner: zipfile.ZipFile):
        nonlocal criados
        conv_files = [n for n in inner.namelist()
                      if n.startswith("conversations-") and n.endswith(".json")]
        for cf in conv_files:
            try:
                data = json.loads(inner.read(cf))
            except Exception:
                continue
            for conv in data:
                try:
                    nome, conteudo = conversa_para_md(conv)
                    # Evita colisão de nomes
                    path = destino / nome
                    if path.exists():
                        stem = path.stem
                        path = destino / f"{stem} ({conv.get('id','')[:6]}).md"
                    path.write_text(conteudo, encoding="utf-8")
                    criados += 1
                except Exception:
                    pass

    try:
        with zipfile.ZipFile(zip_path) as outer:
            # Verifica se é zip simples com conversations-*.json
            if _is_openai_zip(outer):
                processar_inner(outer)
                return criados

            # Formato aninhado: procura zip de conversas dentro
            for member in outer.namelist():
                if "Conversation" in member and member.endswith(".zip"):
                    with outer.open(member) as f:
                        raw = f.read()
                    try:
                        with zipfile.ZipFile(io.BytesIO(raw)) as inner:
                            if _is_openai_zip(inner):
                                processar_inner(inner)
                    except Exception:
                        pass
    except Exception as e:
        print(f"  ⚠ Erro ao converter {zip_path.name}: {e}", flush=True)

    return criados


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Uso: python _nomos_convert_openai.py export.zip /destino/")
        sys.exit(1)
    n = converter_export_openai(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"  ✓ {n} conversas convertidas")
