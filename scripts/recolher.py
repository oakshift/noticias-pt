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
import urllib.robotparser
import threading
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extrair_artigo import extrair  # noqa: E402
from limpar_html import comprimento_texto, limpar, podar_rodape  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent
FONTES = RAIZ / "fontes.json"
SAIDA = RAIZ / "data" / "noticias.json"
PASTA_ARTIGOS = RAIZ / "data" / "artigos"

# Abaixo disto o "corpo" do feed é só um resumo repetido, não vale um leitor.
MIN_TEXTO_LEITOR = 1200
# Da página aceitamos menos: uma notícia curta da RTP tem 700 caracteres e é o
# artigo inteiro. O que se exige é que traga mesmo mais do que o resumo do feed.
MIN_TEXTO_PAGINA = 500
# Se o feed já traz isto, não vale a pena pedir a página: é o mesmo texto.
FEED_JA_CHEGA = 1800
ATRASO_PADRAO = 1.0          # segundos entre pedidos ao mesmo site
ATRASO_MAXIMO = 10.0         # tecto para um crawl-delay exagerado
MAX_SITES_EM_PARALELO = 6

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
    corpo_limpo = podar_rodape(limpar(primeiro(no, "content:encoded", "atom:content")))
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


def _robots_de(base: str) -> tuple[urllib.robotparser.RobotFileParser | None, float]:
    leitor = urllib.robotparser.RobotFileParser()
    leitor.set_url(base + "/robots.txt")
    try:
        pedido = urllib.request.Request(base + "/robots.txt", headers=CABECALHOS)
        with urllib.request.urlopen(pedido, timeout=15, context=CTX) as resposta:
            leitor.parse(resposta.read().decode("utf-8", "replace").splitlines())
    except Exception:  # noqa: BLE001 - sem robots.txt não há restrição declarada
        return None, ATRASO_PADRAO
    atraso = leitor.crawl_delay(UA) or leitor.crawl_delay("*") or ATRASO_PADRAO
    return leitor, min(float(atraso), ATRASO_MAXIMO)


