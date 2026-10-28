"""Gera a página do mapa da região de uma pousada (pousadas/mapas/<slug>.html) a partir do guia local.

    python scripts/gerar_mapa.py pousadas/exemplo-rosa.json

O app das pousadas serve a página em /mapa/<slug>, e o bot manda esse link quando o hóspede pergunta onde fica algo.
Mapa: Leaflet + OpenStreetMap (atribuição obrigatória, já na página). Lugar sem coordenada aparece só na lista.
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

CORES = {"Mercados": "#2f7d4f", "Padarias": "#b7791f", "Farmácias": "#c53030", "Roupas e moda praia": "#805ad5",
         "Restaurantes": "#dd6b20", "Sorveterias": "#d53f8c", "Postos de gasolina": "#4a5568", "Saúde": "#e53e3e",
         "Praias, trilhas e passeios": "#2b6cb0", "Bancos e caixas eletrônicos": "#2c7a7b", "Como chegar": "#1f2421"}
ICONES = {"Como chegar": "✈️", "Mercados": "🛒", "Padarias": "🥐", "Farmácias": "💊", "Roupas e moda praia": "👕", "Restaurantes": "🍽️", "Sorveterias": "🍦",
          "Postos de gasolina": "⛽", "Saúde": "🏥", "Praias, trilhas e passeios": "🌊", "Bancos e caixas eletrônicos": "🏧"}


def distancia(km: float | None) -> str:
    if km is None:
        return ""
    if km < 1:
        return f"{round(km * 1000, -1):.0f} m · {max(1, round(km * 1000 / 80))} min a pé"
    texto = f"{km:.1f} km".replace(".", ",")
    return f"{texto} · {max(1, round(km * 1000 / 80))} min a pé" if km <= 2 else f"{texto} · ~{max(1, round(km * 2))} min de carro"


# categoria do guia -> categoria da busca no Google (bot/lugares.py: CATEGORIAS)
BUSCA = {"Mercados": "mercado", "Padarias": "padaria", "Farmácias": "farmacia", "Roupas e moda praia": "roupas",
         "Restaurantes": "restaurante", "Sorveterias": "sorveteria", "Postos de gasolina": "posto_gasolina", "Saúde": "saude",
         "Praias, trilhas e passeios": "praia_passeio", "Bancos e caixas eletrônicos": "caixa_eletronico"}


def gerar(pousada: dict, google: bool = False) -> str:
    guia = sorted((i for i in pousada["faq"] if i.get("tipo") == "guia"),
                  key=lambda i: (i.get("km") is None, i.get("km") or 0))
    categorias = [c for c in CORES if any(i["categoria"] == c for i in guia)]
    dados = {
        "pousada": {"nome": pousada["nome"], "lat": pousada["lat"], "lon": pousada["lon"],
                    **{k: v for k, v in (pousada.get("mapa") or {}).items()
                       if k in ("descricao", "endereco", "foto", "instagram", "site") and v}},
        "lugares": [{"nome": i["nome"], "categoria": i["categoria"], "lat": i.get("lat"), "lon": i.get("lon"),
                     "dist": (f"{i['km']:.0f} km · {i['tempo']}" if i.get("tempo") and i.get("km") is not None
                             else distancia(i.get("km"))), "descricao": i.get("descricao", ""),
                     "endereco": i.get("endereco", ""), "horario": i.get("horario", ""),
                     "contato": i.get("contato", ""), "temporada": bool(i.get("temporada"))} for i in guia],
        "cores": CORES, "icones": ICONES,
    }
    if google:
        from bot.lugares import CATEGORIAS

        dados["tipos"] = {c: CATEGORIAS[BUSCA[c]] for c in CORES if c in BUSCA}
        categorias = list(dados["tipos"])
    nome = html.escape(pousada["nome"])
    chips = "".join(f'<button class="chip" data-cat="{html.escape(c)}">{ICONES.get(c, "📍")} {html.escape(c)}</button>'
                    for c in categorias)
    return (PAGINA_GOOGLE if google else PAGINA).replace("{{NOME}}", nome).replace("{{CHIPS}}", chips).replace(
        "{{DADOS}}", json.dumps(dados, ensure_ascii=False).replace("</", "<\\/"))


PAGINA = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Guia da região · {{NOME}}</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<style>
  :root { --fundo:#f6f3ee; --cartao:#fff; --texto:#1f2421; --suave:#5d655f; --linha:#e4ded4; --destaque:#2f6f5e; }
  @media (prefers-color-scheme: dark) { :root { --fundo:#141816; --cartao:#1d2320; --texto:#ecefe9; --suave:#a3aca5; --linha:#2c3430; --destaque:#7cc5ac; } }
  * { box-sizing: border-box; }
  /* página travada: só rola para baixo, sem pinça e sem arrasto para os lados; o mapa tem o próprio zoom */
  html, body { touch-action: pan-y; overflow-x: hidden; width: 100%; max-width: 100%; position: relative;
               -webkit-text-size-adjust: 100%; overscroll-behavior-x: none; }
  #mapa { touch-action: none; }
  body { margin:0; font:16px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; background:var(--fundo); color:var(--texto); }
  header { padding:18px 16px 10px; }
  header small { color:var(--suave); letter-spacing:.06em; text-transform:uppercase; font-size:12px; }
  h1 { margin:2px 0 0; font-size:22px; }
  #mapa { height:46vh; min-height:260px; border-top:1px solid var(--linha); border-bottom:1px solid var(--linha); }
  .chips { display:flex; gap:8px; overflow-x:auto; padding:12px 16px; scrollbar-width:none; }
  .chip { flex:none; border:1px solid var(--linha); background:var(--cartao); color:var(--texto); border-radius:999px;
          padding:7px 12px; font:inherit; font-size:14px; cursor:pointer; }
  .chip.ativo { background:var(--destaque); border-color:var(--destaque); color:#fff; }
  main { padding:0 16px 32px; max-width:720px; margin:0 auto; }
  .lugar { background:var(--cartao); border:1px solid var(--linha); border-radius:14px; padding:14px; margin:10px 0; }
  .lugar h3 { margin:0; font-size:16px; display:flex; gap:8px; align-items:baseline; justify-content:space-between; }
  .dist { color:var(--destaque); font-size:13px; font-weight:600; white-space:nowrap; }
  .lugar p { margin:6px 0 0; color:var(--suave); font-size:14px; }
  .acoes { margin-top:10px; display:flex; gap:14px; font-size:14px; }
  .acoes a { color:var(--destaque); font-weight:600; text-decoration:none; }
  .tag { display:inline-block; font-size:12px; padding:2px 8px; border-radius:999px; background:var(--linha); margin-top:8px; }
  footer { color:var(--suave); font-size:12px; padding:0 16px 24px; max-width:720px; margin:0 auto; }
  .pino { width:30px; height:30px; border-radius:50%; display:grid; place-items:center; color:#fff; font-size:15px;
          border:2px solid #fff; box-shadow:0 1px 4px rgba(0,0,0,.35); }
</style>
</head>
<body>
<header><small>Guia da região</small><h1>{{NOME}}</h1></header>
<div id="mapa" role="img" aria-label="Mapa da região com a pousada e os lugares do guia"></div>
<nav class="chips" aria-label="Categorias"><button class="chip ativo" data-cat="">Tudo</button>{{CHIPS}}</nav>
<main id="lista"></main>
<footer>Distâncias aproximadas a partir da pousada. Horários mudam fora da temporada: confirme antes de ir.
Mapa © <a href="https://www.openstreetmap.org/copyright">colaboradores do OpenStreetMap</a>.</footer>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>
const D = {{DADOS}};
const esc = s => String(s || "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const mapa = L.map("mapa", {scrollWheelZoom:false}).setView([D.pousada.lat, D.pousada.lon], 15);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {maxZoom:19,
  attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'}).addTo(mapa);
const pino = (cor, icone) => L.divIcon({className:"", html:`<div class="pino" style="background:${cor}">${icone}</div>`, iconSize:[30,30], iconAnchor:[15,15]});
const P = D.pousada;
L.marker([P.lat, P.lon], {icon:pino("#1f2421","🏡"), zIndexOffset:1000}).addTo(mapa).bindPopup(
  `<div style="max-width:240px">${P.foto ? `<img src="${esc(P.foto)}" alt="" style="display:block;width:100%;height:120px;object-fit:cover;border-radius:8px;margin-bottom:8px">` : ""}`
  + `<b>${esc(P.nome)}</b>${P.descricao ? `<br>${esc(P.descricao)}` : ""}${P.endereco ? `<br><span style="color:#5d655f">📍 ${esc(P.endereco)}</span>` : ""}`
  + `${P.instagram ? `<br><a href="https://instagram.com/${esc(P.instagram.replace("@", ""))}" target="_blank" rel="noopener">${esc(P.instagram)}</a>` : ""}`
  + `<br><a href="https://www.google.com/maps/dir/?api=1&destination=${P.lat},${P.lon}" target="_blank" rel="noopener">Como chegar →</a></div>`);
const marcadores = [];
function rota(l) { return l.lat != null ? `https://www.google.com/maps/dir/?api=1&destination=${l.lat},${l.lon}`
                                      : `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(l.nome + " " + l.endereco)}`; }
function desenhar(cat) {
  marcadores.forEach(m => mapa.removeLayer(m)); marcadores.length = 0;
  const lista = D.lugares.filter(l => !cat || l.categoria === cat);
  const pontos = [[D.pousada.lat, D.pousada.lon]];
  document.getElementById("lista").innerHTML = lista.map(l => {
    if (l.lat != null) {
      const m = L.marker([l.lat, l.lon], {icon:pino(D.cores[l.categoria] || "#555", D.icones[l.categoria] || "📍")})
        .bindPopup(`<b>${esc(l.nome)}</b><br>${esc(l.dist)}`).addTo(mapa);
      marcadores.push(m); pontos.push([l.lat, l.lon]);
    }
    return `<article class="lugar"><h3><span>${D.icones[l.categoria] || "📍"} ${esc(l.nome)}</span><span class="dist">${esc(l.dist)}</span></h3>
      <p>${esc(l.descricao)}</p>${l.endereco ? `<p>📍 ${esc(l.endereco)}</p>` : ""}${l.horario ? `<p>🕒 ${esc(l.horario)}</p>` : ""}
      ${l.contato ? `<p>☎️ ${esc(l.contato)}</p>` : ""}${l.temporada ? `<span class="tag">Só na temporada</span>` : ""}
      <div class="acoes"><a href="${rota(l)}" target="_blank" rel="noopener">Como chegar →</a></div></article>`;
  }).join("");
  if (cat && pontos.length > 1) mapa.fitBounds(pontos, {padding:[30,30], maxZoom:16});
  else mapa.setView([D.pousada.lat, D.pousada.lon], 14);
}
document.querySelectorAll(".chip").forEach(b => b.addEventListener("click", () => {
  document.querySelectorAll(".chip").forEach(x => x.classList.toggle("ativo", x === b)); desenhar(b.dataset.cat);
}));
desenhar("");
</script>
</body>
</html>
"""


