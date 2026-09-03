"""Extrai o corpo de uma notícia a partir do HTML da página.

Nenhum método serve todos os sites: o JSON-LD só existe nalguns, e a etiqueta
<article> tanto envolve o artigo como a página inteira com comentários. Por
isso há três vias, por ordem de fiabilidade:

  1. JSON-LD `articleBody` — o próprio site diz onde está o texto;
  2. pontuação por densidade — variante do algoritmo do Readability: ganha o
     contentor com mais texto em parágrafos e menos ligações;
  3. nada — devolve vazio, e quem chama fica-se pelo resumo do feed.

O resultado passa sempre pelo sanitizador de lista branca antes de sair.
"""
from __future__ import annotations

import json
import re
from html import escape, unescape
from html.parser import HTMLParser

from limpar_html import comprimento_texto, limpar, podar_rodape

VAZIAS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
          "meta", "param", "source", "track", "wbr"}
# Nunca fazem parte do corpo de um artigo.
FORA = {"script", "style", "noscript", "nav", "aside", "footer", "header", "form",
        "iframe", "svg", "canvas", "button", "select", "textarea", "template",
        "video", "audio", "object", "embed"}
# Etiquetas que fecham sozinhas quando aparece outra igual ou de bloco.
AUTO_FECHO = {"p": {"p", "div", "section", "article", "ul", "ol", "h1", "h2", "h3", "h4"},
              "li": {"li"}, "td": {"td", "tr"}, "th": {"th", "tr"}, "tr": {"tr"},
              "option": {"option"}, "dd": {"dd", "dt"}, "dt": {"dd", "dt"}}

BOM_NOME = re.compile(r"artic|body|content|entry|post|text|story|main|corpo|noticia|conteudo", re.I)
MAU_NOME = re.compile(r"coment|share|partilh|footer|rodape|nav|menu|sidebar|barra|relacion|"
                      r"related|promo|publicid|advert|banner|newsletter|subscri|tag|social|"
                      r"cookie|popup|modal|breadcrumb|autor-box|leia-tambem|mais-lidas", re.I)


class No:
    __slots__ = ("etiqueta", "atributos", "filhos", "pai", "texto", "pontos")

    def __init__(self, etiqueta="", atributos=None, pai=None):
        self.etiqueta = etiqueta
        self.atributos = atributos or {}
        self.filhos: list[No] = []
        self.pai = pai
        self.texto = ""
        self.pontos = 0.0

    @property
    def nome(self) -> str:
        return f"{self.atributos.get('class', '')} {self.atributos.get('id', '')}"

    def descendentes(self):
        for filho in self.filhos:
            yield filho
            yield from filho.descendentes()


