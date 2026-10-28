"""Lugares perto da pousada pela Places API (New) do Google, consultados na hora em que o hóspede pergunta.

Os termos do Google proíbem guardar o conteúdo da Places API (só o place_id pode ser guardado), então nada daqui vai
para o banco: o bot usa o resultado na resposta e cita "Fonte: Google Maps". A opinião do dono sobre um lugar é dado
nosso e fica no guia (item tipo="guia" com "place_id" e "opiniao_dono"); ela é juntada ao resultado pelo place_id.

Custo: os campos pedidos (horário, nota, faixa de preço) caem no SKU Nearby Search Enterprise
(1.000 chamadas grátis por mês; depois ~US$ 35 a cada 1.000). Conferir em
https://developers.google.com/maps/billing-and-pricing/pricing
"""

from __future__ import annotations

import logging
import math

import httpx

log = logging.getLogger(__name__)

URL = "https://places.googleapis.com/v1/places:searchNearby"
URL_TEXTO = "https://places.googleapis.com/v1/places:searchText"
CAMPOS = ",".join(f"places.{c}" for c in (
    "id", "displayName", "formattedAddress", "location", "googleMapsUri", "businessStatus", "rating",
    "userRatingCount", "priceLevel", "currentOpeningHours.openNow", "regularOpeningHours.weekdayDescriptions"))

# categoria que o bot pede -> tipos da Tabela A da Places API (New)
CATEGORIAS = {
    "mercado": ["supermarket", "grocery_store", "convenience_store", "market"],  # "market": mercadinho de praia
    "padaria": ["bakery"],
    "farmacia": ["pharmacy", "drugstore"],
    "roupas": ["clothing_store", "shoe_store"],
    "restaurante": ["restaurant"],
    "cafe": ["cafe", "coffee_shop"],
    "sorveteria": ["ice_cream_shop"],
    "bar": ["bar"],
    "caixa_eletronico": ["atm", "bank"],
    "posto_gasolina": ["gas_station"],
    "saude": ["hospital", "doctor", "medical_lab"],
    "praia_passeio": ["beach", "tourist_attraction", "hiking_area", "park"],
}
PRECO = {"PRICE_LEVEL_FREE": "grátis", "PRICE_LEVEL_INEXPENSIVE": "$ (barato)", "PRICE_LEVEL_MODERATE": "$$ (moderado)",
         "PRICE_LEVEL_EXPENSIVE": "$$$ (caro)", "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$ (muito caro)"}
FATOR_RUA = 1.3


