#!/usr/bin/env python3
"""Recolhe os feeds RSS/Atom definidos em fontes.json e escreve data/noticias.json.

Só usa a biblioteca padrão. Corre localmente (`python3 scripts/recolher.py`) ou
no GitHub Actions. Falhas de uma fonte nunca derrubam a recolha: ficam
registadas no relatório de estado que a app mostra ao utilizador.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import gzip
import hashlib
import html
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from limpar_html import comprimento_texto, limpar  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent
FONTES = RAIZ / "fontes.json"
SAIDA = RAIZ / "data" / "noticias.json"
PASTA_ARTIGOS = RAIZ / "data" / "artigos"

# Abaixo disto o "corpo" do feed é só um resumo repetido, não vale um leitor.
MIN_TEXTO_LEITOR = 1200

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
CABECALHOS = {
    "User-Agent": UA,
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
}

# Alguns sites servem TLS antigo ou cadeias incompletas; não queremos perder a
# fonte por isso, o conteúdo é público e não enviamos credenciais.
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
try:
    CTX.set_ciphers("DEFAULT:@SECLEVEL=1")
except ssl.SSLError:
    pass

NS = {
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "atom": "http://www.w3.org/2005/Atom",
}

# Janela larga de propósito: os meios independentes publicam devagar (o Jornal
# MAPA é trimestral, o Divergente publica algumas vezes por mês). Uma janela
# curta apagaria precisamente as fontes que esta app existe para mostrar. O
# limite por fonte é que impede os diários de alto volume de afogar o resto.
DIAS_JANELA = 120
MAX_POR_FONTE = 40
TENTATIVAS = 3

LIXO_QUERY = re.compile(r"^(utm_|fbclid|gclid|mc_|ref|source|xtor)", re.I)
TAGS = re.compile(r"<[^>]+>")
ESPACOS = re.compile(r"\s+")


# --------------------------------------------------------------------------- rede

def buscar(url: str) -> str:
    """Descarrega um feed, com tentativas e recuo progressivo."""
    ultimo = None
    for tentativa in range(TENTATIVAS):
        try:
            pedido = urllib.request.Request(url, headers=CABECALHOS)
            with urllib.request.urlopen(pedido, timeout=30, context=CTX) as resposta:
                bruto = resposta.read()
                codificacao = (resposta.headers.get("Content-Encoding") or "").lower()
                if codificacao == "gzip":
                    bruto = gzip.decompress(bruto)
                elif codificacao == "deflate":
                    bruto = zlib.decompress(bruto, -zlib.MAX_WBITS)
                charset = resposta.headers.get_content_charset() or "utf-8"
                try:
                    return bruto.decode(charset, "replace")
                except LookupError:
                    return bruto.decode("utf-8", "replace")
        except Exception as erro:  # noqa: BLE001 - queremos registar tudo
            ultimo = erro
            if tentativa < TENTATIVAS - 1:
                time.sleep(2 * (tentativa + 1))
    raise ultimo  # type: ignore[misc]


# ------------------------------------------------------------------------ parsing

def texto_limpo(valor: str | None, limite: int | None = None) -> str:
    if not valor:
        return ""
    limpo = html.unescape(TAGS.sub(" ", valor))
    limpo = ESPACOS.sub(" ", limpo).strip()
    if limite and len(limpo) > limite:
        corte = limpo[:limite].rsplit(" ", 1)[0]
        limpo = corte.rstrip(" ,.;:—-") + "…"
    return limpo


def primeiro(no: ET.Element, *caminhos: str) -> str:
    for caminho in caminhos:
        achado = no.find(caminho, NS)
        if achado is not None:
            if achado.text and achado.text.strip():
                return achado.text
            href = achado.get("href")
            if href:
                return href
    return ""


def data_iso(valor: str) -> str | None:
    valor = (valor or "").strip()
    if not valor:
        return None
    try:
        from email.utils import parsedate_to_datetime

        momento = parsedate_to_datetime(valor)
        if momento:
            if momento.tzinfo is None:
                momento = momento.replace(tzinfo=timezone.utc)
            return momento.astimezone(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        pass
    tentativa = valor.replace("Z", "+00:00")
    tentativa = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", tentativa)
    for formato in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M"):
        try:
            momento = (
                datetime.fromisoformat(tentativa)
                if formato is None
                else datetime.strptime(valor, formato)
            )
            if momento.tzinfo is None:
                momento = momento.replace(tzinfo=timezone.utc)
            return momento.astimezone(timezone.utc).isoformat()
        except Exception:  # noqa: BLE001
            continue
    return None


def normalizar_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    try:
        partes = urllib.parse.urlsplit(url)
        query = [
            (chave, valor)
            for chave, valor in urllib.parse.parse_qsl(partes.query, keep_blank_values=True)
            if not LIXO_QUERY.match(chave)
        ]
        caminho = partes.path.rstrip("/") or "/"
        return urllib.parse.urlunsplit(
            (partes.scheme or "https", partes.netloc.lower(), caminho,
             urllib.parse.urlencode(query), "")
        )
    except ValueError:
        return url


def extrair_imagem(no: ET.Element, corpo_html: str) -> str:
    for caminho in ("media:content", "media:thumbnail", "enclosure"):
        for achado in no.findall(caminho, NS):
            url = achado.get("url") or achado.get("href") or ""
            tipo = achado.get("type") or ""
            if url and (tipo.startswith("image") or re.search(r"\.(jpe?g|png|webp|avif)", url, re.I)):
                return url
    for achado in no.findall("media:group/media:content", NS):
        if achado.get("url"):
            return achado.get("url", "")
    encontrada = re.search(r'<img[^>]+src=["\']([^"\']+)', corpo_html or "", re.I)
    return encontrada.group(1) if encontrada else ""


def itens_do_feed(xml_bruto: str) -> list[ET.Element]:
    xml_bruto = xml_bruto.lstrip("﻿ \t\r\n")
    # Remove caracteres de controlo ilegais que rebentam o parser.
    xml_bruto = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_bruto)
    try:
        raiz = ET.fromstring(xml_bruto)
    except ET.ParseError:
        # Última hipótese: cortar lixo antes da declaração XML.
        inicio = xml_bruto.find("<")
        raiz = ET.fromstring(xml_bruto[inicio:] if inicio > 0 else xml_bruto)
    itens = raiz.findall(".//item") + raiz.findall(".//atom:entry", NS)
    if not itens:
        itens = [n for n in raiz.iter() if n.tag.split("}")[-1] == "entry"]
    return itens


def converter(no: ET.Element, fonte: dict) -> dict | None:
    titulo = texto_limpo(primeiro(no, "title", "atom:title"), 200)
    ligacao = primeiro(no, "link", "atom:link[@rel='alternate']", "atom:link", "guid")
    if not titulo or not ligacao:
        return None
    ligacao = normalizar_url(html.unescape(ligacao))
    if not ligacao.startswith("http"):
        return None

    corpo = (
        primeiro(no, "content:encoded")
        or primeiro(no, "description", "atom:summary", "atom:content")
    )
    publicado = data_iso(
        primeiro(no, "pubDate", "atom:published", "atom:updated", "dc:date", "published", "updated")
    )
    autor = texto_limpo(primeiro(no, "dc:creator", "author/name", "atom:author/atom:name", "author"), 80)

    # Só há leitor quando o próprio editor sindica o artigo no feed. Quando o
    # feed traz apenas um resumo, é isso que mostramos — ir buscar o texto à
    # página seria contornar uma decisão de quem o publica.
    corpo_limpo = limpar(primeiro(no, "content:encoded", "atom:content"))
    tem_texto = comprimento_texto(corpo_limpo) >= MIN_TEXTO_LEITOR

    return {
        "id": hashlib.sha1(ligacao.encode()).hexdigest()[:16],
        "titulo": titulo,
        "url": ligacao,
        "resumo": texto_limpo(corpo, 260),
        "publicado": publicado,
        "autor": autor,
        "imagem": extrair_imagem(no, corpo),
        "fonte": fonte["id"],
        "fonteNome": fonte["nome"],
        "tipo": fonte["tipo"],
        "tema": fonte.get("tema", ""),
        "temTexto": tem_texto,
        "_corpo": corpo_limpo if tem_texto else "",
    }


# ------------------------------------------------------------------------ recolha

def recolher_fonte(fonte: dict) -> tuple[dict, list[dict]]:
    estado = {
        "id": fonte["id"],
        "nome": fonte["nome"],
        "site": fonte.get("site", ""),
        "feed": fonte["feed"],
        "tipo": fonte["tipo"],
        "tema": fonte.get("tema", ""),
        "descricao": fonte.get("descricao", ""),
        "ok": False,
        # "ok" | "bloqueada" (não dá para recolher, lê-se no site) | "falha" | "inativa"
        "estado": "falha",
        "erro": "",
        "artigos": 0,
    }
    if not fonte.get("ativa", True):
        estado["estado"] = "inativa"
        estado["erro"] = "desativada em fontes.json"
        return estado, []
    try:
        xml_bruto = buscar(fonte["feed"])
        nos = itens_do_feed(xml_bruto)
        if not nos:
            raise ValueError("o feed não devolveu artigos (resposta sem <item>/<entry>)")
        artigos = [a for a in (converter(no, fonte) for no in nos) if a]
        artigos.sort(key=lambda a: a["publicado"] or "", reverse=True)
        artigos = artigos[:MAX_POR_FONTE]
        estado["ok"] = True
        estado["estado"] = "ok"
        estado["artigos"] = len(artigos)
        return estado, artigos
    except urllib.error.HTTPError as erro:
        estado["erro"] = f"HTTP {erro.code}" + (" (bloqueio anti-bot)" if erro.code in (403, 429) else "")
    except Exception as erro:  # noqa: BLE001
        estado["erro"] = str(erro)[:120] or type(erro).__name__
    # Meios que sabemos que bloqueiam a recolha não são uma avaria: são para ler
    # no site. Só passam a "falha" se o bloqueio deixar de estar documentado.
    if fonte.get("bloqueio"):
        estado["estado"] = "bloqueada"
        estado["erro"] = fonte["bloqueio"]
    return estado, []


def aceita_iframe(url: str) -> bool:
    """Verifica se o site deixa ser embebido, lendo os cabeçalhos que o dizem.

    Conservador: qualquer dúvida (erro de rede, cabeçalho estranho) conta como
    não. Isto evita mostrar ao leitor um botão que só daria um painel em branco.
    """
    try:
        pedido = urllib.request.Request(url, headers=CABECALHOS)
        with urllib.request.urlopen(pedido, timeout=15, context=CTX) as resposta:
            if (resposta.headers.get("X-Frame-Options") or "").strip():
                return False
            politica = resposta.headers.get("Content-Security-Policy") or ""
        for directiva in politica.split(";"):
            directiva = directiva.strip()
            if directiva.startswith("frame-ancestors"):
                origens = directiva.split()[1:]
                return "*" in origens
        return True
    except Exception:  # noqa: BLE001
        return False


def deduplicar(artigos: list[dict]) -> list[dict]:
    vistos_url: set[str] = set()
    vistos_titulo: set[str] = set()
    resultado = []
    for artigo in artigos:
        chave_titulo = re.sub(r"[^a-z0-9]+", "", artigo["titulo"].lower())[:70]
        if artigo["url"] in vistos_url or (chave_titulo and chave_titulo in vistos_titulo):
            continue
        vistos_url.add(artigo["url"])
        if chave_titulo:
            vistos_titulo.add(chave_titulo)
        resultado.append(artigo)
    return resultado


def main() -> int:
    analisador = argparse.ArgumentParser(description="Recolhe notícias das fontes configuradas.")
    analisador.add_argument("--verificar", action="store_true",
                            help="só testa os feeds e imprime o estado, sem escrever ficheiros")
    argumentos = analisador.parse_args()

    config = json.loads(FONTES.read_text(encoding="utf-8"))
    fontes = config["fontes"]

    estados: list[dict] = []
    todos: list[dict] = []
    with futures.ThreadPoolExecutor(max_workers=8) as piscina:
        for estado, artigos in piscina.map(recolher_fonte, fontes):
            estados.append(estado)
            todos.extend(artigos)

    limite = datetime.now(timezone.utc) - timedelta(days=DIAS_JANELA)
    agora = datetime.now(timezone.utc)
    recentes = []
    for artigo in todos:
        if not artigo["publicado"]:
            artigo["publicado"] = agora.isoformat()
            artigo["dataEstimada"] = True
        if datetime.fromisoformat(artigo["publicado"]) >= limite:
            recentes.append(artigo)

    recentes.sort(key=lambda a: a["publicado"], reverse=True)
    recentes = deduplicar(recentes)

    estados.sort(key=lambda e: (e["tipo"] != "independente", e["nome"].lower()))
    ok = sum(1 for e in estados if e["ok"])
    for estado in estados:
        marca = {"ok": "  ok", "bloqueada": " site", "inativa": "  --"}.get(estado["estado"], "FALHA")
        print(f"{marca}  {estado['nome']:<24} {estado['artigos']:>3} artigos  {estado['erro']}")
    print(f"\n{ok}/{len(estados)} fontes responderam · {len(recentes)} artigos únicos "
          f"nos últimos {DIAS_JANELA} dias")

    if argumentos.verificar:
        return 0 if ok else 1


    # Um artigo do feed da Mensagem chega a ter 10 mil caracteres. Se fossem
    # todos para o JSON principal, o arranque da app passava a arrastar megabytes
    # que quase ninguém lê. Vão para ficheiros próprios, buscados só ao abrir.
    if PASTA_ARTIGOS.exists():
        for antigo in PASTA_ARTIGOS.glob("*.json"):
            antigo.unlink()
    PASTA_ARTIGOS.mkdir(parents=True, exist_ok=True)

    com_texto = 0
    for artigo in recentes:
        corpo = artigo.pop("_corpo", "")
        if not corpo:
            continue
        (PASTA_ARTIGOS / f"{artigo['id']}.json").write_text(
            json.dumps({"id": artigo["id"], "html": corpo}, ensure_ascii=False,
                       separators=(",", ":")),
            encoding="utf-8",
        )
        com_texto += 1

    # Uma verificação por fonte, não por artigo — mas feita numa página de artigo
    # real: a raiz do site manda cabeçalhos diferentes das páginas de notícia
    # (o Jornal de Negócios deixa embeber a homepage e bloqueia os artigos).
    amostra: dict[str, str] = {}
    for artigo in recentes:
        amostra.setdefault(artigo["fonte"], artigo["url"])

    embebiveis = {}
    with futures.ThreadPoolExecutor(max_workers=8) as piscina:
        for identificador, aceita in zip(amostra, piscina.map(aceita_iframe, amostra.values())):
            embebiveis[identificador] = aceita
    for estado in estados:
        estado["embebivel"] = embebiveis.get(estado["id"], False)

    print(f"{com_texto} artigos com texto completo · "
          f"{sum(embebiveis.values())} fontes aceitam iframe")

    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(
        json.dumps(
            {
                "geradoEm": agora.isoformat(),
                "janelaDias": DIAS_JANELA,
                "fontes": estados,
                "artigos": recentes,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    print(f"Escrito {SAIDA.relative_to(RAIZ)} ({SAIDA.stat().st_size / 1024:.0f} KB)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
