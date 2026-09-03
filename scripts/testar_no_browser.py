#!/usr/bin/env python3
"""Abre a app num Firefox headless e fotografa-a já carregada.

Existe porque as verificações de ficheiros não chegam: o painel de leitura
esteve uma versão inteira visível por cima da página, e nenhuma inspecção de
CSS ou JS apanhou isso — só abrir a página apanhou.

    python3 scripts/testar_no_browser.py            # a app, carregada
    python3 scripts/testar_no_browser.py --leitor   # abre e fecha um artigo

Dois truques que isto usa: erros de JS são pintados no ecrã (senão morriam na
consola, invisíveis na fotografia), e o evento `load` é segurado por uma imagem
lenta, porque o Firefox fotografa no `load` e os dados chegam depois disso.
"""
from __future__ import annotations

import argparse
import http.server
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
# O Firefox aqui é um snap: não escreve em directórios escondidos nem fora da home.
TRABALHO = Path.home() / "noticias-teste-browser"
PORTA = 8974
SEGURAR_LOAD = 10

CAPTURA_ERROS = """
<div id="diagnostico" style="position:fixed;bottom:0;left:0;right:0;z-index:99;background:#b3261e;
  color:#fff;font:12px/1.5 monospace;padding:8px;white-space:pre-wrap;max-height:40vh;overflow:auto"></div>
<script>
  const caixa = document.getElementById("diagnostico");
  const mostrar = (t) => { caixa.textContent += t + "\\n"; };
  mostrar("sem erros de JavaScript");
  addEventListener("error", (e) =>
    mostrar("ERRO: " + (e.message || e) + " @ " + (e.filename || "") + ":" + (e.lineno || "")), true);
  addEventListener("unhandledrejection", (e) => mostrar("PROMESSA REJEITADA: " + (e.reason?.stack || e.reason)));
</script>
"""

GUIAO_LEITOR = """
<script>
(async () => {
  const dormir = (ms) => new Promise((r) => setTimeout(r, ms));
  const dados = await (await fetch("./data/noticias.json")).json();
  const alvo = dados.artigos.find((a) => a.temTexto);
  if (!alvo) { mostrar("nenhum artigo com texto completo para testar"); return; }
  for (let i = 0; i < 60 && !document.querySelector(`.artigo[data-id="${alvo.id}"] h3 a`); i++) await dormir(100);
  const leitor = document.getElementById("leitor");
  const fundo = document.getElementById("leitor-fundo");
  const ver = () => "leitor(display=" + getComputedStyle(leitor).display + ")"
    + " fundo(display=" + getComputedStyle(fundo).display + ")"
    + " scroll=" + (document.body.style.overflow || "livre");

  document.querySelector(`.artigo[data-id="${alvo.id}"] h3 a`).click();
  await dormir(1200);
  mostrar("1. aberto        " + ver() + " paragrafos=" + document.querySelectorAll(".leitor-texto p").length);
  document.getElementById("leitor-fechar").click(); await dormir(300);
  mostrar("2. botao fechar  " + ver());
  document.querySelector(".artigo h3 a").click(); await dormir(900);
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); await dormir(300);
  mostrar("3. tecla Esc     " + ver());
  document.querySelector(".artigo h3 a").click(); await dormir(900);
  fundo.click(); await dormir(300);
  mostrar("4. clique fora   " + ver());

  const aberto = getComputedStyle(leitor).display !== "none";
  mostrar(aberto ? ">>> FALHOU: o leitor ficou aberto" : ">>> o leitor abre e fecha nas tres formas");
  caixa.style.background = aberto ? "#b3261e" : "#1f6b4d";
})();
</script>
"""


class Servidor(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/lento.gif":
            time.sleep(SEGURAR_LOAD)     # segura o `load` até a app ter desenhado
            corpo = bytes.fromhex("47494638396101000100800000ffffff00000021f90401000000002c"
                                  "00000000010001000002024401003b")
            self.send_response(200)
            self.send_header("Content-Type", "image/gif")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
            return
        super().do_GET()

    def log_message(self, *args):
        pass


def preparar(com_leitor: bool) -> Path:
    if TRABALHO.exists():
        shutil.rmtree(TRABALHO)
    site = TRABALHO / "site"
    site.mkdir(parents=True)
    for nome in ("index.html", "assets", "data", "sw.js", "manifest.webmanifest"):
        origem = RAIZ / nome
        (shutil.copytree if origem.is_dir() else shutil.copy)(origem, site / nome)

    pagina = (site / "index.html").read_text(encoding="utf-8")
    pagina = pagina.replace("<body>", "<body>" + CAPTURA_ERROS)
    fim = (GUIAO_LEITOR if com_leitor else "")
    fim += '<img src="/lento.gif" alt="" style="position:absolute;width:1px;height:1px;opacity:0">\n</body>'
    (site / "index.html").write_text(pagina.replace("</body>", fim), encoding="utf-8")
    return site


def main() -> int:
    analisador = argparse.ArgumentParser(description=__doc__)
    analisador.add_argument("--leitor", action="store_true",
                            help="abre um artigo e testa as três formas de fechar")
    argumentos = analisador.parse_args()

    if not shutil.which("firefox"):
        print("firefox não encontrado — este teste precisa dele", file=sys.stderr)
        return 2

    site = preparar(argumentos.leitor)
    servidor = http.server.ThreadingHTTPServer(
        ("127.0.0.1", PORTA),
        lambda *a, **k: Servidor(*a, directory=str(site), **k),
    )
    threading.Thread(target=servidor.serve_forever, daemon=True).start()

    destino = TRABALHO / ("leitor.png" if argumentos.leitor else "app.png")
    print(f"a abrir http://127.0.0.1:{PORTA}/ no Firefox headless…")
    subprocess.run(
        ["firefox", "--headless", "--screenshot", str(destino),
         "--window-size=1280,900", f"http://127.0.0.1:{PORTA}/"],
        env={"MOZ_HEADLESS": "1", "HOME": str(Path.home()), "PATH": "/usr/bin:/bin"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180,
    )
    servidor.shutdown()

    if not destino.exists():
        print("o Firefox não gravou nada — se for snap, o destino tem de estar "
              "na home e fora de directórios escondidos", file=sys.stderr)
        return 1
    print(f"fotografia: {destino}\nA faixa no fundo mostra erros de JavaScript "
          f"(verde = passou, vermelho = falhou).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
