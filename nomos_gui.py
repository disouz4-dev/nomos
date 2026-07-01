#!/usr/bin/env python3
"""
Nomos — Interface web para o pipeline de organização do vault.

Uso:
    python nomos_gui.py
    Acesse: http://localhost:8765
"""

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
import uvicorn

POV_PATH = Path(__file__).parent
DB_PATH  = POV_PATH / "_nomos_state.db"
PORT     = 8735

# Padrões de origem/destino padrão
DEFAULT_ORIGEM  = str(Path.home() / "Lídia Memory")
DEFAULT_DESTINO = str(POV_PATH.parent)

app = FastAPI()

# ── Estado global de processo ─────────────────────────────────────────────────

_processo_ativo: subprocess.Popen | None = None
_fase_ativa: str = ""
_cancelar: bool = False


# ── DB helpers ────────────────────────────────────────────────────────────────

def db_stats() -> dict:
    if not DB_PATH.exists():
        return {"total": 0, "concluido": 0, "pendente": 0,
                "duplicata": 0, "erro": 0, "em_progresso": 0}
    try:
        conn = sqlite3.connect(DB_PATH)
        cur  = conn.cursor()
        cur.execute("SELECT status, COUNT(*) FROM arquivos GROUP BY status")
        rows = {r[0]: r[1] for r in cur.fetchall()}
        conn.close()
        return {
            "total":       sum(rows.values()),
            "concluido":   rows.get("concluido", 0),
            "pendente":    rows.get("pendente", 0),
            "duplicata":   rows.get("duplicata", 0),
            "erro":        rows.get("erro", 0),
            "em_progresso": rows.get("em_progresso", 0),
        }
    except Exception:
        return {"total": 0, "concluido": 0, "pendente": 0,
                "duplicata": 0, "erro": 0, "em_progresso": 0}


def vault_tree(destino: str) -> list[dict]:
    """Retorna estrutura de pastas com contagem de arquivos."""
    dest = Path(destino)
    if not dest.exists():
        return []
    result = []
    for pasta in sorted(dest.iterdir()):
        if not pasta.is_dir() or pasta.name.startswith(".") or pasta.name == "POV":
            continue
        mds = list(pasta.rglob("*.md"))
        sobres = [f for f in mds if f.name == "_sobre_.md"]
        arquivos = [f for f in mds if f.name != "_sobre_.md" and not f.name.startswith("_")]
        result.append({
            "nome": pasta.name,
            "arquivos": len(arquivos),
            "tem_sobre": len(sobres) > 0
        })
    return result


# ── SSE: stream de processo ───────────────────────────────────────────────────

async def stream_processo(cmd: list[str], env_vars: dict = None) -> AsyncGenerator[str, None]:
    global _processo_ativo, _fase_ativa

    if _processo_ativo and _processo_ativo.poll() is None:
        yield f"data: ⚠ Já existe um processo rodando. Aguarde.\n\n"
        return

    import os
    env = os.environ.copy()
    if env_vars:
        env.update(env_vars)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env
        )
        _processo_ativo = proc

        loop = asyncio.get_event_loop()

        while True:
            line = await loop.run_in_executor(None, proc.stdout.readline)
            if not line and proc.poll() is not None:
                break
            if line:
                escaped = line.rstrip().replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                yield f"data: {escaped}\n\n"
            await asyncio.sleep(0.01)

        code = proc.wait()
        yield f"data: \n\n"
        yield f"data: {'═' * 50}\n\n"
        yield f"data: ✅ Processo encerrado (código {code})\n\n"
        yield f"data: [DONE]\n\n"

    except Exception as e:
        yield f"data: ✗ Erro: {e}\n\n"
        yield f"data: [DONE]\n\n"
    finally:
        _processo_ativo = None
        _fase_ativa = ""


# ── Rotas ─────────────────────────────────────────────────────────────────────

@app.get("/api/gpu")
def api_gpu():
    """Lê dados da GPU via nvidia-smi ou radeontop."""
    import subprocess as sp, re

    # NVIDIA
    try:
        out = sp.check_output(
            ["nvidia-smi",
             "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            timeout=3, stderr=sp.DEVNULL, text=True
        ).strip()
        parts = [p.strip() for p in out.split(",")]
        return JSONResponse({
            "vendor":       "nvidia",
            "name":         parts[0],
            "util_pct":     int(parts[1]),
            "temp_c":       int(parts[2]),
            "vram_used_mb": int(parts[3]),
            "vram_total_mb": int(parts[4]),
        })
    except Exception:
        pass

    # AMD (radeontop)
    try:
        out = sp.check_output(
            ["radeontop", "-d", "-", "-l", "1"],
            timeout=4, stderr=sp.DEVNULL, text=True
        )
        gpu_m = re.search(r"gpu\s+([\d.]+)%", out)
        vram_m = re.search(r"vram\s+([\d.]+)%\s+([\d.]+)mb of\s+([\d.]+)mb", out)
        util = float(gpu_m.group(1)) if gpu_m else 0
        vused = int(float(vram_m.group(2))) if vram_m else None
        vtotal = int(float(vram_m.group(3))) if vram_m else None
        return JSONResponse({
            "vendor":       "amd",
            "name":         "AMD GPU",
            "util_pct":     int(util),
            "temp_c":       None,
            "vram_used_mb": vused,
            "vram_total_mb": vtotal,
        })
    except Exception:
        pass

    # Intel / fallback via /sys
    try:
        freq = Path("/sys/class/drm/card0/gt_cur_freq_mhz").read_text().strip()
        return JSONResponse({"vendor": "intel", "name": "Intel GPU",
                             "util_pct": None, "freq_mhz": int(freq),
                             "temp_c": None, "vram_used_mb": None, "vram_total_mb": None})
    except Exception:
        pass

    return JSONResponse({"error": "GPU não detectada"})


# ── Catálogo de modelos ───────────────────────────────────────────────────────

MODELOS_CATALOGO = [
    {"id": "gemma3:12b",       "nome": "Gemma 3 12B",       "tam": "7.3 GB", "vram_min": 8000, "embed": False},
    {"id": "llama3.1:8b",      "nome": "Llama 3.1 8B",      "tam": "4.7 GB", "vram_min": 7000, "embed": False},
    {"id": "mistral:7b",       "nome": "Mistral 7B",        "tam": "4.1 GB", "vram_min": 6000, "embed": False},
    {"id": "gemma3:4b",        "nome": "Gemma 3 4B",        "tam": "2.5 GB", "vram_min": 4000, "embed": False},
    {"id": "qwen3.5:4b",       "nome": "Qwen 3.5 4B",       "tam": "2.6 GB", "vram_min": 4000, "embed": False},
    {"id": "phi4-mini",        "nome": "Phi 4 Mini 3.8B",   "tam": "2.5 GB", "vram_min": 3500, "embed": False},
    {"id": "gemma4:e4b",       "nome": "Gemma 4 E4B",       "tam": "2.2 GB", "vram_min": 3000, "embed": False},
    {"id": "llama3.2:3b",      "nome": "Llama 3.2 3B",      "tam": "2.0 GB", "vram_min": 3000, "embed": False},
    {"id": "qwen2.5:3b",       "nome": "Qwen 2.5 3B",       "tam": "2.0 GB", "vram_min": 3000, "embed": False},
    {"id": "llama3.2:1b",      "nome": "Llama 3.2 1B",      "tam": "1.3 GB", "vram_min": 2000, "embed": False},
    {"id": "nomic-embed-text", "nome": "Nomic Embed Text",  "tam": "274 MB", "vram_min": 0,    "embed": True},
]


def _hardware_info() -> dict:
    import subprocess as sp, re
    info: dict = {"gpu": None, "vram_total_mb": None, "ram_gb": None, "cpu_cores": None}

    # CPU
    try:
        info["cpu_cores"] = os.cpu_count()
    except Exception:
        pass

    # RAM
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal"):
                    info["ram_gb"] = round(int(line.split()[1]) / 1024 / 1024, 1)
                    break
    except Exception:
        pass

    # GPU NVIDIA
    try:
        out = sp.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            timeout=3, stderr=sp.DEVNULL, text=True
        ).strip().splitlines()[0]
        parts = [p.strip() for p in out.split(",")]
        info["gpu"]          = parts[0]
        info["vram_total_mb"] = int(parts[1])
        info["gpu_vendor"]   = "nvidia"
        return info
    except Exception:
        pass

    # GPU AMD
    try:
        out = sp.check_output(["radeontop", "-d", "-", "-l", "1"], timeout=4,
                               stderr=sp.DEVNULL, text=True)
        m = re.search(r"vram\s+[\d.]+%\s+[\d.]+mb of\s+([\d.]+)mb", out)
        if m:
            info["gpu"]          = "AMD GPU"
            info["vram_total_mb"] = int(float(m.group(1)))
            info["gpu_vendor"]   = "amd"
    except Exception:
        pass

    return info


def _recomendar_modelo(vram_mb: int | None) -> str:
    if vram_mb is None:
        return "llama3.2:3b"
    if vram_mb >= 8000:
        return "gemma3:12b"
    if vram_mb >= 7000:
        return "llama3.1:8b"
    if vram_mb >= 4000:
        return "gemma3:4b"
    if vram_mb >= 3000:
        return "gemma4:e4b"
    return "llama3.2:1b"


@app.get("/api/hardware")
def api_hardware():
    hw = _hardware_info()
    rec = _recomendar_modelo(hw.get("vram_total_mb"))
    # Adiciona flag recommended em cada modelo do catálogo
    catalogo = []
    for m in MODELOS_CATALOGO:
        vram = hw.get("vram_total_mb") or 0
        compativel = m["vram_min"] <= vram or m["embed"]
        catalogo.append({**m, "recomendado": m["id"] == rec, "compativel": compativel})
    return JSONResponse({**hw, "recomendado": rec, "catalogo": catalogo})


@app.get("/api/ollama-status")
def api_ollama_status():
    import subprocess as sp
    try:
        sp.check_output(["which", "ollama"], stderr=sp.DEVNULL)
        instalado = True
    except Exception:
        instalado = False

    modelos_instalados: list[str] = []
    if instalado:
        try:
            out = sp.check_output(["ollama", "list"], stderr=sp.DEVNULL, text=True, timeout=5)
            for line in out.splitlines()[1:]:
                parts = line.split()
                if parts:
                    modelos_instalados.append(parts[0])
        except Exception:
            pass

    return JSONResponse({"instalado": instalado, "modelos": modelos_instalados})


