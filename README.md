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

Os dados (`data/`) não estão no git: são reconstruídos a cada publicação e a
Action guarda-os em cache entre execuções. Depois de clonar, corre a recolha uma
vez antes de abrir a app.

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

Clicar num título abre um leitor lateral com o artigo, em vez de saltar para o
jornal. **74% dos artigos (146 em 195)** têm lá o texto completo.

O texto vem por duas vias. Cinco fontes publicam o artigo inteiro no próprio RSS
(Mensagem de Lisboa, Gerador, DN, ECO, Jornal Económico) e essas saem de graça.
Para as outras, a recolha vai à página buscar o corpo, com o extractor em
[scripts/extrair\_artigo.py](scripts/extrair_artigo.py).

Nenhum método de extracção serve todos os sites, por isso são três, por ordem de
fiabilidade: `articleBody` do **JSON-LD** quando existe (o site diz onde está o
texto); senão **pontuação por densidade**, uma variante do algoritmo do
Readability em que ganha o contentor com mais texto em parágrafos e menos
ligações; senão nada, e fica-se pelo resumo.

Quando falta o texto, a app diz **porquê** — e o motivo é registado na recolha,
não inventado na interface:

| Motivo | Onde acontece |
|---|---|
| o artigo está reservado a assinantes | Jornal de Negócios (8 de 19) |
| o site recusou a leitura automática | PÚBLICO (bloqueio anti-bot em todas) |
| a página não traz mais texto do que este resumo | RTP (boletins de rádio, notícias curtas) |

Onde o site aceita iframe (**PÚBLICO, Observador, Shifter, Gerador, Jornal
MAPA**) há ainda um botão "Ler aqui dentro". Isso é medido a cada recolha nos
cabeçalhos `X-Frame-Options` e `frame-ancestors` de uma página de artigo real,
não da raiz do site, que costuma responder outra coisa.

### Como a recolha se porta

- **Respeita o `robots.txt`** de cada site, e o `crawl-delay` que ele declara
  (a BUALA pede 10 s, o Gerador 3 s). Um site de cada vez por servidor, sites
  diferentes em paralelo.
- **Guarda em cache** o que já extraiu. A primeira execução leva ~90 s e vai à
  rede ~150 vezes; as seguintes levam ~11 s e só buscam os artigos novos.
- **Não repete o que falhou.** Um artigo sem texto fica registado 12 horas antes
  de se tentar outra vez, senão eram 50 pedidos inúteis de meia em meia hora.
- **Sanitiza tudo** por lista branca em [scripts/limpar\_html.py](scripts/limpar_html.py),
  na recolha e não no browser: é HTML de terceiros a ser injectado na página.
  Barras de partilha e rodapés automáticos do WordPress são podados.

Cada artigo vai para `data/artigos/<id>.json` e só é buscado ao abrir, para o
arranque continuar a carregar ~140 KB em vez de mais de um megabyte.

Para saltar a extracção e ficar só pelo que os feeds dão:

```bash
python3 scripts/recolher.py --so-feeds
```

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
