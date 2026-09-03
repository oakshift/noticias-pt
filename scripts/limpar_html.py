"""Sanitizador de HTML por lista branca.

O corpo dos artigos vem dos feeds e é injectado na página, por isso passa por
aqui primeiro. Lista branca, não lista negra: o que não está explicitamente
permitido perde a etiqueta (o texto fica). Corre na recolha, não no browser —
o que chega ao cliente já vem limpo.
"""
from __future__ import annotations

import re
from html import escape, unescape
from html.parser import HTMLParser

ETIQUETAS = {
    "p", "br", "h2", "h3", "h4", "h5", "ul", "ol", "li", "blockquote",
    "strong", "b", "em", "i", "u", "a", "img", "figure", "figcaption",
    "hr", "pre", "code", "sup", "sub", "table", "thead", "tbody", "tr", "th", "td",
}
# Etiquetas cujo conteúdo inteiro é deitado fora, não só a etiqueta.
DESCARTAR_CONTEUDO = {
    "script", "style", "iframe", "object", "embed", "form", "input", "button",
    "select", "textarea", "noscript", "svg", "canvas", "video", "audio", "template",
}
VAZIAS = {"br", "img", "hr"}
ATRIBUTOS = {"a": {"href", "title"}, "img": {"src", "alt"}}
ESQUEMAS = ("http://", "https://", "mailto:", "//")

MAX_CARACTERES = 80_000


class _Limpador(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.partes: list[str] = []
        self.abertas: list[str] = []
        self.descartar = 0

    def handle_starttag(self, etiqueta: str, atributos) -> None:
        if etiqueta in DESCARTAR_CONTEUDO:
            self.descartar += 1
            return
        if self.descartar or etiqueta not in ETIQUETAS:
            return
        permitidos = ATRIBUTOS.get(etiqueta, set())
        saida = []
        for nome, valor in atributos:
            if nome not in permitidos or not valor:
                continue
            if nome in ("href", "src") and not valor.lower().startswith(ESQUEMAS):
                continue  # bloqueia javascript:, data:, vbscript:
            saida.append(f' {nome}="{escape(valor, quote=True)}"')
        if etiqueta == "a":
            saida.append(' target="_blank" rel="noopener noreferrer nofollow"')
        elif etiqueta == "img":
            saida.append(' loading="lazy" referrerpolicy="no-referrer"')
        if etiqueta in VAZIAS:
            self.partes.append(f"<{etiqueta}{''.join(saida)}>")
        else:
            self.partes.append(f"<{etiqueta}{''.join(saida)}>")
            self.abertas.append(etiqueta)

    def handle_endtag(self, etiqueta: str) -> None:
        if etiqueta in DESCARTAR_CONTEUDO:
            self.descartar = max(0, self.descartar - 1)
            return
        if self.descartar or etiqueta in VAZIAS or etiqueta not in ETIQUETAS:
            return
        if etiqueta in self.abertas:
            # Fecha tudo o que ficou aberto por dentro (HTML dos feeds é irregular).
            while self.abertas:
                aberta = self.abertas.pop()
                self.partes.append(f"</{aberta}>")
                if aberta == etiqueta:
                    break

    def handle_data(self, dados: str) -> None:
        if not self.descartar:
            self.partes.append(escape(dados, quote=False))

    def resultado(self) -> str:
        while self.abertas:
            self.partes.append(f"</{self.abertas.pop()}>")
        return "".join(self.partes)


def limpar(bruto: str) -> str:
    if not bruto:
        return ""
    limpador = _Limpador()
    try:
        limpador.feed(bruto)
        limpador.close()
        html = limpador.resultado()
    except Exception:  # noqa: BLE001 - feed malformado não pode derrubar a recolha
        return ""
    html = re.sub(r"(?:\s*<p>\s*</p>\s*)+", "", html)
    html = re.sub(r"\n{3,}", "\n\n", html).strip()
    return html[:MAX_CARACTERES]


def comprimento_texto(html: str) -> int:
    return len(re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html or ""))).strip())
