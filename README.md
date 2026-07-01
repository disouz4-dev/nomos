# Nomos

> *O espírito grego das leis e da organização de estruturas.*

[Português 🇧🇷](README.pt-br.md) | English

Nomos is a local-first Obsidian vault organizer. It uses local AI models via [Ollama](https://ollama.com) to analyze, classify, and structure your notes — no API keys, no cloud, no cost per token.

---

## How it works

Nomos runs a multi-phase pipeline on your vault:

| Phase | Script | What it does |
|-------|--------|--------------|
| **Bootstrap** | `_lidia_bootstrap.py` | Analyzes **all** your files in batches, generates a taxonomy of folders, then creates the folders only after the full taxonomy is consolidated |
| **Classify** | `_lidia_embed_classify.py` | Embeds every file and every folder description, assigns each file to the most semantically similar folder |
| **Links** | `_lidia_links.py` | Creates hub-and-spoke wiki links per folder — each folder gets a central MOC, files link back to it and to similar peers |
| **Rename** | `_lidia_rename.py` | Renames files with generic names (e.g. "Conversa #123") to descriptive titles |

The batch size for each phase is detected automatically based on your GPU VRAM and available RAM.

---

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) with at least one generation model and `nomic-embed-text`
- NVIDIA GPU recommended (RTX 3050 or better); CPU-only works but is much slower

## Setup

```bash
git clone https://github.com/disouz4-dev/nomos
cd nomos
pip install fastapi uvicorn requests numpy

# (optional) map nomos as a local hostname
sudo sh -c 'echo "127.0.0.1 nomos" >> /etc/hosts'

python nomos_gui.py
# Open http://nomos:8735  (or http://localhost:8735)
```

On first launch, Nomos detects your hardware and guides you through installing Ollama and selecting the best model for your GPU.

---

## Web UI

- **Source / Destino** — pick folders with the built-in file browser
- **Rodar Tudo** — runs all phases sequentially, server-side, with live log output
- **Parar** — cancels the current phase cleanly
- **⚙ Config** — manage pipeline settings and download additional Ollama models
- GPU monitor, folder tree, and live progress displayed on the home screen

---

## Fixed folders

Two folders are always created, regardless of what the LLM generates:

| Folder | Purpose |
|--------|---------|
| `00-Akai-Ito` | Personal relational memories (romantic, affective, intimate) |
| `35-Lore-Geral` | Catch-all for files with no clear category |

---

## License

MIT