@app.get("/api/instalar-ollama")
async def api_instalar_ollama():
    async def gen():
        yield "data: Baixando e instalando Ollama...\n\n"
        proc = subprocess.Popen(
            ["bash", "-c", "curl -fsSL https://ollama.com/install.sh | sh"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, proc.stdout.readline)
            if not line and proc.poll() is not None:
                break
            if line:
                yield f"data: {line.rstrip()}\n\n"
        code = proc.wait()
        if code == 0:
            yield "data: ✅ Ollama instalado com sucesso!\n\n"
        else:
            yield "data: ✗ Erro na instalação (código {code})\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/pull-model")
async def api_pull_model(model: str):
    async def gen():
        yield f"data: Baixando {model}...\n\n"
        proc = subprocess.Popen(
            ["ollama", "pull", model],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0
        )
        loop = asyncio.get_event_loop()
        buf = b""
        last_progress = ""
        while True:
            chunk = await loop.run_in_executor(None, proc.stdout.read, 256)
            if not chunk:
                if proc.poll() is not None:
                    break
                continue
            buf += chunk
            # Divide em linhas respeitando \r e \n
            while b"\n" in buf or b"\r" in buf:
                for sep in (b"\n", b"\r"):
                    idx = buf.find(sep)
                    if idx == -1:
                        continue
                    line = buf[:idx].decode("utf-8", errors="replace").strip()
                    buf = buf[idx + 1:]
                    if not line:
                        continue
                    # Linhas de progresso (contêm %) → evento especial "progress"
                    if "%" in line or "pulling" in line.lower() or "verifying" in line.lower():
                        if line != last_progress:
                            last_progress = line
                            yield f"event: progress\ndata: {line}\n\n"
                    else:
                        yield f"data: {line}\n\n"
                    break
        code = proc.wait()
        if code == 0:
            yield f"data: ✅ {model} pronto!\n\n"
        else:
            yield f"data: ✗ Erro ao baixar {model} (código {code})\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/reset-db")
def api_reset_db():
    """Apaga o banco de dados do pipeline anterior."""
    try:
        if DB_PATH.exists():
            DB_PATH.unlink()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


SISTEMA_NOMES = {
    "POV.md", "_lidia_rules_compact.md", "nomos_gui.py",
    "README.md", "README.pt-br.md",
}
SISTEMA_DIRS  = {"POV", ".git", ".obsidian", ".trash"}
ARCHIVE_EXTS  = (".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")


@app.get("/api/scan")
def api_scan(origem: str = DEFAULT_ORIGEM, destino: str = DEFAULT_DESTINO):
    """Conta .md e arquivos compactados no filesystem."""
    def contar_origem(pasta: str) -> dict:
        p = Path(pasta).expanduser().resolve()
        if not p.exists():
            return {"mds": 0, "archives": 0}
        mds = 0
        archives = 0
        for f in p.rglob("*"):
            if any(part in SISTEMA_DIRS for part in f.parts):
                continue
            if f.is_file():
                nome = f.name.lower()
                if nome.endswith(".md") and not f.name.startswith("_") and f.name not in SISTEMA_NOMES:
                    mds += 1
                elif any(nome.endswith(e) for e in ARCHIVE_EXTS):
                    archives += 1
        return {"mds": mds, "archives": archives}

    def contar_destino(pasta: str) -> int:
        p = Path(pasta).expanduser().resolve()
        if not p.exists():
            return 0
        total = 0
        for f in p.rglob("*.md"):
            if any(part in SISTEMA_DIRS for part in f.parts):
                continue
            if f.name.startswith("_") or f.name in SISTEMA_NOMES:
                continue
            total += 1
        return total

    orig = contar_origem(origem)
    return JSONResponse({
        "origem_total":    orig["mds"] + orig["archives"],
        "origem_mds":      orig["mds"],
        "origem_archives": orig["archives"],
        "destino_total":   contar_destino(destino),
    })


@app.get("/api/browse")
def api_browse(path: str = "~"):
    try:
        base = Path(path).expanduser().resolve()
        if not base.is_dir():
            base = base.parent
        entries = []
        parent = str(base.parent) if base != base.parent else None
        try:
            for item in sorted(base.iterdir()):
                if item.is_dir() and not item.name.startswith("."):
                    entries.append(item.name)
        except PermissionError:
            pass
        return JSONResponse({"current": str(base), "parent": parent, "dirs": entries})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/stats")
def api_stats():
    return JSONResponse(db_stats())


@app.get("/api/tree")
def api_tree(destino: str = DEFAULT_DESTINO):
    return JSONResponse(vault_tree(destino))


@app.get("/api/status")
def api_status():
    rodando = _processo_ativo is not None and _processo_ativo.poll() is None
    return JSONResponse({"rodando": rodando, "fase": _fase_ativa})


@app.post("/api/parar")
def api_parar():
    global _processo_ativo, _cancelar, _fase_ativa
    _cancelar = True          # para o loop de _stream_tudo
    _fase_ativa = ""
    if _processo_ativo and _processo_ativo.poll() is None:
        _processo_ativo.terminate()
        return JSONResponse({"ok": True, "msg": "Pipeline cancelado."})
    return JSONResponse({"ok": True, "msg": "Cancelado (nenhum subprocesso ativo no momento)."})


@app.get("/api/run/bootstrap")
async def run_bootstrap(origem: str = DEFAULT_ORIGEM, destino: str = DEFAULT_DESTINO,
                        modelo: str = ""):
    global _fase_ativa
    _fase_ativa = "bootstrap"
    cmd = [sys.executable, str(POV_PATH / "_nomos_bootstrap.py"),
           "--origem", origem, "--destino", destino]
    if modelo:
        cmd += ["--modelo", modelo]
    return StreamingResponse(stream_processo(cmd),
                             media_type="text/event-stream")


@app.get("/api/run/classify")
async def run_classify(origem: str = DEFAULT_ORIGEM, destino: str = DEFAULT_DESTINO,
                       lote: int = 100):
    global _fase_ativa
    _fase_ativa = "classify"
    cmd = [sys.executable, str(POV_PATH / "_nomos_classify.py"),
           "--origem", origem, "--destino", destino, "--lote", str(lote)]
    return StreamingResponse(stream_processo(cmd),
                             media_type="text/event-stream")


@app.get("/api/run/rename")
async def run_rename(destino: str = DEFAULT_DESTINO, lote: int = 20, modelo: str = ""):
    global _fase_ativa
    _fase_ativa = "rename"
    cmd = [sys.executable, str(POV_PATH / "_nomos_rename.py"),
           "--destino", destino, "--lote", str(lote)]
    if modelo:
        cmd += ["--modelo", modelo]
    return StreamingResponse(stream_processo(cmd),
                             media_type="text/event-stream")


@app.get("/api/run/links")
async def run_links(destino: str = DEFAULT_DESTINO,
                    threshold: float = 0.75, max_links: int = 5):
    global _fase_ativa
    _fase_ativa = "links"
    cmd = [sys.executable, str(POV_PATH / "_nomos_links.py"),
           "--destino", destino,
           "--threshold", str(threshold),
           "--max-links", str(max_links)]
    return StreamingResponse(stream_processo(cmd), media_type="text/event-stream")


@app.get("/api/run/consolidate")
async def run_consolidate(origem: str = DEFAULT_DESTINO, destino: str = DEFAULT_DESTINO,
                          lote: int = 20):
    global _fase_ativa
    _fase_ativa = "consolidate"
    cmd = [sys.executable, str(POV_PATH / "_nomos_consolidate.py"),
           "--so-consolidar", "--limite", str(lote)]
    return StreamingResponse(stream_processo(cmd),
                             media_type="text/event-stream")


async def _stream_cmd(cmd: list[str]) -> AsyncGenerator[str, None]:
    """Roda um subprocesso e streama stdout linha a linha.
    Termina imediatamente quando _cancelar=True (terminate unbloca o readline)."""
    global _processo_ativo
    import os, queue, threading

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=os.environ.copy()
    )
    _processo_ativo = proc

    # Thread lê linhas e coloca na fila — readline desbloquia quando proc.terminate()
    q: queue.Queue = queue.Queue()

    def _reader():
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception:
            pass
        finally:
            q.put(None)  # sentinela

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    loop = asyncio.get_event_loop()
    try:
        while True:
            # Checa _cancelar antes de pegar a próxima linha
            if _cancelar:
                proc.terminate()
                break

            try:
                line = await loop.run_in_executor(None, lambda: q.get(timeout=0.3))
            except queue.Empty:
                continue  # timeout curto — volta para checar _cancelar

            if line is None:  # sentinela: processo terminou
                break

            esc = line.rstrip().replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
            yield f"data: {esc}\n\n"

    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        _processo_ativo = None


