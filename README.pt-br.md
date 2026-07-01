# Nomos

> *O espírito grego das leis e da organização de estruturas.*

[English 🇺🇸](README.md) | Português

Nomos é um organizador de vault Obsidian local-first. Usa modelos de IA locais via [Ollama](https://ollama.com) para analisar, classificar e estruturar suas notas — sem chaves de API, sem nuvem, sem custo por token.

---

## Como funciona

O Nomos executa um pipeline multi-fase no seu vault:

| Fase | Script | O que faz |
|------|--------|-----------|
| **Bootstrap** | `_lidia_bootstrap.py` | Analisa **todos** os arquivos em lotes, gera uma taxonomia de pastas, e só cria as pastas após a taxonomia completa ser consolidada |
| **Classify** | `_lidia_embed_classify.py` | Embedda cada arquivo e cada descrição de pasta, atribui cada arquivo à pasta semanticamente mais similar |
| **Links** | `_lidia_links.py` | Cria wiki links hub-and-spoke por pasta — cada pasta ganha um MOC central, os arquivos linkam de volta para ele e para arquivos similares |
| **Rename** | `_lidia_rename.py` | Renomeia arquivos com nomes genéricos (ex: "Conversa #123") para títulos descritivos |

O tamanho do lote é detectado automaticamente com base na VRAM da GPU e RAM disponível.

---

## Requisitos

- Python 3.11+
- [Ollama](https://ollama.com) com pelo menos um modelo de geração e `nomic-embed-text`
- GPU NVIDIA recomendada (RTX 3050 ou melhor); CPU funciona mas é bem mais lento

## Instalação

```bash
git clone https://github.com/disouz4-dev/nomos
cd nomos
pip install fastapi uvicorn requests numpy

# (opcional) mapear nomos como hostname local
sudo sh -c 'echo "127.0.0.1 nomos" >> /etc/hosts'

python nomos_gui.py
# Abra http://nomos:8735  (ou http://localhost:8735)
```

Na primeira abertura, o Nomos detecta o hardware e guia a instalação do Ollama e seleção do melhor modelo para sua GPU.

---

## Interface web

- **Source / Destino** — selecione pastas pelo explorador de arquivos integrado
- **Rodar Tudo** — executa todas as fases em sequência, no servidor, com log ao vivo
- **Parar** — cancela a fase atual de forma limpa
- **⚙ Config** — configure o pipeline e baixe modelos Ollama adicionais
- Monitor de GPU, árvore de pastas e progresso em tempo real na tela principal

---

## Pastas fixas

Duas pastas são sempre criadas, independente do que o LLM sugerir:

| Pasta | Propósito |
|-------|-----------|
| `00-Akai-Ito` | Memórias relacionais pessoais (românticas, afetivas, íntimas) |
| `35-Lore-Geral` | Catch-all para arquivos sem categoria clara |

---

## Licença

MIT