PAGINA_GOOGLE = PAGINA[:PAGINA.index("<link rel=")] + PAGINA[PAGINA.index("<style>"):PAGINA.index("</style>")] + """
  #mapa { height:52vh; }
  /* computador: lista à esquerda com rolagem própria, mapa ocupando a tela à direita */
  @media (min-width: 900px) {
    body { height:100vh; overflow:hidden; display:grid; grid-template-columns:minmax(360px, 420px) 1fr;
           grid-template-rows:auto auto auto 1fr auto; grid-template-areas:"cab mapa" "busca mapa" "chips mapa" "lista mapa" "rodape mapa"; }
    header { grid-area:cab; }
    #mapa { grid-area:mapa; height:100vh; min-height:0; border:0; border-left:1px solid var(--linha); }
    .busca { grid-area:busca; }
    .chips { grid-area:chips; flex-wrap:wrap; overflow:visible; }
    main { grid-area:lista; overflow-y:auto; max-width:none; margin:0; padding:0 16px 16px; min-height:0; }
    footer { grid-area:rodape; max-width:none; margin:0; padding:10px 16px 14px; border-top:1px solid var(--linha); }
    .chip:hover { border-color:var(--destaque); }
  }
  .busca { display:flex; gap:8px; padding:12px 16px 0; }
  .busca input { flex:1; min-width:0; border:1px solid var(--linha); background:var(--cartao); color:var(--texto);
                 border-radius:999px; padding:9px 14px; font:inherit; font-size:16px; }  /* < 16px o iPhone amplia a página ao tocar */
  .busca button { border:0; background:var(--destaque); color:#fff; border-radius:999px; padding:9px 16px; font:inherit; font-size:15px; }
  .vazio { color:var(--suave); padding:18px 0; }
  .chip.filtro.ativo { background:#2f7d4f; border-color:#2f7d4f; }
  article.lugar { cursor:pointer; }
  article.lugar:hover { border-color:var(--destaque); }
  .grupo { margin:18px 0 6px; }
  .grupo h2 { font-size:17px; margin:0 0 4px; padding-bottom:6px; border-bottom:1px solid var(--linha); }
  .linha { display:flex; justify-content:space-between; gap:14px; padding:9px 2px; border-bottom:1px solid var(--linha);
           color:var(--texto); text-decoration:none; font-size:15px; }
  .linha .d { color:var(--suave); white-space:nowrap; font-variant-numeric:tabular-nums; }
  .linha:hover span:first-child, .linha:focus-visible span:first-child { color:var(--destaque); }
</style>
</head>
<body>
<header><small>Guia da região</small><h1>{{NOME}}</h1></header>
<div id="mapa" role="img" aria-label="Mapa da região com a pousada e os lugares"></div>
<form class="busca" role="search"><input id="termo" type="search" placeholder="Procurar: sorveteria, lavanderia, surf…"
  aria-label="Procurar lugar perto da pousada" enterkeyhint="search"><button type="submit">Buscar</button></form>
<nav class="chips" aria-label="Categorias"><button class="chip filtro" id="abertos" type="button" aria-pressed="false">🕒 Abertos agora</button><button class="chip ativo" data-cat="">Guia da pousada</button>{{CHIPS}}</nav>
<main id="lista"></main>
<footer>Distâncias aproximadas a partir da pousada. Horários mudam fora da temporada: confirme antes de ir.
Lugares das categorias e da busca: Google Maps.</footer>
<script>
const D = {{DADOS}};
const esc = s => String(s || "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const rad = x => x * Math.PI / 180;
function km(lat, lon) {
  const a = Math.sin(rad(lat - D.pousada.lat) / 2) ** 2 + Math.cos(rad(D.pousada.lat)) * Math.cos(rad(lat)) * Math.sin(rad(lon - D.pousada.lon) / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.sqrt(a)) * 1.3;
}
function dist(k) {
  if (k == null) return "";
  const ape = Math.max(1, Math.round(k * 1000 / 80)), t = k.toFixed(1).replace(".", ",");
  if (k < 1) return `${Math.round(k * 100) * 10} m · ${ape} min a pé`;
  return k <= 2 ? `${t} km · ${ape} min a pé` : `${t} km · ~${Math.max(1, Math.round(k * 2))} min de carro`;
}
const centro = {lat: D.pousada.lat, lng: D.pousada.lon};
let mapa, marcadores = [], balao;
function rotaDe(l) {
  return l.lat != null ? `https://www.google.com/maps/dir/?api=1&destination=${l.lat},${l.lon}`
    : (l.url || `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(l.nome + " " + (l.endereco || ""))}`);
}
function conteudoBalao(l) {  // miniatura da foto à esquerda, informações ao lado
  const info = `<b>${esc(l.nome)}</b>${l.dist ? `<br>${esc(l.dist)}` : ""}${l.nota ? `<br>⭐ ${esc(l.nota)}` : ""}
    ${l.aberto != null ? `<br><span style="color:${l.aberto ? "#2f7d4f" : "#c53030"};font-weight:600">${l.aberto ? "Aberto agora" : "Fechado agora"}</span>` : ""}
    ${l.hoje ? `<br><span style="font-size:12px;color:#5d655f">${esc(l.hoje)}</span>` : ""}
    <br><a href="${rotaDe(l)}" target="_blank" rel="noopener" style="color:#2f6f5e;font-weight:600">Como chegar →</a>`;
  if (!l.foto) return `<div style="font:14px/1.4 system-ui,sans-serif;color:#1f2421;max-width:230px">${info}</div>`;
  return `<div style="font:14px/1.4 system-ui,sans-serif;color:#1f2421;display:flex;gap:10px;align-items:flex-start;max-width:280px">
    <div style="flex:none;width:76px">
      <img src="${l.foto}" alt="" style="display:block;width:76px;height:76px;object-fit:cover;border-radius:8px">
      ${l.fotoAutor ? `<div style="font-size:9px;line-height:1.2;color:#5d655f;margin-top:3px">Foto: ${esc(l.fotoAutor)}</div>` : ""}
    </div>
    <div style="min-width:0">${info}</div></div>`;
}
async function horario(l, lugar) {  // "Aberto agora" e o horário de hoje, como o Google tem
  const dias = lugar.regularOpeningHours?.weekdayDescriptions;  // começa na segunda-feira
  if (dias && dias.length === 7) l.hoje = "Hoje: " + dias[(new Date().getDay() + 6) % 7].split(": ").slice(1).join(": ");
  const a = abertoAgora(lugar);
  if (a !== null) l.aberto = a;
}
async function abrirBalao(m, l) {  // tocar no pino: foto, nome, distância, nota e o caminho, em cima do mapa
  balao = balao || new google.maps.InfoWindow();
  balao.setContent(conteudoBalao(l));
  balao.open({map: mapa, anchor: m});
  if (l.fotoBuscada) return;
  l.fotoBuscada = true;  // a foto só é buscada no toque (cada busca de foto custa), e uma vez por lugar
  try {
    const {Place} = await google.maps.importLibrary("places");
    let fotos = [];
    if (l.id) {
      const lugar = new Place({id: l.id});
      await lugar.fetchFields({fields: ["photos", "regularOpeningHours", "utcOffsetMinutes", "businessStatus"]});
      fotos = lugar.photos || [];
      await horario(l, lugar);
    } else if (l.lat != null) {
      // lugar do nosso guia (praia, trilha...): acha no Google pelo nome, perto do ponto que temos
      const {places} = await Place.searchByText({textQuery: l.nome, fields: ["location", "photos", "regularOpeningHours", "utcOffsetMinutes", "businessStatus"], maxResultCount: 3,
        locationBias: {center: {lat: l.lat, lng: l.lon}, radius: 2000}, language: "pt-BR"});
      const perto = (places || []).find(p => p.location && Math.abs(p.location.lat() - l.lat) < 0.02
                                              && Math.abs(p.location.lng() - l.lon) < 0.02);
      fotos = perto ? (perto.photos || []) : [];
      if (perto) await horario(l, perto);
    }
    const f = fotos[0];
    if (!f) { if (balao.getAnchor() === m) balao.setContent(conteudoBalao(l)); return; }
    l.foto = f.getURI({maxWidth: 200, maxHeight: 200});
    l.fotoAutor = (f.authorAttributions || [])[0]?.displayName || "";
    if (balao.getAnchor() === m) balao.setContent(conteudoBalao(l));
  } catch (e) { console.warn(e); }
}
function conteudoPousada(P) {  // foto grande em cima, nome, descrição, endereço e links
  return `<div style="font:14px/1.45 system-ui,sans-serif;color:#1f2421;width:250px">
    ${P.foto ? `<img src="${esc(P.foto)}" alt="" style="display:block;width:100%;height:140px;object-fit:cover;border-radius:8px;margin-bottom:8px">` : ""}
    ${P.fotoAutor ? `<div style="font-size:9px;color:#5d655f;margin:-5px 0 6px">Foto: ${esc(P.fotoAutor)}</div>` : ""}
    <b style="font-size:15px">${esc(P.nome)}</b>
    ${P.descricao ? `<br>${esc(P.descricao)}` : ""}
    ${P.endereco ? `<br><span style="color:#5d655f">📍 ${esc(P.endereco)}</span>` : ""}
    ${P.instagram ? `<br><a href="https://instagram.com/${esc(P.instagram.replace("@", ""))}" target="_blank" rel="noopener" style="color:#2f6f5e">${esc(P.instagram)}</a>` : ""}
    ${P.site ? `<br><a href="${esc(P.site)}" target="_blank" rel="noopener" style="color:#2f6f5e">Site</a>` : ""}
    <br><a href="https://www.google.com/maps/dir/?api=1&destination=${P.lat},${P.lon}" target="_blank" rel="noopener" style="color:#2f6f5e;font-weight:600">Como chegar →</a></div>`;
}
async function abrirPousada(m) {  // tocar na casinha: as informações da pousada com a foto
  const P = D.pousada;
  balao = balao || new google.maps.InfoWindow();
  balao.setContent(conteudoPousada(P));
  balao.open({map: mapa, anchor: m});
  if (P.foto || P.fotoBuscada) return;
  P.fotoBuscada = true;  // sem foto no cadastro: usa a primeira foto do Google da própria pousada
  try {
    const {Place} = await google.maps.importLibrary("places");
    const {places} = await Place.searchByText({textQuery: P.nome, fields: ["location", "photos"], maxResultCount: 3,
      locationBias: {center: centro, radius: 1000}, language: "pt-BR"});
    const perto = (places || []).find(p => p.location && Math.abs(p.location.lat() - P.lat) < 0.01 && Math.abs(p.location.lng() - P.lon) < 0.01);
    const f = perto && (perto.photos || [])[0];
    if (!f) return;
    P.foto = f.getURI({maxWidth: 500, maxHeight: 300});
    P.fotoAutor = (f.authorAttributions || [])[0]?.displayName || "";
    if (balao.getAnchor() === m) balao.setContent(conteudoPousada(P));
  } catch (e) { console.warn(e); }
}
const cache = {};
function pino(cor, emoji) {  // bolinha com a cor e o ícone da categoria
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="34" height="34"><circle cx="17" cy="17" r="15" fill="${cor}" stroke="white" stroke-width="2.5"/><text x="17" y="22.5" font-size="15" text-anchor="middle">${emoji}</text></svg>`;
  return {url: "data:image/svg+xml;charset=UTF-8," + encodeURIComponent(svg),
          scaledSize: new google.maps.Size(34, 34), anchor: new google.maps.Point(17, 17)};
}
// busca livre: o ícone vem do tipo do lugar no Google
const POR_TIPO = [
  [["cafe", "coffee_shop"], "☕", "#8b5a2b"], [["restaurant", "pizza_restaurant", "seafood_restaurant", "fast_food_restaurant", "meal_takeaway"], "🍽️", "#dd6b20"],
  [["bakery"], "🥐", "#b7791f"], [["supermarket", "grocery_store", "convenience_store", "market", "food_store"], "🛒", "#2f7d4f"],
  [["pharmacy", "drugstore"], "💊", "#c53030"], [["ice_cream_shop", "dessert_shop", "acai_shop"], "🍦", "#d53f8c"],
  [["bar", "pub", "night_club", "wine_bar"], "🍺", "#9c4221"], [["clothing_store", "shoe_store", "jewelry_store"], "👕", "#805ad5"],
  [["laundry"], "🧺", "#3182ce"], [["gas_station"], "⛽", "#4a5568"], [["atm", "bank"], "🏧", "#2c7a7b"],
  [["hospital", "doctor", "medical_lab", "dentist"], "🏥", "#e53e3e"], [["beach"], "🏖️", "#2b6cb0"],
  [["tourist_attraction", "hiking_area", "park", "national_park"], "🥾", "#2b6cb0"],
  [["sporting_goods_store", "surf_shop"], "🏄", "#0987a0"]];
function visual(l, cat) {
  const c = l.categoria || cat;
  if (D.cores[c]) return [D.cores[c], D.icones[c] || "📍"];
  const achado = POR_TIPO.find(([tipos]) => tipos.includes(l.tipo));
  return achado ? [achado[2], achado[1]] : ["#4a5568", "📍"];
}
function cartao(l, cat, i) {
  const rota = l.lat != null ? `https://www.google.com/maps/dir/?api=1&destination=${l.lat},${l.lon}`
    : (l.url || `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(l.nome + " " + (l.endereco || ""))}`);
  return `<article class="lugar" id="l-${i}"><h3><span>${visual(l, cat)[1]} ${esc(l.nome)}</span><span class="dist">${esc(l.dist)}</span></h3>
    ${l.descricao ? `<p>${esc(l.descricao)}</p>` : ""}${l.endereco ? `<p>📍 ${esc(l.endereco)}</p>` : ""}
    ${l.horario ? `<p>🕒 ${esc(l.horario)}</p>` : ""}${l.nota ? `<p>⭐ ${esc(l.nota)} no Google</p>` : ""}
    ${l.aberto != null || l.hoje ? `<p><span style="color:${l.aberto ? "var(--destaque)" : "#e05252"};font-weight:600">${l.aberto === true ? "Aberto agora" : l.aberto === false ? "Fechado agora" : ""}</span>${l.hoje ? ` <span>${l.aberto != null ? "· " : ""}${esc(l.hoje)}</span>` : ""}</p>` : ""}
    ${l.temporada ? `<span class="tag">Só na temporada</span>` : ""}
    <div class="acoes"><a href="${rota}" target="_blank" rel="noopener">Como chegar →</a></div></article>`;
}
let porIndice = {};  // índice do card -> [pino, lugar], para o card levar o mapa até o lugar
function focar(i) {
  const par = porIndice[i];
  if (!par) return;
  mapa.panTo(par[0].getPosition());
  mapa.setZoom(Math.max(mapa.getZoom(), 17));
  abrirBalao(par[0], par[1]);
  if (window.matchMedia("(max-width: 899px)").matches)  // no celular o mapa fica em cima: volta até ele
    document.getElementById("mapa").scrollIntoView({behavior: "smooth", block: "start"});
}
function mostrar(lista, cat, busca = false) {
  marcadores.forEach(m => m.setMap(null)); marcadores = []; porIndice = {};
  const limites = new google.maps.LatLngBounds(centro);
  lista.forEach((l, i) => {
    if (l.lat == null) return;
    const c = l.categoria || cat, perto = cat || busca || km(l.lat, l.lon) <= 4;  // guia: enquadra só o entorno
    const [cor, icone] = visual(l, cat);
    const m = new google.maps.Marker({position: {lat: l.lat, lng: l.lon}, map: mapa, title: l.nome, icon: pino(cor, icone)});
    m.addListener("click", () => abrirBalao(m, l));
    marcadores.push(m); porIndice[i] = [m, l]; if (perto) limites.extend(m.getPosition());
  });
  document.getElementById("lista").innerHTML = !lista.length
    ? `<p class="vazio">Nada encontrado ${busca ? "com essa busca" : "nesta categoria"} perto da pousada.</p>`
    : (cat || busca) ? lista.map((l, i) => cartao(l, l.categoria || cat, i)).join("")
    : resumo(lista);
  if (marcadores.length) mapa.fitBounds(limites, 40); else { mapa.setCenter(centro); mapa.setZoom(15); }
}
function resumo(lista) {  // guia: lista enxuta por categoria, nome e distância; tocar abre o caminho
  const grupos = {};
  lista.forEach((l, i) => (grupos[l.categoria] = grupos[l.categoria] || []).push([l, i]));
  return Object.keys(D.cores).filter(c => grupos[c]).map(c => `<section class="grupo"><h2>${D.icones[c] || "📍"} ${esc(c)}</h2>
    ${grupos[c].map(([l, i]) => `<a class="linha" id="l-${i}" target="_blank" rel="noopener"
      href="${l.lat != null ? `https://www.google.com/maps/dir/?api=1&destination=${l.lat},${l.lon}` : `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(l.nome + " " + (l.endereco || ""))}`}">
      <span>${esc(l.nome)}</span><span class="d">${esc((l.dist || "").split(" · ")[0])}</span></a>`).join("")}</section>`).join("");
}
async function buscar(cat) {
  if (cache[cat]) return cache[cat];
  const {Place, SearchNearbyRankPreference} = await google.maps.importLibrary("places");
  // duas buscas (a API devolve no máximo 20): as mais perto e as mais relevantes do raio de 10 km (ex.: Campo D'Una)
  const pedir = ordem => Place.searchNearby({
    fields: ["id", "displayName", "location", "formattedAddress", "googleMapsURI", "rating", "userRatingCount", "primaryType",
      "regularOpeningHours", "utcOffsetMinutes", "businessStatus"],
    locationRestriction: {center: centro, radius: 10000}, includedPrimaryTypes: D.tipos[cat], maxResultCount: 20,
    rankPreference: ordem, language: "pt-BR"}).then(r => r.places).catch(() => []);
  const listas = await Promise.all([pedir(SearchNearbyRankPreference.DISTANCE), pedir(SearchNearbyRankPreference.POPULARITY)]);
  const unicos = new Map(); listas.flat().forEach(p => unicos.has(p.id) || unicos.set(p.id, p));
  const places = [...unicos.values()];
  if (!places.length) throw new Error("sem resultados");
  return cache[cat] = await comHorario(places, places.map(p => {
    const lat = p.location.lat(), lon = p.location.lng(), k = km(lat, lon);
    return {id: p.id, tipo: p.primaryType, nome: p.displayName, lat, lon, k, dist: dist(k), endereco: p.formattedAddress, url: p.googleMapsURI,
            nota: p.rating ? `${String(p.rating).replace(".", ",")} (${p.userRatingCount} avaliações)` : ""};
  })).then(ls => semHospedagem(ls).sort((a, b) => a.k - b.k));
}
async function procurar(termo) {
  const chave = "texto:" + termo.toLowerCase();
  if (cache[chave]) return cache[chave];
  const {Place, SearchByTextRankPreference} = await google.maps.importLibrary("places");
  const {places} = await Place.searchByText({textQuery: termo, locationBias: {center: centro, radius: 10000},
    fields: ["id", "displayName", "location", "formattedAddress", "googleMapsURI", "rating", "userRatingCount", "primaryType",
      "regularOpeningHours", "utcOffsetMinutes", "businessStatus"],
    maxResultCount: 20, rankPreference: SearchByTextRankPreference.DISTANCE, language: "pt-BR", region: "br"});
  return cache[chave] = await comHorario(places, places.map(p => {
    const lat = p.location.lat(), lon = p.location.lng(), k = km(lat, lon);
    return {id: p.id, tipo: p.primaryType, nome: p.displayName, lat, lon, k, dist: dist(k), endereco: p.formattedAddress, url: p.googleMapsURI,
            nota: p.rating ? `${String(p.rating).replace(".", ",")} (${p.userRatingCount} avaliações)` : ""};
  })).then(ls => semHospedagem(ls).filter(l => l.k <= 13).sort((a, b) => a.k - b.k));
}
// outras hospedagens nunca aparecem no mapa do cliente
const HOSPEDAGEM = /(^|_)(lodging|hotel|inn|guest_house|hostel|motel|bed_and_breakfast|resort|campground|camping|cottage|farmstay|private_guest_room)($|_)/;
const semHospedagem = ls => ls.filter(l => !HOSPEDAGEM.test(l.tipo || ""));
function abertoAgora(p) {  // calcula pelo horário do lugar (o isOpen() do Google só existe no beta)
  if (p.businessStatus && p.businessStatus !== "OPERATIONAL") return false;
  const periodos = p.regularOpeningHours?.periods;
  if (!periodos || !periodos.length || p.utcOffsetMinutes == null) return null;
  const agora = new Date(Date.now() + p.utcOffsetMinutes * 60000);  // relógio local do lugar, lido em UTC
  const semana = 7 * 1440, min = agora.getUTCDay() * 1440 + agora.getUTCHours() * 60 + agora.getUTCMinutes();
  return periodos.some(({open: a, close: f}) => {
    if (!a) return false;
    if (!f) return true;  // aberto 24 horas
    const ini = a.day * 1440 + a.hour * 60 + a.minute;
    let fim = f.day * 1440 + f.hour * 60 + f.minute;
    if (fim <= ini) fim += semana;  // vira a meia-noite ou a semana
    return (min >= ini && min < fim) || (min + semana >= ini && min + semana < fim);
  });
}
async function comHorario(places, lugares) {  // "aberto agora" e horário de hoje de cada lugar da busca
  const hoje = (new Date().getDay() + 6) % 7;  // a lista do Google começa na segunda
  await Promise.all(places.map(async (p, i) => {
    const dias = p.regularOpeningHours?.weekdayDescriptions;
    if (dias && dias.length === 7) lugares[i].hoje = "Hoje: " + dias[hoje].split(": ").slice(1).join(": ");
    const a = abertoAgora(p);
    if (a !== null) lugares[i].aberto = a;
  }));
  return lugares;
}
let soAbertos = false, atual = {lista: [], cat: "", busca: false};
function exibir(lista, cat, busca = false) {  // aplica o filtro "Abertos agora" antes de desenhar
  atual = {lista, cat, busca};
  const doGoogle = lista.some(l => l.id);
  const vis = soAbertos && doGoogle ? lista.filter(l => l.aberto === true) : lista;
  mostrar(vis, cat, busca);
  const el = document.getElementById("lista");
  if (soAbertos && !doGoogle)
    el.insertAdjacentHTML("afterbegin", `<p class="vazio">"Abertos agora" vale para as categorias e a busca. Escolha uma acima.</p>`);
  else if (soAbertos) {
    const semHorario = lista.filter(l => l.aberto == null).length;
    if (semHorario) el.insertAdjacentHTML("beforeend", `<p class="vazio">+${semHorario} sem horário informado no Google.</p>`);
  }
}
async function escolher(cat) {
  if (!cat) return exibir(D.lugares, "");
  document.getElementById("lista").innerHTML = `<p class="vazio">Buscando no Google Maps…</p>`;
  try { exibir(await buscar(cat), cat); }
  catch (e) { console.warn(e); exibir(D.lugares.filter(l => l.categoria === cat), cat); }
}
async function iniciar() {
  const {Map} = await google.maps.importLibrary("maps");
  mapa = new Map(document.getElementById("mapa"), {center: centro, zoom: 15,
    mapTypeControl: true, mapTypeControlOptions: {mapTypeIds: ["roadmap", "hybrid"],  // Mapa / Satélite (com nomes)
      style: google.maps.MapTypeControlStyle.HORIZONTAL_BAR, position: google.maps.ControlPosition.TOP_RIGHT},
    streetViewControl: false, fullscreenControl: false, gestureHandling: "greedy", clickableIcons: false,
    styles: [{featureType: "poi.business", stylers: [{visibility: "off"}]}, {featureType: "poi.attraction", elementType: "labels.icon", stylers: [{visibility: "off"}]}]});  // sem outras hospedagens nem comércios no fundo  // um dedo arrasta, pinça amplia (só no mapa)
  const casa = new google.maps.Marker({position: centro, map: mapa, title: D.pousada.nome, zIndex: 999, icon: pino("#1f2421", "🏡")});
  casa.addListener("click", () => abrirPousada(casa));
  document.querySelector(".busca").addEventListener("submit", async ev => {
    ev.preventDefault();
    const termo = document.getElementById("termo").value.trim();
    if (!termo) return;
    document.getElementById("termo").blur();
    document.querySelectorAll(".chip").forEach(x => x.classList.remove("ativo"));
    document.getElementById("lista").innerHTML = `<p class="vazio">Buscando "${esc(termo)}" no Google Maps…</p>`;
    try { exibir(await procurar(termo), "", true); }
    catch (e) { console.warn(e); document.getElementById("lista").innerHTML = `<p class="vazio">Não deu para buscar agora. Tente de novo.</p>`; }
  });
  document.getElementById("abertos").addEventListener("click", e => {
    soAbertos = !soAbertos;
    e.currentTarget.setAttribute("aria-pressed", String(soAbertos));
    e.currentTarget.classList.toggle("ativo", soAbertos);
    exibir(atual.lista, atual.cat, atual.busca);
  });
  document.querySelectorAll(".chip[data-cat]").forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll(".chip[data-cat]").forEach(x => x.classList.toggle("ativo", x === b));
    escolher(b.dataset.cat);
  }));
  document.getElementById("lista").addEventListener("click", e => {  // tocar no card (fora do link) mostra o lugar no mapa
    const card = e.target.closest("article.lugar");
    if (!card || e.target.closest("a")) return;
    focar(Number(card.id.slice(2)));
  });
  escolher("");
}
(g=>{var h,a,k,p="The Google Maps JavaScript API",c="google",l="importLibrary",q="__ib__",m=document,b=window;b=b[c]||(b[c]={});var d=b.maps||(b.maps={}),r=new Set,e=new URLSearchParams,u=()=>h||(h=new Promise(async(f,n)=>{await (a=m.createElement("script"));e.set("libraries",[...r]+"");for(k in g)e.set(k.replace(/[A-Z]/g,t=>"_"+t[0].toLowerCase()),g[k]);e.set("callback",c+".maps."+q);a.src=`https://maps.${c}apis.com/maps/api/js?`+e;d[q]=f;a.onerror=()=>h=n(Error(p+" could not load."));a.nonce=m.querySelector("script[nonce]")?.nonce||"";m.head.append(a)}));d[l]?console.warn(p+" only loads once. Ignoring:",g):d[l]=(f,...n)=>r.add(f)&&u().then(()=>d[l](f,...n))})({key: "{{CHAVE_MAPS}}", v: "weekly", language: "pt-BR", region: "BR"});
// iPhone ignora user-scalable=no: bloqueia a pinça e o toque duplo fora do mapa
document.addEventListener("gesturestart", e => { if (!e.target.closest?.("#mapa")) e.preventDefault(); }, {passive: false});
let ultimoToque = 0;
document.addEventListener("touchend", e => {
  const agora = Date.now();
  if (agora - ultimoToque < 300 && !e.target.closest?.("#mapa, a, button, input")) e.preventDefault();
  ultimoToque = agora;
}, {passive: false});
iniciar();
</script>
</body>
</html>
"""


def salvar(origem: Path) -> tuple[Path, Path]:
    """Gera os dois mapas (OpenStreetMap e Google) em <pasta do JSON>/mapas/<slug>.html."""
    pousada = json.loads(origem.read_text(encoding="utf-8"))
    destino = origem.parent / "mapas" / f"{pousada['slug']}.html"
    destino.parent.mkdir(exist_ok=True)
    destino.write_text(gerar(pousada), encoding="utf-8")
    destino.with_suffix(".google.html").write_text(gerar(pousada, google=True), encoding="utf-8")
    return destino, destino.with_suffix(".google.html")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # bot.lugares (mapa do Google)
    osm, google = salvar(Path(sys.argv[1]))
    print(f"mapas gerados: {osm} (OpenStreetMap) e {google} (Google)")