async def _stream_tudo(origem: str, destino: str,
                       lote_classify: int, lote_rename: int,
                       com_rename: bool, modelo: str = "") -> AsyncGenerator[str, None]:
    global _fase_ativa, _cancelar

    if _processo_ativo and _processo_ativo.poll() is None:
        yield "data: ⚠ Já existe um processo rodando.\n\n"
        yield "data: [DONE]\n\n"
        return

    _cancelar = False

    def emit(msg: str) -> str:
        return f"data: {msg}\n\n"

    def cancelado() -> bool:
        return _cancelar

    # ── Pré-voo: verificar SOURCE ─────────────────────────────────────────────
    origem_path = Path(origem)
    if not origem_path.exists():
        yield emit(f"✗ SOURCE não encontrado: {origem}")
        yield emit("  Configure o caminho correto antes de rodar.")
        yield "data: [DONE]\n\n"
        return
    mds_origem = [f for f in origem_path.rglob("*.md")
                  if not f.name.startswith("_") and f.name not in {
                      "POV.md", "_lidia_rules_compact.md"}]
    archives_origem = [f for f in origem_path.rglob("*")
                       if f.is_file() and any(
                           f.name.lower().endswith(e)
                           for e in (".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar"))]
    if not mds_origem and not archives_origem:
        yield emit(f"✗ SOURCE vazio: {origem}")
        yield emit("  Precisa ter arquivos .md ou compactados (.zip, .tar.gz).")
        yield "data: [DONE]\n\n"
        return
    resumo = []
    if mds_origem:    resumo.append(f"{len(mds_origem)} .md")
    if archives_origem: resumo.append(f"{len(archives_origem)} arquivo(s) compactado(s)")
    yield emit(f"  ✓ SOURCE: {', '.join(resumo)}")
    yield emit("")

    dest_path = Path(destino)

    # ── Fase 1: Bootstrap ─────────────────────────────────────────────────────
    tem_sobres = [f for f in dest_path.rglob("_sobre_.md")
                  if "POV" not in f.parts]
    if not tem_sobres:
        _fase_ativa = "bootstrap"
        yield emit("═" * 50)
        yield emit("▶ FASE 1 — Bootstrap")
        yield emit("═" * 50)
        cmd = [sys.executable, str(POV_PATH / "_nomos_bootstrap.py"),
               "--origem", origem, "--destino", destino]
        if modelo:
            cmd += ["--modelo", modelo]
        import re as _re_bs
        _bs_prog = _re_bs.compile(r"\[(\d+)/(\d+)\]")
        async for chunk in _stream_cmd(cmd):
            yield chunk
            # Emite progresso ao detectar [XX/YY]
            if chunk.startswith("data: "):
                m = _bs_prog.search(chunk)
                if m:
                    pct = round(int(m.group(1)) / max(int(m.group(2)), 1) * 100)
                    yield f"event: fase-prog\ndata: bootstrap:{pct}\n\n"
        if cancelado():
            yield emit("⏹ Pipeline cancelado pelo usuário.")
            yield "data: [DONE]\n\n"
            return
        yield emit("")
    else:
        yield emit(f"⟳ Bootstrap pulado — {len(tem_sobres)} _sobre_.md já existem")
        yield emit("")

    # ── Fase 2: Classify (loop) ───────────────────────────────────────────────
    _fase_ativa = "classify"
    yield emit("═" * 50)
    yield emit("▶ FASE 2 — Embed + Classify (loop até zerar pendentes)")
    yield emit("═" * 50)

    rodada = 1
    primeira_rodada = True
    _total_pend_cl = 0
    while not cancelado():
        stats_cl = db_stats()
        pendentes = stats_cl.get("pendente", 0)
        total_cl  = stats_cl.get("total", 0)
        # Sai apenas após pelo menos uma rodada e sem pendentes
        if pendentes == 0 and not primeira_rodada:
            yield emit("✅ Classify concluído — sem pendentes.")
            yield f"event: fase-prog\ndata: classify:100\n\n"
            break
        if _total_pend_cl == 0 and total_cl > 0:
            _total_pend_cl = total_cl
        if _total_pend_cl > 0:
            concl = _total_pend_cl - pendentes
            pct_cl = round(concl / _total_pend_cl * 100)
            yield f"event: fase-prog\ndata: classify:{pct_cl}\n\n"
        yield emit(f"  Rodada {rodada} — {pendentes} pendentes")
        cmd = [sys.executable, str(POV_PATH / "_nomos_classify.py"),
               "--origem", origem, "--destino", destino, "--lote", str(lote_classify)]
        async for chunk in _stream_cmd(cmd):
            yield chunk
        primeira_rodada = False
        rodada += 1
        await asyncio.sleep(0.5)

    if cancelado():
        yield emit("⏹ Pipeline cancelado pelo usuário.")
        yield "data: [DONE]\n\n"
        return

    yield emit("")

    # ── Fase 3: Links ─────────────────────────────────────────────────────────
    _fase_ativa = "links"
    yield emit("═" * 50)
    yield emit("▶ FASE 3 — Wiki Links (por pasta)")
    yield emit("═" * 50)
    cmd = [sys.executable, str(POV_PATH / "_nomos_links.py"),
           "--destino", destino]
    async for chunk in _stream_cmd(cmd):
        yield chunk
    if cancelado():
        yield emit("⏹ Pipeline cancelado pelo usuário.")
        yield "data: [DONE]\n\n"
        return
    yield emit("")

    # ── Fase 4: Rename (opcional, loop) ───────────────────────────────────────
    if com_rename:
        _fase_ativa = "rename"
        yield emit("═" * 50)
        yield emit("▶ FASE 4 — Rename (loop até zerar nomes genéricos)")
        yield emit("═" * 50)

        import re as _re
        GENERICO = _re.compile(
            r"Conversa\s*#?\d*|sem t[ií]tulo|Help request|\(\d+\)", _re.IGNORECASE
        )

        rodada = 1
        while not cancelado():
            genericos = [
                f for f in dest_path.rglob("*.md")
                if not f.name.startswith("_") and GENERICO.search(f.name)
            ]
            if not genericos:
                yield emit("✅ Rename concluído — sem nomes genéricos.")
                break
            yield emit(f"  Rodada {rodada} — {len(genericos)} arquivos com nome genérico")
            cmd = [sys.executable, str(POV_PATH / "_nomos_rename.py"),
                   "--destino", destino, "--lote", str(lote_rename)]
            if modelo:
                cmd += ["--modelo", modelo]
            async for chunk in _stream_cmd(cmd):
                yield chunk
            rodada += 1
            await asyncio.sleep(0.5)

        if cancelado():
            yield emit("⏹ Pipeline cancelado na fase Rename.")
            yield "data: [DONE]\n\n"
            return
        yield emit("")
    else:
        yield emit("ℹ  Rename pulado")
        yield emit("")

    _fase_ativa = ""
    yield emit("═" * 50)
    yield emit("✅ Pipeline completo!")
    yield emit("═" * 50)
    yield "data: [DONE]\n\n"


@app.get("/api/run/tudo")
async def run_tudo(
    origem: str = DEFAULT_ORIGEM,
    destino: str = DEFAULT_DESTINO,
    lote_classify: int = 100,
    lote_rename: int = 20,
    com_rename: bool = True,
    modelo: str = ""
):
    return StreamingResponse(
        _stream_tudo(origem, destino, lote_classify, lote_rename, com_rename, modelo),
        media_type="text/event-stream"
    )


