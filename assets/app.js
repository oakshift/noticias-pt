/* Notícias PT — agregador da imprensa independente portuguesa.
   Sem dependências, sem rastreio. Todo o estado do utilizador vive no localStorage. */
(() => {
  "use strict";

  const FICHEIRO = "./data/noticias.json";
  const CHAVES = { guardados: "noticias:guardados", lidos: "noticias:lidos", aspeto: "noticias:aspeto" };

  const estado = {
    dados: null,
    vista: "tudo",
    fonte: null,
    assunto: null,
    soIndependentes: false,
    pesquisa: "",
    guardados: carregarConjunto(CHAVES.guardados),
    lidos: carregarConjunto(CHAVES.lidos),
    aberto: null,          // artigo no leitor
    visiveis: [],          // lista filtrada actual, para o ↑/↓ do leitor
  };

  const corposEmCache = new Map();

  const $ = (sel) => document.querySelector(sel);
  const el = {
    artigos: $("#artigos"), avisos: $("#avisos"), fontes: $("#lista-fontes"),
    temas: $("#lista-temas"), pesquisa: $("#pesquisa"), carimbo: $("#carimbo"),
    rodape: $("#rodape-estado"), indep: $("#btn-indep"),
    noSite: $("#grupo-no-site"), listaNoSite: $("#lista-no-site"),
    leitor: $("#leitor"), leitorFundo: $("#leitor-fundo"), leitorCorpo: $("#leitor-corpo"),
    leitorOriginal: $("#leitor-original"), leitorGuardar: $("#leitor-guardar"),
  };

  // ------------------------------------------------------------ persistência
  function carregarConjunto(chave) {
    try { return new Set(JSON.parse(localStorage.getItem(chave) || "[]")); }
    catch { return new Set(); }
  }
  function guardarConjunto(chave, conjunto) {
    try { localStorage.setItem(chave, JSON.stringify([...conjunto].slice(-4000))); }
    catch { /* modo privado, quota cheia — a app continua a funcionar */ }
  }

  // ----------------------------------------------------------------- formato
  const fmtDia = new Intl.DateTimeFormat("pt-PT", { weekday: "long", day: "numeric", month: "long" });
  const fmtHora = new Intl.DateTimeFormat("pt-PT", { hour: "2-digit", minute: "2-digit" });

  function relativo(iso) {
    const minutos = Math.round((Date.now() - new Date(iso)) / 60000);
    if (minutos < 1) return "agora mesmo";
    if (minutos < 60) return `há ${minutos} min`;
    const horas = Math.round(minutos / 60);
    if (horas < 24) return `há ${horas} h`;
    const dias = Math.round(horas / 24);
    if (dias === 1) return "ontem";
    if (dias < 7) return `há ${dias} dias`;
    return fmtDia.format(new Date(iso));
  }

  // Nos cartões o dia já está no cabeçalho do grupo, por isso o que falta é a hora.
  function quando(iso) {
    const dias = (Date.now() - new Date(iso)) / 864e5;
    return dias < 7 ? relativo(iso) : fmtHora.format(new Date(iso));
  }

  function rotuloDia(iso) {
    const data = new Date(iso);
    const hoje = new Date();
    const chave = (d) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
    if (chave(data) === chave(hoje)) return "Hoje";
    const ontem = new Date(hoje); ontem.setDate(hoje.getDate() - 1);
    if (chave(data) === chave(ontem)) return "Ontem";
    return fmtDia.format(data);
  }

  const escapar = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const normalizar = (s) => String(s ?? "").toLowerCase()
    .normalize("NFD").replace(/[\u0300-\u036f]/g, "");

  // ---------------------------------------------------------------- filtros
  function filtrar() {
    const artigos = estado.dados?.artigos ?? [];
    const termos = normalizar(estado.pesquisa).split(/\s+/).filter(Boolean);
    return artigos.filter((a) => {
      if (estado.soIndependentes && a.tipo !== "independente") return false;
      if (estado.fonte && a.fonte !== estado.fonte) return false;
      if (estado.assunto && a.tema !== estado.assunto) return false;
      if (estado.vista === "guardados" && !estado.guardados.has(a.id)) return false;
      if (estado.vista === "novos" && estado.lidos.has(a.id)) return false;
      if (termos.length) {
        const alvo = normalizar(`${a.titulo} ${a.resumo} ${a.fonteNome} ${a.autor}`);
        if (!termos.every((t) => alvo.includes(t))) return false;
      }
      return true;
    });
  }

  // Contagens das abas ignoram a aba activa mas respeitam os restantes filtros.
  function contarPara(vista) {
    const anterior = estado.vista;
    estado.vista = vista;
    const n = filtrar().length;
    estado.vista = anterior;
    return n;
  }

  // ------------------------------------------------------------- renderizar
  function cartao(a) {
    const guardado = estado.guardados.has(a.id);
    const lido = estado.lidos.has(a.id);
    const indep = a.tipo === "independente";
    const imagem = a.imagem
      ? `<img class="miniatura" src="${escapar(a.imagem)}" alt="" loading="lazy" decoding="async"
           referrerpolicy="no-referrer" onerror="this.remove()">`
      : "";
    return `
      <article class="artigo${lido ? " lido" : ""}" data-id="${a.id}">
        <div class="artigo-texto">
          <div class="meta">
            <span class="selo${indep ? " indep" : ""}">${escapar(a.fonteNome)}</span>
            <span title="${escapar(new Date(a.publicado).toLocaleString("pt-PT"))}">${escapar(quando(a.publicado))}</span>
            ${a.autor ? `<span class="sep">·</span><span>${escapar(a.autor)}</span>` : ""}
          </div>
          <h3><a href="${escapar(a.url)}" target="_blank" rel="noopener noreferrer">${escapar(a.titulo)}</a></h3>
          ${a.resumo ? `<p>${escapar(a.resumo)}</p>` : ""}
        </div>
        ${imagem}
        <button class="guardar" data-guardar="${a.id}" aria-pressed="${guardado}"
                title="${guardado ? "Remover dos guardados" : "Guardar para ler depois"}"
                aria-label="${guardado ? "Remover dos guardados" : "Guardar para ler depois"}">${guardado ? "★" : "☆"}</button>
      </article>`;
  }

  function renderizarArtigos() {
    const lista = filtrar();
    estado.visiveis = lista;
    el.artigos.setAttribute("aria-busy", "false");

    if (!lista.length) {
      const razao = estado.vista === "guardados"
        ? "Ainda não guardaste nada. Carrega na estrela de um artigo."
        : estado.pesquisa
          ? `Nada encontrado para “${escapar(estado.pesquisa)}”.`
          : "Nenhum artigo corresponde a estes filtros.";
      el.artigos.innerHTML = `<div class="vazio"><strong>Sem resultados</strong>${razao}</div>`;
    } else {
      let html = "";
      let diaAtual = null;
      for (const a of lista) {
        const dia = rotuloDia(a.publicado);
        if (dia !== diaAtual) { diaAtual = dia; html += `<h2 class="dia">${escapar(dia)}</h2>`; }
        html += cartao(a);
      }
      el.artigos.innerHTML = html;
    }

    $("#c-tudo").textContent = contarPara("tudo");
    $("#c-novos").textContent = contarPara("novos");
    $("#c-guardados").textContent = contarPara("guardados");
  }

  function renderizarLateral() {
    const fontes = estado.dados?.fontes ?? [];
    const contagem = {};
    for (const a of estado.dados?.artigos ?? []) contagem[a.fonte] = (contagem[a.fonte] || 0) + 1;

    const visiveis = fontes.filter((f) => !estado.soIndependentes || f.tipo === "independente");
    const recolhidas = visiveis.filter((f) => f.estado !== "bloqueada" && f.estado !== "inativa");
    const soNoSite = visiveis.filter((f) => f.estado === "bloqueada");

    el.fontes.innerHTML = recolhidas.map((f) => `
      <button class="fonte-btn${f.tipo === "independente" ? " indep" : ""}${f.ok ? "" : " falhou"}"
              data-fonte="${f.id}" aria-pressed="${estado.fonte === f.id}"
              title="${escapar(f.descricao)}${f.ok ? "" : ` — indisponível: ${escapar(f.erro)}`}">
        <span class="pastilha" aria-hidden="true"></span>
        <span class="nome">${escapar(f.nome)}</span>
        <span class="n">${contagem[f.id] || 0}</span>
      </button>`).join("");

    // Meios que não deixam recolher o feed continuam a valer a pena: entram como
    // ligação directa, não como erro.
    el.noSite.hidden = soNoSite.length === 0;
    el.listaNoSite.innerHTML = soNoSite.map((f) => `
      <a class="fonte-btn fonte-ligacao${f.tipo === "independente" ? " indep" : ""}"
         href="${escapar(f.site)}" target="_blank" rel="noopener noreferrer"
         title="${escapar(f.descricao)} — ${escapar(f.erro)}">
        <span class="pastilha" aria-hidden="true"></span>
        <span class="nome">${escapar(f.nome)}</span>
        <span class="seta" aria-hidden="true">↗</span>
      </a>`).join("");

    // Só temas que têm mesmo artigos — uma fonte pode responder e não ter nada na janela.
    const comArtigos = new Set(visiveis.filter((f) => contagem[f.id]).map((f) => f.id));
    const temas = [...new Set((estado.dados?.artigos ?? [])
      .filter((a) => comArtigos.has(a.fonte)).map((a) => a.tema).filter(Boolean))].sort();
    el.temas.innerHTML = temas.map((t) => `
      <button class="fonte-btn" data-assunto="${escapar(t)}" aria-pressed="${estado.assunto === t}">
        <span class="nome">${escapar(t)}</span>
      </button>`).join("");
  }

  function renderizarAvisos() {
    const falhadas = (estado.dados?.fontes ?? []).filter((f) => f.estado === "falha");
    if (!falhadas.length) { el.avisos.innerHTML = ""; return; }
    const links = falhadas.map((f) =>
      `<a href="${escapar(f.site)}" target="_blank" rel="noopener noreferrer">${escapar(f.nome)}</a>`).join(", ");
    el.avisos.innerHTML = `
      <div class="aviso">
        <span aria-hidden="true">⚠</span>
        <span><b>${falhadas.length} ${falhadas.length === 1 ? "fonte não respondeu" : "fontes não responderam"}</b>
        na última recolha: ${links}. Deve ser passageiro — podes ler directamente no site.</span>
      </div>`;
  }

  function renderizarCarimbo() {
    if (!estado.dados) return;
    const ok = estado.dados.fontes.filter((f) => f.ok).length;
    el.carimbo.textContent = `actualizado ${relativo(estado.dados.geradoEm)}`;
    el.carimbo.title = new Date(estado.dados.geradoEm).toLocaleString("pt-PT");
    el.rodape.textContent =
      `${estado.dados.artigos.length} artigos de ${ok} fontes · recolha de ${new Date(estado.dados.geradoEm).toLocaleString("pt-PT")}`;
  }

  const renderizar = () => { renderizarLateral(); renderizarAvisos(); renderizarArtigos(); renderizarCarimbo(); };

  // -------------------------------------------------------------- leitor
  async function corpoDe(artigo) {
    if (!artigo.temTexto) return null;
    if (corposEmCache.has(artigo.id)) return corposEmCache.get(artigo.id);
    const resposta = await fetch(`./data/artigos/${artigo.id}.json`);
    if (!resposta.ok) throw new Error(`HTTP ${resposta.status}`);
    const { html } = await resposta.json();
    corposEmCache.set(artigo.id, html);
    return html;
  }

  function cabecalhoLeitor(a) {
    return `
      <h1 id="leitor-titulo">${escapar(a.titulo)}</h1>
      <div class="leitor-meta">
        <span class="selo${a.tipo === "independente" ? " indep" : ""}">${escapar(a.fonteNome)}</span>
        <span>${escapar(new Date(a.publicado).toLocaleString("pt-PT", { dateStyle: "long", timeStyle: "short" }))}</span>
        ${a.autor ? `<span class="sep">·</span><span>${escapar(a.autor)}</span>` : ""}
      </div>
      ${a.imagem ? `<img class="leitor-capa" src="${escapar(a.imagem)}" alt=""
           referrerpolicy="no-referrer" onerror="this.remove()">` : ""}`;
  }

  // Sem texto no feed não inventamos: mostra-se o resumo que o editor sindicou
  // e dão-se as duas saídas honestas — o site original, ou o iframe se o site
  // permitir ser embebido (medido na recolha, não adivinhado aqui).
  function painelSemTexto(a) {
    const fonte = estado.dados?.fontes.find((f) => f.id === a.fonte);
    const embebivel = Boolean(fonte?.embebivel);
    return `
      ${a.resumo ? `<p class="leitor-resumo">${escapar(a.resumo)}</p>` : ""}
      <div class="leitor-parcial">
        <b>${escapar(a.fonteNome)} só publica o resumo no feed.</b>
        O texto completo fica no site — é o editor que decide o que sindicar, e não vamos
        buscá-lo por trás dessa decisão.
        <div class="botoes">
          <a class="botao" href="${escapar(a.url)}" target="_blank" rel="noopener noreferrer">Abrir no site ↗</a>
          ${embebivel ? `<button data-embeber="${a.id}">Ler aqui dentro</button>` : ""}
        </div>
      </div>`;
  }

  function sincronizarBotoesLeitor() {
    const a = estado.aberto;
    if (!a) return;
    const guardado = estado.guardados.has(a.id);
    el.leitorGuardar.setAttribute("aria-pressed", String(guardado));
    el.leitorGuardar.textContent = guardado ? "★" : "☆";
    el.leitorGuardar.title = guardado ? "Remover dos guardados" : "Guardar para ler depois";
    const posicao = estado.visiveis.findIndex((x) => x.id === a.id);
    $("#leitor-anterior").disabled = posicao <= 0;
    $("#leitor-seguinte").disabled = posicao < 0 || posicao >= estado.visiveis.length - 1;
  }

  async function abrirLeitor(artigo) {
    estado.aberto = artigo;
    el.leitor.hidden = false;
    el.leitorFundo.hidden = false;
    document.body.style.overflow = "hidden";
    el.leitorOriginal.href = artigo.url;
    el.leitorCorpo.scrollTop = 0;
    el.leitorCorpo.innerHTML = cabecalhoLeitor(artigo) +
      (artigo.temTexto ? `<div class="leitor-erro">a carregar o artigo…</div>` : painelSemTexto(artigo));
    el.leitorCorpo.focus();
    sincronizarBotoesLeitor();
    marcarLido(artigo.id);

    if (!artigo.temTexto) return;
    try {
      const html = await corpoDe(artigo);
      if (estado.aberto?.id !== artigo.id) return;   // já mudou de artigo
      el.leitorCorpo.innerHTML = cabecalhoLeitor(artigo) + `<div class="leitor-texto">${html}</div>`;
    } catch (erro) {
      if (estado.aberto?.id !== artigo.id) return;
      el.leitorCorpo.innerHTML = cabecalhoLeitor(artigo) + painelSemTexto(artigo);
    }
  }

  function fecharLeitor() {
    estado.aberto = null;
    el.leitor.hidden = true;
    el.leitorFundo.hidden = true;
    document.body.style.overflow = "";
  }

  function saltarArtigo(passo) {
    if (!estado.aberto) return;
    const posicao = estado.visiveis.findIndex((x) => x.id === estado.aberto.id);
    if (posicao < 0) return;
    const seguinte = estado.visiveis[posicao + passo];
    if (seguinte) abrirLeitor(seguinte);
  }

  // -------------------------------------------------------------- dados
  async function carregar({ forcar = false } = {}) {
    try {
      const resposta = await fetch(`${FICHEIRO}?v=${forcar ? Date.now() : Math.floor(Date.now() / 6e5)}`,
        { cache: forcar ? "reload" : "default" });
      if (!resposta.ok) throw new Error(`HTTP ${resposta.status}`);
      estado.dados = await resposta.json();
      renderizar();
    } catch (erro) {
      el.artigos.setAttribute("aria-busy", "false");
      el.artigos.innerHTML = `<div class="vazio"><strong>Não consegui carregar as notícias</strong>
        ${escapar(erro.message)}. Corre <code>python3 scripts/recolher.py</code> para gerar
        <code>data/noticias.json</code>, ou verifica a ligação.</div>`;
    }
  }

  // -------------------------------------------------------------- interacção
  function alternarGuardado(id) {
    estado.guardados.has(id) ? estado.guardados.delete(id) : estado.guardados.add(id);
    guardarConjunto(CHAVES.guardados, estado.guardados);
    renderizarArtigos();
    sincronizarBotoesLeitor();
  }

  function marcarLido(id) {
    if (estado.lidos.has(id)) return;
    estado.lidos.add(id);
    guardarConjunto(CHAVES.lidos, estado.lidos);
    document.querySelector(`.artigo[data-id="${id}"]`)?.classList.add("lido");
    $("#c-novos").textContent = contarPara("novos");
  }

  function aplicarAspeto(aspeto) {
    document.documentElement.dataset.tema = aspeto;
    try { localStorage.setItem(CHAVES.aspeto, aspeto); } catch {}
  }

  document.addEventListener("click", (evento) => {
    const guardar = evento.target.closest("[data-guardar]");
    if (guardar) { evento.preventDefault(); alternarGuardado(guardar.dataset.guardar); return; }

    const fonte = evento.target.closest("[data-fonte]");
    if (fonte) { estado.fonte = estado.fonte === fonte.dataset.fonte ? null : fonte.dataset.fonte; renderizar(); return; }

    const assunto = evento.target.closest("[data-assunto]");
    if (assunto) {
      estado.assunto = estado.assunto === assunto.dataset.assunto ? null : assunto.dataset.assunto;
      renderizar();
      return;
    }

    const aba = evento.target.closest("[data-vista]");
    if (aba) {
      estado.vista = aba.dataset.vista;
      document.querySelectorAll("[data-vista]").forEach((b) =>
        b.setAttribute("aria-selected", String(b === aba)));
      renderizarArtigos();
      return;
    }

    const embeber = evento.target.closest("[data-embeber]");
    if (embeber) {
      const a = estado.aberto;
      if (a) {
        embeber.closest(".leitor-parcial").insertAdjacentHTML("afterend",
          `<iframe class="leitor-quadro" src="${escapar(a.url)}" title="${escapar(a.titulo)}"
             loading="lazy" referrerpolicy="no-referrer"
             sandbox="allow-scripts allow-same-origin allow-popups allow-forms"></iframe>`);
        embeber.remove();
      }
      return;
    }

    const ligacao = evento.target.closest(".artigo a[href]");
    if (ligacao) {
      const artigo = estado.visiveis.find((x) => x.id === ligacao.closest(".artigo").dataset.id);
      // Modificadores e botão do meio ficam com o comportamento normal do browser.
      if (!artigo || evento.metaKey || evento.ctrlKey || evento.shiftKey || evento.altKey || evento.button !== 0) {
        if (artigo) marcarLido(artigo.id);
        return;
      }
      evento.preventDefault();
      abrirLeitor(artigo);
    }
  });

  // ------------------------------------------------------------ eventos do leitor
  $("#leitor-fechar").addEventListener("click", fecharLeitor);
  el.leitorFundo.addEventListener("click", fecharLeitor);
  $("#leitor-anterior").addEventListener("click", () => saltarArtigo(-1));
  $("#leitor-seguinte").addEventListener("click", () => saltarArtigo(1));
  el.leitorGuardar.addEventListener("click", () => {
    if (!estado.aberto) return;
    alternarGuardado(estado.aberto.id);
    sincronizarBotoesLeitor();
  });

  el.indep.addEventListener("click", () => {
    estado.soIndependentes = !estado.soIndependentes;
    el.indep.setAttribute("aria-pressed", String(estado.soIndependentes));
    if (estado.soIndependentes && estado.fonte) {
      const f = estado.dados?.fontes.find((x) => x.id === estado.fonte);
      if (f && f.tipo !== "independente") estado.fonte = null;
    }
    renderizar();
  });

  let temporizador;
  el.pesquisa.addEventListener("input", () => {
    clearTimeout(temporizador);
    temporizador = setTimeout(() => { estado.pesquisa = el.pesquisa.value.trim(); renderizarArtigos(); }, 140);
  });

  $("#btn-tema").addEventListener("click", () => {
    const escuroActivo = document.documentElement.dataset.tema
      ? document.documentElement.dataset.tema === "escuro"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    aplicarAspeto(escuroActivo ? "claro" : "escuro");
  });

  $("#btn-recarregar").addEventListener("click", () => carregar({ forcar: true }));

  document.addEventListener("keydown", (evento) => {
    const aEscrever = /^(INPUT|TEXTAREA)$/.test(evento.target.tagName);

    if (estado.aberto) {
      if (evento.key === "Escape") { fecharLeitor(); return; }
      if (aEscrever) return;
      if (evento.key === "j" || evento.key === "ArrowDown") { evento.preventDefault(); saltarArtigo(1); return; }
      if (evento.key === "k" || evento.key === "ArrowUp") { evento.preventDefault(); saltarArtigo(-1); return; }
      if (evento.key === "s") { el.leitorGuardar.click(); return; }
      if (evento.key === "o") { window.open(estado.aberto.url, "_blank", "noopener"); return; }
      return;
    }

    if (evento.key === "/" && !aEscrever) { evento.preventDefault(); el.pesquisa.focus(); return; }
    if (evento.key === "Enter" && aEscrever && estado.visiveis[0]) {
      el.pesquisa.blur(); abrirLeitor(estado.visiveis[0]); return;
    }
    if (evento.key === "Escape" && aEscrever) { el.pesquisa.value = ""; estado.pesquisa = ""; el.pesquisa.blur(); renderizarArtigos(); return; }
    if (aEscrever || evento.metaKey || evento.ctrlKey || evento.altKey) return;
    if (evento.key === "t") $("#btn-tema").click();
    if (evento.key === "r") carregar({ forcar: true });
    if (evento.key === "i") el.indep.click();
  });

  // ------------------------------------------------------------------ arranque
  try {
    const guardado = localStorage.getItem(CHAVES.aspeto);
    if (guardado) document.documentElement.dataset.tema = guardado;
  } catch {}

  carregar();
  // Recolha nova de 30 em 30 min no servidor; verificamos de 10 em 10.
  setInterval(() => carregar(), 6e5);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) renderizarCarimbo(); });

  if ("serviceWorker" in navigator && location.protocol.startsWith("http")) {
    addEventListener("load", () => navigator.serviceWorker.register("./sw.js").catch(() => {}));
  }
})();
