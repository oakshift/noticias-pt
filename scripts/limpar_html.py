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


# --------------------------------------------------------------------- rodapés

# O WordPress cola isto ao fim de tudo o que sai no feed.
_POST_WP = re.compile(r"<p>\s*The post\s+.{0,400}?appeared first on.{0,200}?</p>", re.I | re.S)
_PARTILHA = re.compile(r"facebook|twitter|whatsapp|telegram|linkedin|e-?mail|partilh|share|"
                       r"imprimir|copiar\s+link|subscre|newsletter", re.I)


def _lista_e_so_partilha(bloco: str) -> bool:
    texto = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", bloco)).strip()
    if len(texto) > 220:
        return False                      # lista longa é conteúdo, não barra de partilha
    if not texto:
        return True                       # lista vazia: só ícones que não sobreviveram
    palavras = [p for p in re.split(r"[\s,·|]+", texto) if p]
    return sum(1 for p in palavras if _PARTILHA.search(p)) >= max(1, len(palavras) * 0.5)


def podar_rodape(html: str) -> str:
    """Tira barras de partilha e assinaturas automáticas do fim do artigo."""
    if not html:
        return ""
    html = _POST_WP.sub("", html)
    for bloco in re.findall(r"<(?:ul|ol)\b.*?</(?:ul|ol)>", html, re.S):
        if _lista_e_so_partilha(bloco):
            html = html.replace(bloco, "")
    # Espaço branco a mais só engorda o ficheiro; o browser colapsa-o na mesma.
    html = re.sub(r">\s{2,}<", "><", html)
    html = re.sub(r"(?:\s*<p>\s*(?:&nbsp;|\s)*</p>\s*)+", "", html)
    return html.strip()


def comprimento_texto(html: str) -> int:
    return len(re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html or ""))).strip())