def _texto_da_pagina(url: str) -> str:
    pedido = urllib.request.Request(url, headers={**CABECALHOS,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    with urllib.request.urlopen(pedido, timeout=30, context=CTX) as resposta:
        bruto = resposta.read()
        codificacao = (resposta.headers.get("Content-Encoding") or "").lower()
        if codificacao == "gzip":
            bruto = gzip.decompress(bruto)
        elif codificacao == "deflate":
            bruto = zlib.decompress(bruto, -zlib.MAX_WBITS)
        return bruto.decode(resposta.headers.get_content_charset() or "utf-8", "replace")


SEM_TEXTO = RAIZ / "data" / "sem-texto.json"
HORAS_ATE_TENTAR_OUTRA_VEZ = 12


# Frases inteiras, não palavras soltas: "premium" e "assinante" aparecem em
# anúncios e menus de sites sem paywall nenhum, e marcavam a RTP como paga.
PAYWALL = re.compile(
    r"exclusiv[ao] (?:para|a) assinantes|reservad[ao] a(?:os)? assinantes|"
    r"funcionalidade exclusiva para assinantes|torne-se assinante|"
    r"(?:subscreva|assine)[^.<]{0,40}para (?:continuar|ler)|"
    r"para continuar a ler[^.<]{0,30}(?:assine|subscreva|sess[ãa]o)|"
    r"artigo (?:exclusivo|reservado)|conte[úu]do exclusivo para", re.I)


def _carregar_sem_texto() -> dict[str, dict]:
    """Artigos onde já se foi à página e não havia texto de jeito.

    Sem isto, os mesmos 60 artigos sem corpo eram pedidos de meia em meia hora,
    para todo o sempre. Ao fim de algumas horas volta-se a tentar, porque há
    sites que publicam a notícia curta e completam-na depois.
    """
    try:
        registo = json.loads(SEM_TEXTO.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    corte = (datetime.now(timezone.utc) - timedelta(hours=HORAS_ATE_TENTAR_OUTRA_VEZ)).isoformat()
    return {identificador: nota for identificador, nota in registo.items()
            if isinstance(nota, dict) and nota.get("quando", "") > corte}


def buscar_corpos(artigos: list[dict], cache: dict[str, str],
                  ja_sem_texto: dict[str, dict]) -> tuple[dict[str, int], dict[str, str]]:
    """Vai buscar o texto completo às páginas dos artigos que o feed não traz.

    Um site de cada vez por servidor, respeitando o robots.txt e o crawl-delay
    que ele declara; sites diferentes em paralelo. O que já está em cache de uma
    execução anterior não é pedido outra vez — em regime de cruzeiro só entram
    os artigos novos.
    """
    sem_texto: dict[str, str] = {}
    por_site: dict[str, list[dict]] = {}
    for artigo in artigos:
        if artigo["id"] in cache or artigo.get("_corpo") or artigo["id"] in ja_sem_texto:
            continue
        partes = urllib.parse.urlsplit(artigo["url"])
        por_site.setdefault(f"{partes.scheme}://{partes.netloc}", []).append(artigo)

    contas = {"extraídos": 0, "em cache": len(cache), "sem texto (recente)": len(ja_sem_texto),
              "robots": 0, "falhados": 0, "sem texto": 0}
    if not por_site:
        return contas, sem_texto
    bloqueio = threading.Lock()

    def tratar_site(base: str) -> None:
        regras, atraso = _robots_de(base)
        for indice, artigo in enumerate(por_site[base]):
            if regras is not None and not regras.can_fetch(UA, artigo["url"]):
                with bloqueio:
                    contas["robots"] += 1
                    sem_texto[artigo["id"]] = "o robots.txt do site não permite a leitura automática"
                continue
            if indice:
                time.sleep(atraso)
            try:
                pagina = _texto_da_pagina(artigo["url"])
            except urllib.error.HTTPError as erro:
                with bloqueio:
                    contas["falhados"] += 1
                    sem_texto[artigo["id"]] = (
                        "o site recusou a leitura automática" if erro.code in (202, 403, 429)
                        else f"a página respondeu HTTP {erro.code}")
                continue
            except Exception:  # noqa: BLE001
                with bloqueio:
                    contas["falhados"] += 1
                    sem_texto[artigo["id"]] = "não foi possível chegar à página"
                continue

            # Um "200 OK" com 1 KB não é a notícia: é uma página de bloqueio
            # anti-bot que não se dá ao trabalho de devolver um código de erro.
            if len(pagina) < 2500:
                with bloqueio:
                    contas["falhados"] += 1
                    sem_texto[artigo["id"]] = "o site recusou a leitura automática"
                continue

            corpo, _via = extrair(pagina)
            tamanho = comprimento_texto(corpo)
            with bloqueio:
                # Vale a pena se for artigo a sério e acrescentar ao resumo.
                if tamanho >= MIN_TEXTO_PAGINA and tamanho > len(artigo["resumo"]) * 1.5:
                    artigo["_corpo"] = corpo
                    contas["extraídos"] += 1
                else:
                    contas["sem texto"] += 1
                    sem_texto[artigo["id"]] = (
                        "o artigo está reservado a assinantes" if PAYWALL.search(pagina[:200_000])
                        else "a página não traz mais texto do que este resumo")

    with futures.ThreadPoolExecutor(max_workers=MAX_SITES_EM_PARALELO) as piscina:
        list(piscina.map(tratar_site, por_site))
    return contas, sem_texto


def corrigir_relogios_adiantados(artigos: list[dict], agora: datetime) -> None:
    """Endireita fontes cujo relógio está à frente do nosso.

    A RTP data os boletins pela hora de emissão, que fica cerca de uma hora no
    futuro. Sem correcção, esses artigos ficam colados ao topo durante uma hora,
    por cima de notícias que saíram mesmo agora — e limitá-los ao presente só os
    empata todos lá em cima.

    A regra: se o artigo mais recente de uma fonte está no futuro, é porque a
    fonte está adiantada nessa medida. Recua-se a fonte inteira por esse desvio,
    o que a põe no sítio certo em relação às outras e preserva a ordem interna.
    O que o feed dizia fica em "publicadoFeed" e a app mostra-o ao passar o rato.
    """
    por_fonte: dict[str, list[dict]] = {}
    for artigo in artigos:
        por_fonte.setdefault(artigo["fonte"], []).append(artigo)

    for daquela_fonte in por_fonte.values():
        datas = [datetime.fromisoformat(a["publicado"]) for a in daquela_fonte]
        desvio = max(datas) - agora
        if desvio.total_seconds() <= 60:      # margem para relógios normais
            continue
        for artigo, data in zip(daquela_fonte, datas):
            artigo["publicadoFeed"] = artigo["publicado"]
            artigo["publicado"] = (data - desvio).isoformat()


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
    analisador.add_argument("--so-feeds", action="store_true", dest="so_feeds",
                            help="não vai buscar o texto às páginas; fica-se pelo que o feed traz")
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

    corrigir_relogios_adiantados(recentes, agora)
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

    # Corpos extraídos em execuções anteriores. A Action guarda esta pasta em
    # cache, por isso em regime de cruzeiro só se vai à rede pelos artigos novos.
    cache: dict[str, str] = {}
    if PASTA_ARTIGOS.exists():
        for ficheiro in PASTA_ARTIGOS.glob("*.json"):
            try:
                cache[ficheiro.stem] = json.loads(ficheiro.read_text(encoding="utf-8"))["html"]
            except Exception:  # noqa: BLE001 - ficheiro corrompido volta a ser buscado
                ficheiro.unlink(missing_ok=True)

    if argumentos.so_feeds:
        print("--so-feeds: não se vai buscar o texto às páginas")
    else:
        ja_sem_texto = _carregar_sem_texto()
        contas, novos_sem_texto = buscar_corpos(recentes, cache, ja_sem_texto)
        print("corpos: " + " · ".join(f"{valor} {chave}" for chave, valor in contas.items() if valor))
        agora_iso = agora.isoformat()
        ja_sem_texto.update({identificador: {"quando": agora_iso, "motivo": motivo}
                             for identificador, motivo in novos_sem_texto.items()})
        for artigo in recentes:
            nota = ja_sem_texto.get(artigo["id"])
            if nota:
                artigo["semTexto"] = nota["motivo"]
        actuais_ids = {a["id"] for a in recentes}
        SEM_TEXTO.parent.mkdir(parents=True, exist_ok=True)
        SEM_TEXTO.write_text(json.dumps(
            {k: v for k, v in ja_sem_texto.items() if k in actuais_ids},
            ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # Um artigo do feed da Mensagem chega a ter 10 mil caracteres. Se fossem
    # todos para o JSON principal, o arranque da app passava a arrastar megabytes
    # que quase ninguém lê. Vão para ficheiros próprios, buscados só ao abrir.
    PASTA_ARTIGOS.mkdir(parents=True, exist_ok=True)
    actuais = {a["id"] for a in recentes}
    for antigo in PASTA_ARTIGOS.glob("*.json"):
        if antigo.stem not in actuais:      # artigo saiu da janela
            antigo.unlink()

    com_texto = 0
    for artigo in recentes:
        corpo = artigo.pop("_corpo", "") or cache.get(artigo["id"], "")
        artigo["temTexto"] = comprimento_texto(corpo) >= MIN_TEXTO_PAGINA
        if not artigo["temTexto"]:
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
