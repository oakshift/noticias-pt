# Notícias.pt

Agregador de imprensa **independente portuguesa**, com meios de referência como contraponto.
Site estático, sem dependências, sem rastreio, sem algoritmo — a ordem é cronológica e ponto.

## Como funciona

Não há servidor. O RSS não pode ser lido directamente do browser (CORS), por isso:

1. Uma **GitHub Action** corre de 30 em 30 minutos, vai buscar os feeds e escreve `data/noticias.json`.
2. O mesmo passo publica o site no **GitHub Pages**.
3. O browser só lê esse JSON. Rápido, barato, e funciona offline depois da primeira visita.

```
fontes.json           lista de meios (edita aqui para adicionar/remover)
scripts/recolher.py   recolha dos feeds -> data/noticias.json   (só stdlib)
index.html            a app
assets/               estilos e lógica
sw.js                 service worker (leitura offline)
```

## Correr localmente

```bash
python3 scripts/recolher.py     # actualiza data/noticias.json
python3 -m http.server 8000     # abre http://localhost:8000
```

Abrir o `index.html` directamente também funciona na maioria dos browsers, mas o
service worker e a cache só funcionam por HTTP.

Para testar só os feeds, sem escrever nada:

```bash
python3 scripts/recolher.py --verificar
```

## Ver a app a funcionar

```bash
python3 scripts/testar_no_browser.py            # a app, já carregada
python3 scripts/testar_no_browser.py --leitor   # abre um artigo e testa fechá-lo
```

Abre a app num Firefox headless e grava uma fotografia em
`~/noticias-teste-browser/`, com uma faixa no fundo que fica **verde** se não
houve erros de JavaScript e **vermelha** se houve.

Isto existe porque inspeccionar ficheiros não chega. O painel de leitura esteve
uma versão inteira visível por cima da página, sem forma de o fechar, e nenhuma
verificação de CSS ou de JS apanhou: `[hidden]` vem da folha do *user-agent* e
perde para qualquer `display:` do autor, e `.leitor` traz `display: flex`. Só
abrir a página no browser mostrou isso.

## Adicionar uma fonte

Junta uma entrada a `fontes.json`:

```json
{
  "id": "identificador-curto",
  "nome": "Nome do meio",
  "feed": "https://exemplo.pt/feed/",
  "site": "https://exemplo.pt",
  "tipo": "independente",
  "tema": "investigação",
  "descricao": "Uma linha sobre o meio.",
  "ativa": true
}
```

`tipo` só tem dois valores: `independente` (conta para o filtro "só independentes")
ou `referência`. Corre `--verificar` para confirmar que o feed responde antes de
fazer commit.

O `data/noticias.json` no repositório é só uma semente para o site funcionar mal
se clona — quem manda é o ficheiro que a Action gera a cada publicação.

## Relógios adiantados

A RTP data os boletins de rádio pela hora de emissão, cerca de uma hora no
futuro. Sem correcção esses artigos colam-se ao topo durante uma hora, por cima
de notícias acabadas de sair, e mostram todos "agora mesmo". Limitá-los ao
presente não resolve — só os empata lá em cima.

A recolha detecta o desvio por fonte (se o artigo mais recente de uma fonte está
no futuro, a fonte está adiantada nessa medida) e recua a fonte inteira, o que a
põe no sítio certo em relação às outras e preserva a ordem interna. O que o feed
dizia fica em `publicadoFeed`, e a app mostra-o ao passar o rato pela data.

## Fontes que não deixam recolher

Alguns meios estão atrás de Cloudflare, limitam pedidos automáticos, ou
simplesmente não publicam RSS. Contornar isso seria passar por cima de uma
escolha do site, por isso não se faz. Em vez disso documenta-se:

```json
"bloqueio": "Bloqueia a recolha automática (Cloudflare)."
```

Uma fonte com `bloqueio` continua a ser tentada em cada recolha — se abrirem o
feed, entra sozinha. Enquanto falhar, aparece na barra lateral em **"Sem feed —
ler no site"**, com ligação directa, em vez de um erro. O aviso vermelho no topo
fica reservado a avarias inesperadas, que é o que merece atenção.

À data de construção: **Fumaça** (403), **PÁGINA UM** (429) e **Setenta e
Quatro** (não publica RSS). Os runners do GitHub têm outro IP, por isso podem
correr melhor ou pior do que a tua máquina.

## Ler dentro da app

Clicar num título abre um leitor lateral em vez de saltar para o jornal. O que
lá aparece depende do que **o editor decidiu sindicar no feed**, e há três casos:

| | O que o leitor mostra |
|---|---|
| O feed traz o artigo inteiro | O texto completo, tipografado para leitura |
| O feed traz só o resumo | O resumo, e o porquê, com ligação ao original |
| ...e o site aceita iframe | Um botão extra, "Ler aqui dentro", que embebe a página |

Neste momento publicam o artigo completo no feed: **Mensagem de Lisboa, Gerador,
DN, ECO e Jornal Económico** (~50 artigos de cada vez). Aceitam iframe:
**PÚBLICO, Observador, Shifter, Gerador e Jornal MAPA** — medido a cada recolha
nos cabeçalhos `X-Frame-Options` e `frame-ancestors` de uma página de artigo
real, não da raiz do site, que costuma responder outra coisa.

O que **não** se faz é ir buscar o texto à página de quem só sindica o resumo.
Isso seria contornar uma decisão de quem publica — e são meios que vivem de
assinaturas e donativos.

O corpo dos artigos é sanitizado por lista branca em [scripts/limpar_html.py](scripts/limpar_html.py)
durante a recolha, não no browser: o HTML dos feeds é conteúdo de terceiros a ser
injectado na página. Cada artigo vai para `data/artigos/<id>.json` e só é buscado
ao abrir — assim o arranque continua a carregar ~140 KB em vez de megabytes.

## Atalhos

| Atalho | Acção |
|---|---|
| `/` | pesquisar · `Enter` abre o primeiro resultado |
| `i` | alternar "só independentes" |
| `t` | tema claro/escuro |
| `r` | ir buscar dados novos |
| `Esc` | fechar o leitor, ou limpar a pesquisa |

Com o leitor aberto: `j`/`k` (ou `↓`/`↑`) saltam de artigo, `s` guarda,
`o` abre o original. `⌘`/`Ctrl`+clique num título continua a ir direito ao jornal.

Guardados e lidos ficam no `localStorage` — só neste browser, nunca saem daqui.

## Créditos e uso justo

Só se guardam título, resumo curto e ligação, como qualquer leitor de RSS.
Cada artigo liga sempre ao original. Se lês estes meios com regularidade,
assina-os: quase todos vivem de leitores.