def km_rua(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return round(2 * 6371.0 * math.asin(math.sqrt(a)) * FATOR_RUA, 2)


def buscar(chave: str, lat: float, lon: float, categoria: str, *, idioma: str = "pt", raio_m: int = 10000,
           maximo: int = 5, regionais: int = 3, opinioes: dict[str, str] | None = None,
           http: httpx.Client | None = None) -> dict:
    """{"lugares": os `maximo` mais perto, "mais_conhecidos_da_regiao": até `regionais` lugares mais avaliados do raio
    (ex.: supermercado grande em outra localidade), "fonte": "Google Maps"}, ou {"erro": ...}.
    Duas buscas: a API devolve no máximo 20 por busca, e só a busca por distância não alcança o comércio mais longe."""
    tipos = CATEGORIAS.get(categoria)
    if not tipos:
        return {"erro": f"categoria desconhecida: {categoria}; use uma de {', '.join(CATEGORIAS)}"}
    cliente = http or httpx.Client(timeout=10)
    vistos: dict[str, dict] = {}
    por_relevancia: list[str] = []
    for ordem in ("DISTANCE", "POPULARITY"):
        corpo = {"includedPrimaryTypes": tipos, "maxResultCount": 20, "rankPreference": ordem,
                 "languageCode": {"es": "es", "en": "en"}.get(idioma, "pt-BR"),
                 "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lon}, "radius": float(raio_m)}}}
        try:
            r = cliente.post(URL, json=corpo, headers={"X-Goog-Api-Key": chave, "X-Goog-FieldMask": CAMPOS})
            r.raise_for_status()
        except httpx.HTTPError as e:
            detalhe = e.response.text[:300] if isinstance(e, httpx.HTTPStatusError) else ""
            log.warning("places: falha na busca (%s, %s): %s %s", categoria, ordem, type(e).__name__, detalhe)
            if ordem == "DISTANCE":
                return {"erro": "busca de lugares indisponível agora; use o guia local"}
            break
        for p in (r.json() or {}).get("places", []):
            item = _item(p, lat, lon, opinioes)
            if item is None:
                continue
            chave_lugar = p.get("id") or item["nome"]
            vistos.setdefault(chave_lugar, item)
            if ordem == "POPULARITY":
                por_relevancia.append(chave_lugar)
    perto = sorted(vistos.values(), key=lambda i: i["distancia_km"])[:maximo]
    regiao = [vistos[k] for k in por_relevancia if vistos[k] not in perto][:regionais]
    return {"lugares": perto, "mais_conhecidos_da_regiao": regiao, "fonte": "Google Maps"}


def buscar_texto(chave: str, lat: float, lon: float, termo: str, *, idioma: str = "pt", raio_m: int = 10000,
                 maximo: int = 5, opinioes: dict[str, str] | None = None, http: httpx.Client | None = None) -> dict:
    """Busca por texto livre ("sorveteria", "aluguel de prancha", "pet shop") perto da pousada, para o que não tem
    categoria fixa. Mesmo formato de `buscar`; lugares fora do raio ficam de fora."""
    termo = (termo or "").strip()[:80]
    if not termo:
        return {"erro": "diga o que procurar em `termo` (ex.: sorveteria)"}
    corpo = {"textQuery": termo, "pageSize": 20, "rankPreference": "DISTANCE",
             "languageCode": {"es": "es", "en": "en"}.get(idioma, "pt-BR"),
             "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lon}, "radius": float(raio_m)}}}
    cliente = http or httpx.Client(timeout=10)
    try:
        r = cliente.post(URL_TEXTO, json=corpo, headers={"X-Goog-Api-Key": chave, "X-Goog-FieldMask": CAMPOS})
        r.raise_for_status()
    except httpx.HTTPError as e:
        detalhe = e.response.text[:300] if isinstance(e, httpx.HTTPStatusError) else ""
        log.warning("places: falha na busca por texto: %s %s", type(e).__name__, detalhe)
        return {"erro": "busca de lugares indisponível agora; use o guia local"}
    itens = [i for i in (_item(p, lat, lon, opinioes) for p in (r.json() or {}).get("places", []))
             if i is not None and i["distancia_km"] <= raio_m * FATOR_RUA / 1000]
    itens.sort(key=lambda i: i["distancia_km"])
    return {"lugares": itens[:maximo], "fonte": "Google Maps"}


def _item(p: dict, lat: float, lon: float, opinioes: dict[str, str] | None) -> dict | None:
    if p.get("businessStatus") not in (None, "OPERATIONAL"):
        return None
    loc = p.get("location") or {}
    if "latitude" not in loc:
        return None
    item = {"nome": (p.get("displayName") or {}).get("text", ""), "distancia_km": km_rua(lat, lon, loc["latitude"], loc["longitude"]),
            "endereco": p.get("formattedAddress", ""), "link_maps": p.get("googleMapsUri", "")}
    if (p.get("currentOpeningHours") or {}).get("openNow") is not None:
        item["aberto_agora"] = p["currentOpeningHours"]["openNow"]
    if (p.get("regularOpeningHours") or {}).get("weekdayDescriptions"):
        item["horarios"] = p["regularOpeningHours"]["weekdayDescriptions"]
    if p.get("rating"):
        item["nota_google"] = f"{p['rating']} ({p.get('userRatingCount', 0)} avaliações)"
    if p.get("priceLevel") in PRECO:
        item["faixa_de_preco_google"] = PRECO[p["priceLevel"]]
    if opinioes and p.get("id") in opinioes:
        item["opiniao_do_dono"] = opinioes[p["id"]]
    return item


def opinioes_do_guia(faq: list[dict]) -> dict[str, str]:
    """place_id -> opinião do dono, dos itens do guia que têm os dois."""
    return {i["place_id"]: i["opiniao_dono"] for i in faq
            if i.get("tipo") == "guia" and i.get("place_id") and i.get("opiniao_dono")}