class Arvore(HTMLParser):
    """Constrói uma árvore tolerante a HTML mal formado (que é quase todo)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.raiz = No("#raiz")
        self.actual = self.raiz

    def handle_starttag(self, etiqueta, atributos):
        fecha = AUTO_FECHO.get(self.actual.etiqueta)
        if fecha and etiqueta in fecha:
            self.actual = self.actual.pai or self.raiz
        no = No(etiqueta, {c: (v or "") for c, v in atributos}, self.actual)
        self.actual.filhos.append(no)
        if etiqueta not in VAZIAS:
            self.actual = no

    def handle_startendtag(self, etiqueta, atributos):
        self.actual.filhos.append(No(etiqueta, {c: (v or "") for c, v in atributos}, self.actual))

    def handle_endtag(self, etiqueta):
        no = self.actual
        while no is not self.raiz:
            if no.etiqueta == etiqueta:
                self.actual = no.pai or self.raiz
                return
            no = no.pai or self.raiz

    def handle_data(self, dados):
        if dados.strip():
            folha = No("#texto", pai=self.actual)
            folha.texto = dados
            self.actual.filhos.append(folha)


def texto_de(no: No) -> str:
    if no.etiqueta == "#texto":
        return no.texto
    if no.etiqueta in FORA:
        return ""
    return "".join(texto_de(filho) for filho in no.filhos)


def comprimento(no: No) -> int:
    return len(re.sub(r"\s+", " ", texto_de(no)).strip())


def densidade_de_ligacoes(no: No) -> float:
    total = comprimento(no)
    if not total:
        return 1.0
    ligado = sum(comprimento(d) for d in no.descendentes() if d.etiqueta == "a")
    return min(ligado / total, 1.0)


def serializar(no: No) -> str:
    if no.etiqueta == "#texto":
        return escape(no.texto, quote=False)
    if no.etiqueta in FORA or no.etiqueta == "#raiz":
        return "".join(serializar(f) for f in no.filhos) if no.etiqueta == "#raiz" else ""
    atributos = "".join(f' {c}="{escape(v, quote=True)}"'
                        for c, v in no.atributos.items() if c in ("href", "src", "alt", "title"))
    dentro = "".join(serializar(f) for f in no.filhos)
    if no.etiqueta in VAZIAS:
        return f"<{no.etiqueta}{atributos}>"
    return f"<{no.etiqueta}{atributos}>{dentro}</{no.etiqueta}>"


# ------------------------------------------------------------------- via 1: JSON-LD

def _do_json_ld(pagina: str) -> str:
    melhor = ""
    for bloco in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', pagina, re.S):
        try:
            dados = json.loads(bloco.strip())
        except Exception:  # noqa: BLE001
            continue
        fila = [dados]
        while fila:
            no = fila.pop()
            if isinstance(no, dict):
                corpo = no.get("articleBody")
                if isinstance(corpo, str) and len(corpo) > len(melhor):
                    melhor = corpo
                fila += [v for v in no.values() if isinstance(v, (dict, list))]
            elif isinstance(no, list):
                fila += no
    if len(melhor) < 400:
        return ""
    paragrafos = [p.strip() for p in re.split(r"\n{1,}|(?<=[.!?])\s{2,}", unescape(melhor)) if p.strip()]
    return "".join(f"<p>{escape(p, quote=False)}</p>" for p in paragrafos)


# --------------------------------------------------- via 2: pontuação por densidade

def _pontuar(raiz: No) -> No | None:
    candidatos: dict[int, No] = {}
    for no in raiz.descendentes():
        if no.etiqueta not in ("p", "pre", "td", "blockquote"):
            continue
        conteudo = re.sub(r"\s+", " ", texto_de(no)).strip()
        if len(conteudo) < 25:
            continue
        pontos = 1 + conteudo.count(",") + conteudo.count(";") + min(len(conteudo) / 100, 3)
        for nivel, ascendente in enumerate((no.pai, no.pai.pai if no.pai else None)):
            if ascendente is None or ascendente.etiqueta in ("#raiz", "html", "body"):
                continue
            if id(ascendente) not in candidatos:
                base = 0.0
                if BOM_NOME.search(ascendente.nome):
                    base += 25
                if MAU_NOME.search(ascendente.nome):
                    base -= 25
                if ascendente.etiqueta in ("article", "main"):
                    base += 15
                ascendente.pontos = base
                candidatos[id(ascendente)] = ascendente
            ascendente.pontos += pontos / (nivel + 1)

    melhor, melhor_pontos = None, 0.0
    for no in candidatos.values():
        pontos = no.pontos * (1 - densidade_de_ligacoes(no))
        if pontos > melhor_pontos:
            melhor, melhor_pontos = no, pontos
    return melhor


def _limpar_ramo(no: No) -> None:
    """Poda o que sobrou dentro do contentor escolhido."""
    for filho in list(no.filhos):
        if filho.etiqueta in FORA or MAU_NOME.search(filho.nome):
            no.filhos.remove(filho)
            continue
        # Listas e caixas que são só ligações são navegação, não texto.
        if filho.etiqueta in ("ul", "ol", "div", "section") and comprimento(filho) > 0:
            if densidade_de_ligacoes(filho) > 0.6 and comprimento(filho) < 400:
                no.filhos.remove(filho)
                continue
        _limpar_ramo(filho)


def extrair(pagina: str, mínimo: int = 600) -> tuple[str, str]:
    """Devolve (html_do_corpo, via_usada). Vazio se não houver nada de jeito."""
    if not pagina or len(pagina) < 500:
        return "", "página vazia"

    do_ld = podar_rodape(limpar(_do_json_ld(pagina)))
    if comprimento_texto(do_ld) >= mínimo:
        return do_ld, "json-ld"

    try:
        arvore = Arvore()
        arvore.feed(pagina)
        arvore.close()
    except Exception:  # noqa: BLE001
        return (do_ld, "json-ld") if do_ld else ("", "html ilegível")

    melhor = _pontuar(arvore.raiz)
    if melhor is None:
        return (do_ld, "json-ld") if do_ld else ("", "sem candidato")

    # Muitos sites partem o artigo por vários contentores irmãos (o Negócios
    # corta a meio para a pré-visualização do paywall). Recolhem-se os irmãos
    # que também pontuaram, ou que são parágrafos de texto corrido.
    pai = melhor.pai
    limiar = max(10.0, melhor.pontos * 0.2)
    escolhidos = [melhor]
    if pai is not None:
        escolhidos = [
            no for no in pai.filhos
            if no is melhor
            or no.pontos >= limiar
            or (no.etiqueta in ("p", "blockquote", "figure", "h2", "h3")
                and comprimento(no) > 60 and densidade_de_ligacoes(no) < 0.3)
        ] or [melhor]

    for no in escolhidos:
        _limpar_ramo(no)
    por_densidade = podar_rodape(limpar("".join(serializar(no) for no in escolhidos)))

    # Fica o mais completo dos dois, desde que passe o mínimo.
    if comprimento_texto(por_densidade) >= max(mínimo, comprimento_texto(do_ld)):
        return por_densidade, "densidade"
    if comprimento_texto(do_ld) >= mínimo:
        return do_ld, "json-ld"
    return "", "texto insuficiente"