# ── HTML ─────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nomos</title>
<style>
  :root {
    --bg:       #0d1117;
    --surface:  #161b22;
    --border:   #30363d;
    --accent:   #7c5cbf;
    --accent2:  #58a6ff;
    --green:    #3fb950;
    --yellow:   #d29922;
    --red:      #f85149;
    --text:     #e6edf3;
    --muted:    #8b949e;
    --radius:   10px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; min-height: 100vh; }

  /* ── Layout ── */
  .app { display: grid; grid-template-columns: 280px 1fr; grid-template-rows: 60px 1fr; height: 100vh; }
  .header { grid-column: 1/-1; background: var(--surface); border-bottom: 1px solid var(--border);
            display: flex; align-items: center; padding: 0 24px; gap: 12px; }
  .header h1 { font-size: 1.1rem; font-weight: 600; letter-spacing: .5px; }
  .header .tag { background: var(--accent); color: #fff; font-size: .7rem; padding: 2px 8px; border-radius: 20px; }
  .header .spacer { flex: 1; }
  .header .status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  .header .status-dot.ativo { background: var(--green); animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

  .sidebar { background: var(--surface); border-right: 1px solid var(--border);
             overflow-y: auto; padding: 16px 0; }
  .main { display: flex; flex-direction: column; overflow: hidden; }

  /* ── Stats bar ── */
  .stats { display: grid; grid-template-columns: repeat(5, 1fr); gap: 1px;
           background: var(--border); border-bottom: 1px solid var(--border); }
  .stat { background: var(--surface); padding: 14px 16px; text-align: center; }
  .stat .n { font-size: 1.5rem; font-weight: 700; line-height: 1; }
  .stat .l { font-size: .7rem; color: var(--muted); margin-top: 3px; text-transform: uppercase; letter-spacing: .5px; }
  .stat.ok .n    { color: var(--green); }
  .stat.pend .n  { color: var(--accent2); }
  .stat.dup .n   { color: var(--muted); }
  .stat.err .n   { color: var(--red); }
  .stat.tot .n   { color: var(--text); }

  /* ── Pipeline ── */
  .pipeline { padding: 20px; display: flex; flex-direction: column; gap: 10px; flex-shrink: 0; }
  .phase { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
           padding: 14px 16px; display: flex; align-items: center; gap: 14px; cursor: default; }
  .phase .num { width: 28px; height: 28px; border-radius: 50%; background: var(--border);
                display: flex; align-items: center; justify-content: center;
                font-size: .8rem; font-weight: 700; color: var(--muted); flex-shrink: 0; }
  .phase.ready .num { background: var(--accent); color: #fff; }
  .phase.running .num { background: var(--yellow); color: #000; animation: pulse 1s infinite; }
  .phase.done .num { background: var(--green); color: #000; }
  .phase .info { flex: 1; }
  .phase .info h3 { font-size: .9rem; font-weight: 600; }
  .phase .info p  { font-size: .75rem; color: var(--muted); margin-top: 2px; }
  .btn { padding: 6px 14px; border-radius: 6px; border: 1px solid var(--border); cursor: pointer;
         font-size: .8rem; font-weight: 600; background: transparent; color: var(--text);
         transition: all .15s; white-space: nowrap; }
  .btn:hover { background: var(--accent); border-color: var(--accent); color: #fff; }
  .btn.danger:hover { background: var(--red); border-color: var(--red); }
  .btn:disabled { opacity: .4; cursor: not-allowed; }
  .btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  .btn.primary:hover { opacity: .85; }

  /* ── Log ── */
  .log-section { flex: 1; display: flex; flex-direction: column; overflow: hidden; padding: 0 20px 20px; }
  .log-header { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .log-header h3 { font-size: .85rem; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; flex: 1; }
  .log { flex: 1; background: #010409; border: 1px solid var(--border); border-radius: var(--radius);
         overflow-y: auto; padding: 14px 16px; font-family: 'JetBrains Mono', 'Fira Code', monospace;
         font-size: .78rem; line-height: 1.6; }
  .log .line { white-space: pre-wrap; word-break: break-all; }
  .log .ok   { color: var(--green); }
  .log .err  { color: var(--red); }
  .log .info { color: var(--accent2); }
  .log .sep  { color: var(--border); }
  .log .warn { color: var(--yellow); }

  /* ── Sidebar: pasta tree ── */
  .sidebar-title { font-size: .7rem; text-transform: uppercase; letter-spacing: .8px;
                   color: var(--muted); padding: 0 16px 8px; }
  .folder-item { padding: 7px 16px; display: flex; align-items: center; gap: 8px;
                 border-left: 2px solid transparent; font-size: .8rem; }
  .folder-item:hover { background: rgba(255,255,255,.03); }
  .folder-item .folder-icon { font-size: .9rem; flex-shrink: 0; }
  .folder-item .folder-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .folder-item .folder-count { font-size: .7rem; color: var(--muted); background: var(--border);
                                padding: 1px 6px; border-radius: 10px; }
  .folder-item.lore { border-left-color: var(--yellow); }
  .folder-item.has-sobre { border-left-color: var(--accent); }

  /* ── Config / Setup modal ── */
  .modal-bg { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.75);
              z-index: 100; align-items: center; justify-content: center; }
  .modal-bg.open { display: flex; }
  .modal { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
           padding: 28px; width: 520px; max-width: 95vw; }
  .modal h2 { font-size: 1.05rem; margin-bottom: 6px; }
  .modal .subtitle { font-size: .8rem; color: var(--muted); margin-bottom: 20px; }
  .form-group { margin-bottom: 16px; }
  .form-group label { display: block; font-size: .8rem; color: var(--muted); margin-bottom: 5px;
                      text-transform: uppercase; letter-spacing: .4px; }
  .form-group input { width: 100%; padding: 9px 12px; background: var(--bg);
                      border: 1px solid var(--border); border-radius: 6px;
                      color: var(--text); font-size: .85rem; font-family: monospace; }
  .form-group input:focus { outline: none; border-color: var(--accent); }
  .form-group .hint { font-size: .72rem; color: var(--muted); margin-top: 4px; }
  .modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 22px; }

  /* ── Setup overlay (primeira vez) ── */
  /* ── Nomos Setup Overlay ── */
  #nomosSetup { display: none; position: fixed; inset: 0; background: var(--bg);
                z-index: 400; align-items: center; justify-content: center; overflow-y: auto; }
  #nomosSetup.open { display: flex; }
  .setup-card { background: var(--surface); border: 1px solid var(--border); border-radius: 16px;
                padding: 36px; width: min(640px, 96vw); display: flex; flex-direction: column; gap: 24px; }
  .setup-logo { font-size: 2rem; font-weight: 800; letter-spacing: -1px; }
  .setup-logo span { color: var(--accent); }
  .setup-subtitle { color: var(--muted); font-size: .85rem; margin-top: -16px; }
  .hw-card { background: var(--bg); border: 1px solid var(--border); border-radius: 10px;
             padding: 14px 18px; display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .hw-row { display: flex; gap: 8px; align-items: baseline; font-size: .83rem; }
  .hw-label { color: var(--muted); font-size: .72rem; text-transform: uppercase; letter-spacing: .5px; width: 44px; }
  .hw-val { color: var(--text); font-family: monospace; }
  .model-list { display: flex; flex-direction: column; gap: 6px; }
  .model-item { display: flex; align-items: center; gap: 10px; padding: 10px 14px;
                border: 1px solid var(--border); border-radius: 8px; cursor: pointer;
                transition: border-color .15s; }
  .model-item:hover, .model-item.selected { border-color: var(--accent); background: rgba(88,166,255,.05); }
  .model-item input[type=radio] { accent-color: var(--accent); flex-shrink: 0; }
  .model-name { flex: 1; font-size: .88rem; }
  .model-size { font-size: .75rem; color: var(--muted); font-family: monospace; }
  .tag-rec { background: var(--accent); color: #000; font-size: .65rem; font-weight: 700;
             padding: 2px 6px; border-radius: 4px; letter-spacing: .3px; }
  .tag-installed { background: var(--green); color: #000; font-size: .65rem; font-weight: 700;
                   padding: 2px 6px; border-radius: 4px; }
  .tag-incompativel { background: var(--border); color: var(--muted); font-size: .65rem;
                      padding: 2px 6px; border-radius: 4px; }
  .setup-section-title { font-size: .72rem; text-transform: uppercase; letter-spacing: .7px;
                          color: var(--muted); margin-bottom: 4px; }
  .setup-log { background: #010409; border: 1px solid var(--border); border-radius: 8px;
               padding: 12px; font-family: monospace; font-size: .75rem; height: 100px;
               overflow-y: auto; color: var(--green); display: none; }
  .setup-log.open { display: block; }
  #setupOverlay { display: none; }

  /* ── Progress bar ── */
  .progress-wrap { height: 3px; background: var(--border); border-radius: 2px; overflow: hidden; }
  .progress-bar  { height: 100%; background: var(--accent); transition: width .3s; border-radius: 2px; }
  .phase-prog-wrap { height: 3px; background: var(--border); border-radius: 2px; overflow: hidden; margin-top: 6px; }
  .phase-prog-bar  { height: 100%; background: var(--accent); transition: width .5s ease; border-radius: 2px; width: 0%; }
  .phase-prog-label { font-size: .65rem; color: var(--muted); margin-top: 2px; }

  /* ── Paths box ── */
  .paths-box { background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius);
               padding: 12px 14px; margin-bottom: 10px; display: flex; flex-direction: column; gap: 8px; }
  .path-row  { display: flex; align-items: center; gap: 10px; }
  .path-label { font-size: .65rem; font-weight: 700; text-transform: uppercase; letter-spacing: .8px;
                color: var(--accent); width: 52px; flex-shrink: 0; }
  .path-input { flex: 1; min-width: 0; background: transparent; border: none;
                border-bottom: 1px solid var(--border);
                color: var(--text); font-family: monospace; font-size: .8rem; padding: 3px 0;
                outline: none; }
  .path-input:focus { border-bottom-color: var(--accent); }
  .path-browse-btn { background: var(--surface); border: 1px solid var(--border); border-radius: 4px;
                     color: var(--muted); font-size: .75rem; padding: 3px 8px; cursor: pointer;
                     flex-shrink: 0; white-space: nowrap; }
  .path-browse-btn:hover { border-color: var(--accent); color: var(--accent); }

  /* ── File browser modal ── */
  #browserOverlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.6);
                    z-index: 300; align-items: center; justify-content: center; }
  #browserOverlay.open { display: flex; }
  .browser-box { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
                 width: min(520px, 94vw); max-height: 70vh; display: flex; flex-direction: column;
                 overflow: hidden; }
  .browser-header { padding: 12px 16px; border-bottom: 1px solid var(--border);
                    display: flex; align-items: center; gap: 8px; }
  .browser-header span { font-size: .7rem; color: var(--muted); font-family: monospace; flex: 1;
                          overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .browser-up { background: none; border: 1px solid var(--border); border-radius: 4px;
                color: var(--muted); font-size: .75rem; padding: 3px 8px; cursor: pointer; }
  .browser-up:hover { border-color: var(--accent); color: var(--accent); }
  .browser-list { overflow-y: auto; flex: 1; }
  .browser-item { padding: 9px 16px; cursor: pointer; display: flex; align-items: center; gap: 8px;
                  font-size: .85rem; border-bottom: 1px solid var(--border); }
  .browser-item:hover { background: var(--bg); color: var(--accent); }
  .browser-item::before { content: "📁"; font-size: .8rem; }
  .browser-footer { padding: 10px 16px; border-top: 1px solid var(--border);
                    display: flex; gap: 8px; justify-content: flex-end; }

  /* ── Config tabs ── */
  .cfg-tab { background: none; border: none; border-bottom: 2px solid transparent; color: var(--muted);
             font-size: .82rem; padding: 6px 14px 8px; cursor: pointer; margin-bottom: -1px; }
  .cfg-tab.active { color: var(--text); border-bottom-color: var(--accent); }
  .cfg-tab:hover { color: var(--text); }

  /* ── GPU bar ── */
  .gpu-widget { display:flex; align-items:center; gap:10px; padding:8px 20px;
                background: var(--surface); border-bottom: 1px solid var(--border);
                font-size:.78rem; }
  .gpu-label  { color: var(--muted); font-size:.7rem; text-transform:uppercase;
                letter-spacing:.5px; white-space:nowrap; }
  .gpu-bar-wrap { flex:1; height:6px; background:var(--border); border-radius:3px; overflow:hidden; }
  .gpu-bar      { height:100%; background:var(--green); border-radius:3px; transition:width .4s,background .4s; }
  .gpu-info { white-space:nowrap; color:var(--text); }
  .gpu-vram { white-space:nowrap; color:var(--muted); font-size:.72rem; }
</style>
</head>
<body>

<div class="app">

  <!-- Header -->
  <header class="header">
    <span style="font-size:1.3rem">🧠</span>
    <h1>Nomos</h1>
    <span class="tag">local</span>
    <div class="spacer"></div>
    <span id="hStatus" style="font-size:.8rem;color:var(--muted)">Ocioso</span>
    <div id="statusDot" class="status-dot"></div>
    <button class="btn danger" onclick="parar()" style="margin-left:8px">⏹ Parar</button>
    <button class="btn" onclick="abrirConfig()" style="margin-left:4px">⚙ Config</button>
    <button class="btn" onclick="abrirSetup()" style="margin-left:4px" title="Setup e modelos Ollama">🤖 Setup</button>
  </header>

  <!-- Sidebar -->
  <aside class="sidebar">
    <div class="sidebar-title">Pastas do vault</div>
    <div id="folderTree">
      <div style="padding:12px 16px;color:var(--muted);font-size:.78rem">Carregando...</div>
    </div>
  </aside>

  <!-- Main -->
  <main class="main">

    <!-- Stats -->
    <div class="stats">
      <div class="stat tot"><div class="n" id="sOrigem">–</div><div class="l">📂 Source</div></div>
      <div class="stat ok"> <div class="n" id="sDestino">–</div><div class="l">📁 Destino</div></div>
      <div class="stat pend" id="statPend" style="display:none"><div class="n" id="sPend">0</div><div class="l">Pendentes</div></div>
      <div class="stat err"  id="statErr"  style="display:none"><div class="n" id="sErr">0</div><div class="l">Erros</div></div>
      <div class="stat" style="cursor:pointer" onclick="resetarDB()" title="Limpar banco de dados do pipeline anterior">
        <div class="n" style="font-size:.9rem">🗑</div><div class="l">Resetar DB</div>
      </div>
    </div>

    <!-- Pipeline -->
    <div class="pipeline">

      <!-- Paths: sempre visíveis -->
      <div class="paths-box">
        <div class="path-row">
          <span class="path-label">SOURCE</span>
          <input type="text" id="inOrigem" class="path-input" placeholder="/caminho/para/arquivos-brutos"
                 onchange="salvarPaths()">
          <button class="path-browse-btn" onclick="abrirBrowser('inOrigem')">📂 pasta</button>
        </div>
        <div class="path-row">
          <span class="path-label">DESTINO</span>
          <input type="text" id="inDestino" class="path-input" placeholder="/caminho/do/vault-organizado"
                 onchange="salvarPaths()">
          <button class="path-browse-btn" onclick="abrirBrowser('inDestino')">📂 pasta</button>
        </div>
      </div>

      <!-- Botão principal -->
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:4px">
        <button class="btn primary" id="btnTudo" onclick="runTudo(true)"
                style="flex:1;padding:10px;font-size:.95rem;letter-spacing:.3px">
          ▶ Rodar Tudo  <span style="opacity:.7;font-size:.8rem">(Bootstrap → Classify → Links → Rename)</span>
        </button>
        <button class="btn" id="btnTudoSemRename" onclick="runTudo(false)"
                title="Rodar sem Rename (mais rápido)"
                style="padding:10px;font-size:.85rem">
          ▶ Sem Rename
        </button>
      </div>

      <div style="height:1px;background:var(--border);margin:4px 0"></div>

      <!-- Fases individuais -->
      <div class="phase ready" id="phBootstrap">
        <div class="num">1</div>
        <div class="info">
          <h3>Bootstrap</h3>
          <p>Cria taxonomia de pastas + _sobre_.md a partir de amostras</p>
          <div class="phase-prog-wrap" id="progWrapBootstrap" style="display:none">
            <div class="phase-prog-bar" id="progBarBootstrap"></div>
          </div>
          <span class="phase-prog-label" id="progLabelBootstrap" style="display:none"></span>
        </div>
        <button class="btn" onclick="runFase('bootstrap')">▶ Rodar</button>
      </div>

      <div class="phase" id="phClassify">
        <div class="num">2</div>
        <div class="info">
          <h3>Embed + Classify</h3>
          <p>Classifica arquivos por similaridade vetorial (nomic-embed-text)</p>
          <div class="phase-prog-wrap" id="progWrapClassify" style="display:none">
            <div class="phase-prog-bar" id="progBarClassify"></div>
          </div>
          <span class="phase-prog-label" id="progLabelClassify" style="display:none"></span>
        </div>
        <div style="display:flex;gap:6px">
          <button class="btn" onclick="runFase('classify')">▶ Lote</button>
          <button class="btn" onclick="runFaseLoop('classify')" title="Rodar até zerar pendentes">⟳ Loop</button>
        </div>
      </div>

      <div class="phase" id="phLinks">
        <div class="num">3</div>
        <div class="info">
          <h3>Wiki Links</h3>
          <p>Cria [[links]] entre arquivos similares dentro de cada pasta</p>
          <div class="phase-prog-wrap" id="progWrapLinks" style="display:none">
            <div class="phase-prog-bar" id="progBarLinks"></div>
          </div>
          <span class="phase-prog-label" id="progLabelLinks" style="display:none"></span>
        </div>
        <button class="btn" onclick="runFase('links')">▶ Rodar</button>
      </div>

      <div class="phase" id="phRename">
        <div class="num">4</div>
        <div class="info">
          <h3>Rename</h3>
          <p>Renomeia arquivos com títulos descritivos via <span id="modeloLabel">—</span></p>
          <div class="phase-prog-wrap" id="progWrapRename" style="display:none">
            <div class="phase-prog-bar" id="progBarRename"></div>
          </div>
          <span class="phase-prog-label" id="progLabelRename" style="display:none"></span>
        </div>
        <div style="display:flex;gap:6px">
          <button class="btn" onclick="runFase('rename')">▶ Lote</button>
          <button class="btn" onclick="runFaseLoop('rename')" title="Renomear tudo">⟳ Loop</button>
        </div>
      </div>
    </div>

    <!-- GPU Monitor -->
    <div class="gpu-widget">
      <span class="gpu-label">GPU</span>
      <div class="gpu-bar-wrap"><div class="gpu-bar" id="gpuBar" style="width:0%"></div></div>
      <span class="gpu-info" id="gpuTxt">...</span>
      <span class="gpu-vram" id="gpuVram"></span>
    </div>

    <!-- Progress bar -->
    <div style="padding: 0 20px 8px">
      <div class="progress-wrap"><div class="progress-bar" id="progressBar" style="width:0%"></div></div>
      <div style="display:flex;justify-content:space-between;margin-top:4px">
        <span style="font-size:.7rem;color:var(--muted)" id="progressLabel">0%</span>
        <span style="font-size:.7rem;color:var(--muted)" id="progressEta"></span>
      </div>
    </div>

    <!-- Log -->
    <div class="log-section">
      <div class="log-header">
        <h3>Log</h3>
        <button class="btn" onclick="copiarLog()" title="Copiar log para área de transferência">📋 Copiar</button>
        <button class="btn" onclick="limparLog()">Limpar</button>
        <label style="font-size:.8rem;display:flex;align-items:center;gap:4px;cursor:pointer">
          <input type="checkbox" id="autoScroll" checked> Auto-scroll
        </label>
      </div>
      <div class="log" id="logEl"></div>
    </div>
  </main>
</div>

<!-- Setup overlay (primeira vez / sem config salva) -->
<div id="setupOverlay">
  <div class="setup-box">
    <h1>🧠 Nomos</h1>
    <p class="tagline">Configure os caminhos para começar</p>

    <div class="form-group">
      <label>Source — pasta com os arquivos brutos</label>
      <input type="text" id="setupOrigem" placeholder="/home/usuario/Vault/Lídia Memory">
      <div class="hint">Onde estão os .md para classificar</div>
    </div>
    <div class="form-group">
      <label>Destino — onde os arquivos organizados vão ficar</label>
      <input type="text" id="setupDestino" placeholder="/home/usuario/Vault">
      <div class="hint">Pasta raiz do vault organizado (pastas temáticas serão criadas aqui)</div>
    </div>
    <div class="form-group">
      <label>Lote — Classify (arquivos por rodada)</label>
      <input type="number" id="setupLoteC" value="100" min="10" max="500">
    </div>
    <div class="form-group">
      <label>Lote — Rename (arquivos por rodada)</label>
      <input type="number" id="setupLoteR" value="20" min="5" max="100">
    </div>
    <div class="modal-actions">
      <button class="btn primary" onclick="salvarSetup()" style="padding:10px 24px">
        Confirmar e abrir →
      </button>
    </div>
  </div>
</div>

<!-- ── Nomos Setup Overlay ─────────────────────────────────────────────────── -->
<div id="nomosSetup">
  <div class="setup-card">
    <div>
      <div class="setup-logo">N<span>o</span>mos</div>
      <div class="setup-subtitle">O espírito grego das leis e da organização de estruturas</div>
    </div>

    <!-- Hardware -->
    <div>
      <div class="setup-section-title">Hardware detectado</div>
      <div class="hw-card" id="hwCard">
        <div class="hw-row"><span class="hw-label">GPU</span><span class="hw-val" id="hwGpu">Detectando...</span></div>
        <div class="hw-row"><span class="hw-label">VRAM</span><span class="hw-val" id="hwVram">—</span></div>
        <div class="hw-row"><span class="hw-label">RAM</span><span class="hw-val" id="hwRam">—</span></div>
        <div class="hw-row"><span class="hw-label">CPU</span><span class="hw-val" id="hwCpu">—</span></div>
      </div>
    </div>

    <!-- Ollama status + install -->
    <div id="ollamaSection">
      <div class="setup-section-title">Ollama</div>
      <div id="ollamaStatus" style="font-size:.85rem;color:var(--muted);margin-bottom:8px">Verificando...</div>
      <button class="btn primary" id="btnInstalarOllama" style="display:none"
              onclick="instalarOllama()">⬇ Instalar Ollama</button>
    </div>

    <!-- Modelo -->
    <div id="modelSection" style="display:none">
      <div class="setup-section-title">Modelo de linguagem</div>
      <div class="model-list" id="modelList"></div>
      <div style="margin-top:8px;display:flex;gap:8px">
        <button class="btn primary" id="btnBaixarModelo" onclick="baixarModeloSelecionado()">
          ⬇ Baixar modelo selecionado
        </button>
        <button class="btn" id="btnUsarModelo" style="display:none" onclick="fecharSetup()">
          ✓ Usar este modelo
        </button>
      </div>
    </div>

    <!-- Log de download -->
    <div id="setupLogWrap" style="display:none">
      <div class="setup-section-title">Progresso</div>
      <div class="setup-log open" id="setupLog"></div>
    </div>

  </div>
</div>

<!-- Config modal -->
<div class="modal-bg" id="modalBg" onclick="if(event.target===this)fecharConfig()">
  <div class="modal" style="width:600px">
    <h2>⚙ Configurações</h2>

    <!-- Abas -->
    <div style="display:flex;gap:4px;margin-bottom:20px;border-bottom:1px solid var(--border);padding-bottom:0">
      <button class="cfg-tab active" onclick="abrirAba('pipeline')" id="tabPipeline">Pipeline</button>
      <button class="cfg-tab" onclick="abrirAba('modelos')" id="tabModelos">Modelos Ollama</button>
    </div>

    <!-- Aba: Pipeline -->
    <div id="abaP">
      <div class="form-group">
        <label>Pasta de origem (arquivos brutos)</label>
        <input type="text" id="cfgOrigem" value="">
      </div>
      <div class="form-group">
        <label>Pasta de destino (vault organizado)</label>
        <input type="text" id="cfgDestino" value="">
      </div>
      <div class="form-group">
        <label>Lote — Classify (arquivos por rodada)</label>
        <input type="number" id="cfgLoteClassify" value="100" min="10" max="500">
      </div>
      <div class="form-group">
        <label>Lote — Rename (arquivos por rodada)</label>
        <input type="number" id="cfgLoteRename" value="20" min="5" max="100">
      </div>
    </div>

    <!-- Aba: Modelos -->
    <div id="abaM" style="display:none">
      <div style="font-size:.78rem;color:var(--muted);margin-bottom:12px">
        Modelos instalados no Ollama · ⭐ = recomendado para seu hardware
      </div>
      <div class="model-list" id="cfgModelList" style="max-height:280px;overflow-y:auto"></div>
      <div style="margin-top:12px;display:flex;gap:8px;align-items:center">
        <input type="text" id="cfgModelInput" placeholder="nome:tag (ex: gemma3:4b)"
               style="flex:1;background:var(--bg);border:1px solid var(--border);border-radius:6px;
                      padding:6px 10px;color:var(--text);font-family:monospace;font-size:.82rem">
        <button class="btn primary" onclick="cfgBaixarModelo()">⬇ Baixar</button>
      </div>
      <div class="setup-log" id="cfgModelLog" style="margin-top:10px;height:80px"></div>
    </div>

    <div class="modal-actions" style="margin-top:20px">
      <button class="btn" onclick="fecharConfig()">Fechar</button>
      <button class="btn primary" id="btnSalvarCfg" onclick="salvarConfig()">Salvar</button>
    </div>
  </div>
</div>

<script>
// ── Nomos Setup ───────────────────────────────────────────────────────────────
let _hwData   = null;
let _ollamaOk = false;
let _modeloSelecionado = null;

async function abrirSetup() {
  document.getElementById("nomosSetup").classList.add("open");
  await carregarSetupConteudo();
}

async function iniciarSetup() {
  const jaConfigurado = localStorage.getItem("nomosSetupDone");
  const status = await fetch("/api/ollama-status").then(r => r.json()).catch(() => ({instalado: false, modelos: []}));
  _ollamaOk = status.instalado && status.modelos.length > 0;

  // Mostra se nunca configurou OU se o Ollama não está ok
  if (jaConfigurado && _ollamaOk) return;

  document.getElementById("nomosSetup").classList.add("open");
  await carregarSetupConteudo();
}

async function carregarSetupConteudo() {
  const [hw, status] = await Promise.all([
    fetch("/api/hardware").then(r => r.json()).catch(() => ({})),
    fetch("/api/ollama-status").then(r => r.json()).catch(() => ({instalado: false, modelos: []}))
  ]);
  _hwData = hw;

  document.getElementById("hwGpu").textContent  = hw.gpu || "Não detectada";
  document.getElementById("hwVram").textContent = hw.vram_total_mb ? `${(hw.vram_total_mb/1024).toFixed(1)} GB` : "—";
  document.getElementById("hwRam").textContent  = hw.ram_gb ? `${hw.ram_gb} GB` : "—";
  document.getElementById("hwCpu").textContent  = hw.cpu_cores ? `${hw.cpu_cores} núcleos` : "—";

  const ollamaEl = document.getElementById("ollamaStatus");
  if (!status.instalado) {
    ollamaEl.textContent = "❌ Ollama não encontrado";
    ollamaEl.style.color = "var(--red)";
    document.getElementById("btnInstalarOllama").style.display = "";
  } else {
    ollamaEl.textContent = "✅ Ollama instalado";
    ollamaEl.style.color = "var(--green)";
    renderizarModelos(status.modelos, hw.catalogo || []);
  }
}

function renderizarModelos(instalados, catalogo) {
  const sec  = document.getElementById("modelSection");
  const list = document.getElementById("modelList");
  sec.style.display = "";
  list.innerHTML = "";

  const set = new Set(instalados.map(m => m.split(":")[0] + (m.includes(":") ? ":" + m.split(":")[1] : "")));

  catalogo.forEach((m, i) => {
    const estaInstalado = instalados.some(inst => inst.startsWith(m.id.split(":")[0]));
    const tags = [];
    if (m.recomendado)   tags.push(`<span class="tag-rec">⭐ Recomendado</span>`);
    if (estaInstalado)   tags.push(`<span class="tag-installed">✓ Instalado</span>`);
    if (!m.compativel && !m.embed) tags.push(`<span class="tag-incompativel">VRAM insuficiente</span>`);

    const item = document.createElement("label");
    item.className = "model-item" + (m.recomendado ? " selected" : "");
    item.innerHTML = `
      <input type="radio" name="modelChoice" value="${m.id}" ${m.recomendado ? "checked" : ""}>
      <span class="model-name">${m.nome}</span>
      <span class="model-size">${m.tam}</span>
      ${tags.join(" ")}
    `;
    item.querySelector("input").addEventListener("change", () => {
      document.querySelectorAll(".model-item").forEach(el => el.classList.remove("selected"));
      item.classList.add("selected");
      _modeloSelecionado = m.id;
      // Se já instalado, mostra "Usar este modelo" em vez de "Baixar"
      document.getElementById("btnBaixarModelo").style.display = estaInstalado ? "none" : "";
      document.getElementById("btnUsarModelo").style.display   = estaInstalado ? "" : "none";
    });
    if (m.recomendado) _modeloSelecionado = m.id;
    list.appendChild(item);
  });

  // Se nenhum instalado, botão principal é "Baixar"; se o recomendado já está instalado, mostrar "Usar"
  const recInstalado = instalados.some(m2 => {
    const rec = (catalogo.find(c => c.recomendado) || {}).id || "";
    return m2.startsWith(rec.split(":")[0]);
  });
  document.getElementById("btnBaixarModelo").style.display = recInstalado ? "none" : "";
  document.getElementById("btnUsarModelo").style.display   = recInstalado ? "" : "none";
}

async function instalarOllama() {
  document.getElementById("btnInstalarOllama").disabled = true;
  abrirSetupLog();
  const es = new EventSource("/api/instalar-ollama");
  es.onmessage = e => {
    if (e.data === "[DONE]") {
      es.close();
      // Recarrega status
      fetch("/api/ollama-status").then(r => r.json()).then(s => {
        document.getElementById("ollamaStatus").textContent = "✅ Ollama instalado";
        document.getElementById("ollamaStatus").style.color = "var(--green)";
        document.getElementById("btnInstalarOllama").style.display = "none";
        renderizarModelos(s.modelos, _hwData?.catalogo || []);
      });
      return;
    }
    appendSetupLog(e.data);
  };
}

async function baixarModeloSelecionado() {
  const model = _modeloSelecionado || (document.querySelector('input[name="modelChoice"]:checked') || {}).value;
  if (!model) return;
  document.getElementById("btnBaixarModelo").disabled = true;
  abrirSetupLog();
  const es = new EventSource(`/api/pull-model?model=${encodeURIComponent(model)}`);
  es.onmessage = e => {
    if (e.data === "[DONE]") {
      es.close();
      document.getElementById("btnBaixarModelo").style.display = "none";
      document.getElementById("btnUsarModelo").style.display   = "";
      appendSetupLog(`✅ ${model} pronto!`);
      const c = JSON.parse(localStorage.getItem("nomoscfg") || "{}");
      c.modelo = model;
      localStorage.setItem("nomoscfg", JSON.stringify(c));
      const lbl = document.getElementById("modeloLabel");
      if (lbl) lbl.textContent = model;
      return;
    }
    appendSetupLog(e.data);
  };
  // Linhas de progresso substituem a última linha
  es.addEventListener("progress", e => updateSetupLogProgress(e.data));
}

function abrirSetupLog() {
  document.getElementById("setupLogWrap").style.display = "";
}
function appendSetupLog(txt) {
  const el = document.getElementById("setupLog");
  el.textContent += txt + "\\n";
  el.scrollTop = el.scrollHeight;
}
function updateSetupLogProgress(txt) {
  const el = document.getElementById("setupLog");
  // Substitui a última linha se for linha de progresso, senão acrescenta
  const lines = el.textContent.split("\\n");
  const last = lines[lines.length - 2] || "";
  if (last.includes("%") || last.startsWith("pulling") || last.startsWith("verifying")) {
    lines[lines.length - 2] = txt;
  } else {
    lines.splice(lines.length - 1, 0, txt);
  }
  el.textContent = lines.join("\\n");
  el.scrollTop = el.scrollHeight;
}

function fecharSetup() {
  // Salva o modelo selecionado antes de fechar
  const radio = document.querySelector('input[name="modelChoice"]:checked');
  if (radio) {
    const c = JSON.parse(localStorage.getItem("nomoscfg") || "{}");
    c.modelo = radio.value;
    localStorage.setItem("nomoscfg", JSON.stringify(c));
    const lbl = document.getElementById("modeloLabel");
    if (lbl) lbl.textContent = radio.value;
  }
  document.getElementById("nomosSetup").classList.remove("open");
  localStorage.setItem("nomosSetupDone", "1");
}

// ── Config ────────────────────────────────────────────────────────────────────
const DEFAULTS = {
  origem:       "%(ORIGEM)s",
  destino:      "%(DESTINO)s",
  loteClassify: 100,
  loteRename:   20,
  threshold:    0.75,
  maxLinks:     5,
};

function cfg() {
  const saved = JSON.parse(localStorage.getItem("nomoscfg") || "{}");
  // Lê os campos visíveis como fonte primária
  const origem  = (document.getElementById("inOrigem")  || {}).value || saved.origem  || DEFAULTS.origem;
  const destino = (document.getElementById("inDestino") || {}).value || saved.destino || DEFAULTS.destino;
  return Object.assign({}, DEFAULTS, saved, {
    origem:  origem.trim()  || DEFAULTS.origem,
    destino: destino.trim() || DEFAULTS.destino,
  });
}

function salvarPaths() {
  const c = cfg();
  localStorage.setItem("nomoscfg", JSON.stringify(c));
  atualizarTree();
}

async function resetarDB() {
  if (!confirm("Apagar o banco de dados do pipeline anterior?\\nIsso zera contadores de progresso.")) return;
  await fetch("/api/reset-db", { method: "POST" });
  await atualizarStats();
}

// ── Config modal ──────────────────────────────────────────────────────────────
function abrirAba(aba) {
  document.getElementById("abaP").style.display = aba === "pipeline" ? "" : "none";
  document.getElementById("abaM").style.display = aba === "modelos"  ? "" : "none";
  document.getElementById("tabPipeline").classList.toggle("active", aba === "pipeline");
  document.getElementById("tabModelos").classList.toggle("active",  aba === "modelos");
  document.getElementById("btnSalvarCfg").style.display = aba === "pipeline" ? "" : "none";
  if (aba === "modelos") carregarModelosCfg();
}

async function carregarModelosCfg() {
  const [hw, status] = await Promise.all([
    fetch("/api/hardware").then(r => r.json()).catch(() => ({})),
    fetch("/api/ollama-status").then(r => r.json()).catch(() => ({modelos: []}))
  ]);
  const list = document.getElementById("cfgModelList");
  list.innerHTML = "";
  (hw.catalogo || []).forEach(m => {
    const instalado = status.modelos.some(i => i.startsWith(m.id.split(":")[0]));
    const tags = [];
    if (m.recomendado) tags.push(`<span class="tag-rec">⭐ Recomendado</span>`);
    if (instalado)     tags.push(`<span class="tag-installed">✓ Instalado</span>`);
    else if (!m.compativel && !m.embed) tags.push(`<span class="tag-incompativel">VRAM insuficiente</span>`);
    const row = document.createElement("div");
    row.className = "model-item";
    row.style.cursor = "default";
    row.innerHTML = `<span class="model-name">${m.nome}</span><span class="model-size">${m.tam}</span>${tags.join(" ")}
      ${!instalado ? `<button class="btn" style="margin-left:auto;font-size:.72rem;padding:2px 8px"
        onclick="cfgBaixarModeloId('${m.id}')">⬇</button>` : ""}`;
    list.appendChild(row);
  });
}

async function cfgBaixarModelo() {
  const model = document.getElementById("cfgModelInput").value.trim();
  if (!model) return;
  await cfgBaixarModeloId(model);
}

async function cfgBaixarModeloId(model) {
  const log = document.getElementById("cfgModelLog");
  log.classList.add("open");
  log.textContent = "";
  const es = new EventSource(`/api/pull-model?model=${encodeURIComponent(model)}`);
  es.onmessage = e => {
    if (e.data === "[DONE]") { es.close(); carregarModelosCfg(); return; }
    log.textContent += e.data + "\\n";
    log.scrollTop = log.scrollHeight;
  };
  es.addEventListener("progress", e => {
    const lines = log.textContent.split("\\n");
    const last = lines[lines.length - 2] || "";
    if (last.includes("%") || last.startsWith("pulling") || last.startsWith("verifying")) {
      lines[lines.length - 2] = e.data;
    } else {
      lines.splice(lines.length - 1, 0, e.data);
    }
    log.textContent = lines.join("\\n");
    log.scrollTop = log.scrollHeight;
  });
}

function abrirConfig() {
  const c = cfg();
  document.getElementById("cfgOrigem").value       = c.origem;
  document.getElementById("cfgDestino").value      = c.destino;
  document.getElementById("cfgLoteClassify").value = c.loteClassify;
  document.getElementById("cfgLoteRename").value   = c.loteRename;
  abrirAba("pipeline");
  document.getElementById("modalBg").classList.add("open");
}
function fecharConfig() { document.getElementById("modalBg").classList.remove("open"); }
function salvarConfig() {
  localStorage.setItem("nomoscfg", JSON.stringify({
    origem:       document.getElementById("cfgOrigem").value.trim(),
    destino:      document.getElementById("cfgDestino").value.trim(),
    loteClassify: +document.getElementById("cfgLoteClassify").value,
    loteRename:   +document.getElementById("cfgLoteRename").value,
    threshold:    cfg().threshold,
    maxLinks:     cfg().maxLinks,
  }));
  fecharConfig();
  atualizarStats();
  atualizarTree();
}

// ── Log ───────────────────────────────────────────────────────────────────────
const logEl = document.getElementById("logEl");

function addLog(text) {
  const div = document.createElement("div");
  div.className = "line";
  if (text.startsWith("✓") || text.startsWith("✅"))      div.classList.add("ok");
  else if (text.startsWith("✗") || text.startsWith("⚠")) div.classList.add("err");
  else if (text.startsWith("  ⟳") || text.startsWith("═")) div.classList.add("sep");
  else if (text.includes("Erro") || text.includes("erro"))  div.classList.add("warn");
  div.textContent = text;
  logEl.appendChild(div);
  if (document.getElementById("autoScroll").checked)
    logEl.scrollTop = logEl.scrollHeight;
}

function limparLog() { logEl.innerHTML = ""; }

async function copiarLog() {
  const texto = Array.from(logEl.querySelectorAll(".line"))
    .map(el => el.textContent).join("\\n");
  try {
    await navigator.clipboard.writeText(texto);
    const btn = document.querySelector('button[onclick="copiarLog()"]');
    const orig = btn.textContent;
    btn.textContent = "✓ Copiado";
    setTimeout(() => { btn.textContent = orig; }, 1500);
  } catch(e) {
    prompt("Copie o log abaixo:", texto);
  }
}

// ── Status ────────────────────────────────────────────────────────────────────
let rodando = false;
let looping = false;
let loopFase = "";

const FASE_MAP = {
  bootstrap: {nome:"Bootstrap", card:"phBootstrap",  idx:0},
  classify:  {nome:"Classify",  card:"phClassify",   idx:1},
  links:     {nome:"Links",     card:"phLinks",       idx:2},
  rename:    {nome:"Rename",    card:"phRename",      idx:3},
};
const FASE_ORDEM = ["bootstrap","classify","links","rename"];

function setFaseCard(faseAtiva) {
  // Acende o número da fase ativa, marca anteriores como done
  const idxAtivo = FASE_ORDEM.indexOf(faseAtiva);
  FASE_ORDEM.forEach((f, i) => {
    const el = document.getElementById(FASE_MAP[f].card);
    if (!el) return;
    el.classList.remove("running","done");
    if (i < idxAtivo)       el.classList.add("done");
    else if (i === idxAtivo) el.classList.add("running");
  });
}

function setRodando(val, fase = "") {
  rodando = val;
  document.getElementById("statusDot").className = "status-dot" + (val ? " ativo" : "");
  document.getElementById("hStatus").textContent = val ? `Rodando: ${fase}` : "Ocioso";
  if (!val) {
    // Zera classes e barras ao encerrar
    FASE_ORDEM.forEach(f => {
      const el = document.getElementById(FASE_MAP[f].card);
      if (el) el.classList.remove("running","done");
      const wrap  = document.getElementById("progWrap"  + FASE_MAP[f].nome);
      const bar   = document.getElementById("progBar"   + FASE_MAP[f].nome);
      const label = document.getElementById("progLabel" + FASE_MAP[f].nome);
      if (wrap)  wrap.style.display  = "none";
      if (bar)   bar.style.width     = "0%";
      if (label) label.style.display = "none";
    });
  }
}

function setFaseProg(fase, pct) {
  const info = FASE_MAP[fase];
  if (!info) return;
  // Acende número da fase
  setFaseCard(fase);
  // Atualiza barra
  const wrap  = document.getElementById("progWrap"  + info.nome);
  const bar   = document.getElementById("progBar"   + info.nome);
  const label = document.getElementById("progLabel" + info.nome);
  if (!wrap) return;
  wrap.style.display  = "";
  bar.style.width     = pct + "%";
  if (label) {
    label.style.display = "";
    label.textContent   = pct + "%";
  }
}

async function preflightCheck(fase) {
  const c = cfg();
  if (!c.origem) {
    addLog("✗ SOURCE não configurado. Selecione uma pasta de origem.");
    return false;
  }
  if (fase === "bootstrap" || fase === "tudo") {
    try {
      const r    = await fetch(`/api/scan?origem=${encodeURIComponent(c.origem)}&destino=${encodeURIComponent(c.destino)}`);
      const scan = await r.json();
      const temConteudo = (scan.origem_mds ?? 0) + (scan.origem_archives ?? 0);
      if (temConteudo === 0) {
        addLog("✗ A pasta SOURCE está vazia ou não existe.");
        addLog(`  Caminho: ${c.origem}`);
        addLog("  Precisa ter arquivos .md ou .zip com conversas.");
        return false;
      }
    } catch(e) {
      addLog("✗ Não foi possível verificar a pasta SOURCE.");
      return false;
    }
  }
  return true;
}

// ── Estatísticas ──────────────────────────────────────────────────────────────
let _total = 0, _concl = 0;
let _tempoInicio = null;

async function atualizarStats() {
  try {
    const c = cfg();
    // Dados reais do filesystem
    const rScan = await fetch(`/api/scan?origem=${encodeURIComponent(c.origem)}&destino=${encodeURIComponent(c.destino)}`);
    const scan  = await rScan.json();
    const mds = scan.origem_mds ?? scan.origem_total ?? 0;
    const arcs = scan.origem_archives ?? 0;
    document.getElementById("sOrigem").textContent =
      arcs > 0 ? `${mds} md + ${arcs} zip` : mds.toLocaleString();
    document.getElementById("sDestino").textContent = (scan.destino_total ?? 0).toLocaleString();

    // Progresso do DB — só mostra se o pipeline estiver rodando ou tiver dados ativos
    const rDb = await fetch("/api/stats");
    const d   = await rDb.json();
    const temDados = rodando || (d.pendente > 0 && d.total > 0);
    document.getElementById("statPend").style.display = temDados ? "" : "none";
    document.getElementById("statErr").style.display  = (rodando || d.erro > 0) ? "" : "none";
    document.getElementById("sPend").textContent = d.pendente.toLocaleString();
    document.getElementById("sErr").textContent  = d.erro.toLocaleString();

    const processavel = d.total - d.duplicata;
    const pct = processavel > 0 ? (d.concluido / processavel * 100) : 0;
    document.getElementById("progressBar").style.width = pct.toFixed(1) + "%";
    document.getElementById("progressLabel").textContent = pct.toFixed(1) + "%";

    if (rodando && _tempoInicio && d.concluido > _concl) {
      const delta   = d.concluido - _concl;
      const elapsed = (Date.now() - _tempoInicio) / 1000;
      const rate    = delta / elapsed;
      if (rate > 0) {
        const eta = d.pendente / rate;
        const h = Math.floor(eta / 3600);
        const m = Math.floor((eta % 3600) / 60);
        document.getElementById("progressEta").textContent =
          `ETA: ${h > 0 ? h + "h " : ""}${m}min`;
      }
    }
    _total = d.total;
    _concl = d.concluido;
  } catch (e) {}
}

// ── Emojis por tema de pasta ──────────────────────────────────────────────────
const FOLDER_EMOJIS = [
  [/akai.ito|mulher|namor|relacion|amor|afet|roman/i,    "🧵"],
  [/escrita|conto|poema|letra|ficção|histór|roteiro/i,   "✍️"],
  [/música|song|karaok|melodia|áudio|voxly/i,            "🎵"],
  [/código|program|dev|software|tech|sistema|web|api/i,  "💻"],
  [/ia|llm|model|ollama|intelig|lidia|iris|isa/i,        "🤖"],
  [/reflexão|filosofi|pensament|diário|mente|conscien/i, "🧠"],
  [/projeto|nexus|trabalho|client|contrat|negócio/i,     "💼"],
  [/saúde|corpo|exerc|treino|aliment|bem.estar/i,        "🏋️"],
  [/financ|dinheiro|invest|econom|gasto|renda/i,         "💰"],
  [/viagem|lugar|cidade|mapa|memória|saudade/i,          "🗺️"],
  [/família|mãe|pai|irmão|filho|casa/i,                  "🏠"],
  [/aprendiz|estudo|curso|livro|leitura|conhec/i,        "📚"],
  [/espiritu|crença|fé|medita|universo/i,                "✨"],
  [/jogo|game|anime|série|cinema|entretenimento/i,       "🎮"],
  [/lore.geral|miscelân|geral|vário/i,                   "📦"],
];

function emojiPasta(nome) {
  const n = nome.toLowerCase();
  for (const [re, em] of FOLDER_EMOJIS) {
    if (re.test(n)) return em;
  }
  return "📁";
}

// ── Árvore de pastas ──────────────────────────────────────────────────────────
async function atualizarTree() {
  try {
    const c = cfg();
    const r = await fetch(`/api/tree?destino=${encodeURIComponent(c.destino)}`);
    const pastas = await r.json();
    const el = document.getElementById("folderTree");

    if (!pastas.length) {
      el.innerHTML = '<div style="padding:12px 16px;color:var(--muted);font-size:.78rem">Nenhuma pasta ainda</div>';
      return;
    }

    el.innerHTML = pastas.map(p => {
      const isLore = /lore/i.test(p.nome);
      const cls    = p.tem_sobre ? (isLore ? "lore" : "has-sobre") : "";
      const icon   = emojiPasta(p.nome);
      return `<div class="folder-item ${cls}">
        <span class="folder-icon">${icon}</span>
        <span class="folder-name">${p.nome}</span>
        <span class="folder-count">${p.arquivos}</span>
      </div>`;
    }).join("");
  } catch(e) {}
}

// ── Rodar fase ────────────────────────────────────────────────────────────────
function urlFase(fase) {
  const c = cfg();
  const modelo = c.modelo || "";
  const p = new URLSearchParams();
  if (fase === "bootstrap") {
    p.set("origem",  c.origem);
    p.set("destino", c.destino);
    if (modelo) p.set("modelo", modelo);
  } else if (fase === "classify") {
    p.set("origem",  c.origem);
    p.set("destino", c.destino);
    p.set("lote",    c.loteClassify);
  } else if (fase === "links") {
    p.set("destino",   c.destino);
    p.set("threshold", c.threshold || 0.75);
    p.set("max_links", c.maxLinks  || 5);
  } else if (fase === "rename") {
    p.set("destino", c.destino);
    p.set("lote",    c.loteRename);
    if (modelo) p.set("modelo", modelo);
  } else if (fase === "consolidate") {
    p.set("origem",  c.destino);
    p.set("destino", c.destino);
    p.set("lote",    20);
  }
  return `/api/run/${fase}?${p.toString()}`;
}

// ── Run helpers ───────────────────────────────────────────────────────────────

function conectarSSE(url, label) {
  // Para fases individuais, mostra a barra imediatamente
  if (label && label !== "tudo") setFaseProg(label, 0);
  let faseAtiva = label;

  return new Promise(resolve => {
    const evtSrc = new EventSource(url);
    evtSrc.onmessage = e => {
      if (e.data === "[DONE]") {
        // Marca 100% ao concluir
        if (faseAtiva && faseAtiva !== "tudo") setFaseProg(faseAtiva, 100);
        evtSrc.close(); resolve(); return;
      }
      // Detecta transição de fase no runTudo ("▶ FASE X")
      const faseM = e.data.match(/FASE (\d)/);
      if (faseM) {
        const idx = parseInt(faseM[1]);
        faseAtiva = ["","bootstrap","classify","links","rename"][idx] || label;
        setFaseProg(faseAtiva, 0);
      }
      // Detecta [XX/YY] para progresso inline (bootstrap/classify/rename)
      const progM = e.data.match(/\[(\d+)\/(\d+)\]/);
      if (progM) {
        const pct = Math.round(parseInt(progM[1]) / Math.max(parseInt(progM[2]), 1) * 100);
        if (faseAtiva && faseAtiva !== "tudo") setFaseProg(faseAtiva, pct);
      }
      addLog(e.data);
    };
    // Evento de progresso emitido pelo servidor
    evtSrc.addEventListener("fase-prog", e => {
      const [fase, pctStr] = e.data.split(":");
      setFaseProg(fase, parseInt(pctStr) || 0);
    });
    evtSrc.onerror = () => { evtSrc.close(); resolve(); };
  });
}

async function runTudo(comRename) {
  if (rodando) { addLog("⚠ Aguarde o processo atual terminar."); return; }
  if (!await preflightCheck("tudo")) return;
  looping = false;
  setRodando(true, "tudo");
  _tempoInicio = Date.now();
  addLog("═".repeat(50));
  addLog(`▶ RODAR TUDO${comRename ? "" : " (sem Rename)"}  —  ${new Date().toLocaleTimeString()}`);
  addLog("");

  const c = cfg();
  const p = new URLSearchParams({
    origem:        c.origem,
    destino:       c.destino,
    lote_classify: c.loteClassify,
    lote_rename:   c.loteRename,
    com_rename:    comRename,
    modelo:        c.modelo || ""
  });
  await conectarSSE(`/api/run/tudo?${p}`, "tudo");

  setRodando(false);
  atualizarStats();
  atualizarTree();
}

async function runFase(fase) {
  if (rodando) { addLog("⚠ Aguarde o processo atual terminar."); return; }
  if (!await preflightCheck(fase)) return;
  looping = false;
  setRodando(true, fase);
  _tempoInicio = Date.now();
  addLog("═".repeat(50));
  addLog(`▶ Iniciando: ${fase}  —  ${new Date().toLocaleTimeString()}`);
  addLog("");
  await conectarSSE(urlFase(fase), fase);
  setRodando(false);
  atualizarStats();
  atualizarTree();
}

async function runFaseLoop(fase) {
  if (rodando) { addLog("⚠ Aguarde o processo atual terminar."); return; }
  looping = true;
  _tempoInicio = Date.now();
  addLog(`⟳ Loop iniciado: ${fase}`);

  while (looping) {
    setRodando(true, fase + " [loop]");
    await conectarSSE(urlFase(fase), fase);
    setRodando(false);
    await atualizarStats();
    await atualizarTree();
    try {
      const r = await fetch("/api/stats");
      const d = await r.json();
      if (!looping || d.pendente === 0) {
        looping = false;
        addLog("✅ Loop encerrado — sem mais pendentes.");
        break;
      }
      addLog(`  ⟳ Loop: ${d.pendente} pendentes — continuando...`);
      await new Promise(r => setTimeout(r, 1500));
    } catch(e) { looping = false; break; }
  }
}

async function parar() {
  looping = false;
  try {
    const r = await fetch("/api/parar", { method: "POST" });
    const d = await r.json();
    addLog(d.msg);
    setRodando(false);
    atualizarStats();
  } catch(e) {}
}

// ── GPU Monitor ───────────────────────────────────────────────────────────────
const gpuBar = document.getElementById("gpuBar");
const gpuTxt = document.getElementById("gpuTxt");
const gpuVram = document.getElementById("gpuVram");

async function atualizarGPU() {
  try {
    const r = await fetch("/api/gpu");
    const d = await r.json();
    if (d.error) {
      gpuTxt.textContent = d.error;
      gpuBar.style.width = "0%";
      return;
    }
    const pct = d.util_pct ?? 0;
    gpuBar.style.width = pct + "%";
    gpuBar.style.background = pct > 85 ? "var(--red)" : pct > 60 ? "var(--yellow)" : "var(--green)";
    gpuTxt.textContent = `${d.name ?? "GPU"}  ${pct}%  ${d.temp_c ?? "?"}°C`;
    gpuVram.textContent = d.vram_used_mb != null
      ? `VRAM ${d.vram_used_mb}/${d.vram_total_mb} MB`
      : "";
  } catch(e) {
    gpuTxt.textContent = "GPU: erro de leitura";
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
(function() {
  const saved = JSON.parse(localStorage.getItem("nomoscfg") || "{}");

  // Corrige path de origem salvo errado (continha /Valt/ no meio)
  if (saved.origem && saved.origem.includes("/Valt/")) {
    saved.origem = DEFAULTS.origem;
    localStorage.setItem("nomoscfg", JSON.stringify(saved));
  }

  const origem  = saved.origem  || DEFAULTS.origem;
  const destino = saved.destino || DEFAULTS.destino;

  // Campos visíveis na home
  const elO = document.getElementById("inOrigem");
  const elD = document.getElementById("inDestino");
  if (elO) elO.value = origem;
  if (elD) elD.value = destino;

  // Label do modelo selecionado na descrição do Rename
  const modelo = saved.modelo || "";
  const labelEl = document.getElementById("modeloLabel");
  if (labelEl) labelEl.textContent = modelo || "nenhum modelo selecionado";

  // Sempre esconde o overlay legado
  const ov = document.getElementById("setupOverlay");
  if (ov) ov.style.display = "none";
})();

iniciarSetup();
atualizarStats();
atualizarTree();
atualizarGPU();
setInterval(atualizarStats, 3000);
setInterval(atualizarTree, 8000);
setInterval(atualizarGPU, 2000);
</script>

<!-- ── File Browser Modal ─────────────────────────────────────────────────── -->
<div id="browserOverlay" onclick="if(event.target===this)fecharBrowser()">
  <div class="browser-box">
    <div class="browser-header">
      <button class="browser-up" onclick="browserNavegar(browserParent)" id="browserUpBtn">↑ Subir</button>
      <span id="browserPath">/</span>
    </div>
    <div class="browser-list" id="browserList"></div>
    <div class="browser-footer">
      <button class="btn" onclick="fecharBrowser()">Cancelar</button>
      <button class="btn primary" onclick="browserSelecionar()">✓ Selecionar esta pasta</button>
    </div>
  </div>
</div>

<script>
let browserTarget = null;  // id do input a preencher
let browserParent = null;
let browserCurrent = "/";

async function abrirBrowser(inputId) {
  browserTarget = inputId;
  const input = document.getElementById(inputId);
  const startPath = input.value.trim() || "~";
  browserOverlay.classList.add("open");
  await browserNavegar(startPath);
}

function fecharBrowser() {
  document.getElementById("browserOverlay").classList.remove("open");
  browserTarget = null;
}

async function browserNavegar(path) {
  const res = await fetch("/api/browse?path=" + encodeURIComponent(path || "~"));
  const data = await res.json();
  if (data.error) { alert(data.error); return; }

  browserCurrent = data.current;
  browserParent  = data.parent;

  document.getElementById("browserPath").textContent = data.current;
  const upBtn = document.getElementById("browserUpBtn");
  upBtn.disabled = !data.parent;
  upBtn.style.opacity = data.parent ? "1" : "0.4";

  const list = document.getElementById("browserList");
  list.innerHTML = "";
  if (data.dirs.length === 0) {
    list.innerHTML = '<div style="padding:16px;color:var(--muted);font-size:.85rem">Pasta vazia</div>';
    return;
  }
  data.dirs.forEach(name => {
    const item = document.createElement("div");
    item.className = "browser-item";
    item.textContent = name;
    item.onclick = () => browserNavegar(data.current + "/" + name);
    list.appendChild(item);
  });
}

function browserSelecionar() {
  if (!browserTarget) return;
  const input = document.getElementById(browserTarget);
  if (input) {
    input.value = browserCurrent;
    salvarPaths();
  }
  fecharBrowser();
}
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    html = HTML.replace("%(ORIGEM)s", DEFAULT_ORIGEM).replace("%(DESTINO)s", DEFAULT_DESTINO)
    return HTMLResponse(html)


# ── Entrada ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import socket
    tem_nomos = "nomos" in open("/etc/hosts").read() if Path("/etc/hosts").exists() else False
    url = f"http://nomos:{PORT}" if tem_nomos else f"http://localhost:{PORT}"
    print(f"\n{'═' * 50}")
    print(f"  Nomos — {url}")
    if not tem_nomos:
        print(f"  Dica: sudo sh -c 'echo \"127.0.0.1 nomos\" >> /etc/hosts'")
        print(f"        para acessar via http://nomos:{PORT}")
    print(f"  Vault  : {POV_PATH.parent}")
    print(f"  DB     : {DB_PATH}")
    print(f"{'═' * 50}\n")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
