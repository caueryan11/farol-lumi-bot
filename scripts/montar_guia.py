"""Junta a pesquisa de lugares da região (JSON) ao FAQ de uma pousada como itens do guia local.

    python scripts/montar_guia.py pousadas/guia-exemplo-rosa-pesquisa.json pousadas/exemplo-rosa.json \
        --lat -28.13074 --lon -48.66448

Distância: linha reta (haversine) x 1,3, uma aproximação da distância de rua. Lugar sem coordenada fica sem distância.
Itens não confirmados (confirmado=false) ficam de fora. A opinião do dono (opiniao_dono) entra na implantação.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

FATOR_RUA = 1.3
CATEGORIAS = {"mercado": "Mercados", "padaria": "Padarias", "farmacia": "Farmácias", "roupas": "Roupas e moda praia",
              "restaurante": "Restaurantes", "posto": "Postos de gasolina", "banco": "Bancos e caixas eletrônicos",
              "saude": "Saúde", "passeio": "Praias, trilhas e passeios", "como_chegar": "Como chegar"}
# a pesquisa às vezes escreve a distância na descrição; quem calcula a distância é este script
_DIST_NA_DESCRICAO = re.compile(r"[;,]?\s*(o mais (perto|próximo)[^;()]*)?\s*\(~?\s*[\d.,]+\s*km\)", re.I)


def km_rua(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)) * FATOR_RUA, 2)


def item_do_guia(lugar: dict, lat: float, lon: float) -> dict:
    km = None
    if lugar.get("lat") not in (None, "") and lugar.get("lon") not in (None, ""):
        km = km_rua(lat, lon, float(lugar["lat"]), float(lugar["lon"]))
    categoria = CATEGORIAS.get(lugar["categoria"], lugar["categoria"])
    if lugar.get("km_estrada"):  # longe, a linha reta engana: vale a distância de estrada da pesquisa (rota OSRM)
        km = float(lugar["km_estrada"])
    item = {"tipo": "guia", "categoria": categoria, "nome": lugar["nome"], "km": km}
    if km is not None:
        item["lat"], item["lon"] = float(lugar["lat"]), float(lugar["lon"])  # para o mapa (scripts/gerar_mapa.py)
    if lugar.get("descricao"):
        lugar = {**lugar, "descricao": _DIST_NA_DESCRICAO.sub("", lugar["descricao"]).strip(" ;,")}
    for chave in ("descricao", "endereco", "horario", "contato", "preco", "temporada", "opiniao_dono", "fonte", "tempo"):
        if lugar.get(chave) not in (None, "", []):
            item[chave] = lugar[chave]
    return item


def montar(pesquisa: str | Path, arquivo_pousada: str | Path, lat: float, lon: float) -> tuple[int, int]:
    """Troca o guia do JSON da pousada pelo da pesquisa e grava. Devolve (lugares no guia, não confirmados)."""
    lugares = json.loads(Path(pesquisa).read_text(encoding="utf-8"))
    pousada = json.loads(Path(arquivo_pousada).read_text(encoding="utf-8"))
    guia = [item_do_guia(l, lat, lon) for l in lugares if l.get("confirmado", True)]
    pousada["lat"], pousada["lon"] = lat, lon
    pousada["faq"] = [i for i in pousada["faq"] if i.get("tipo") != "guia"] + guia
    Path(arquivo_pousada).write_text(json.dumps(pousada, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(guia), len(lugares) - len(guia)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pesquisa")
    ap.add_argument("pousada")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    a = ap.parse_args()
    n, fora = montar(a.pesquisa, a.pousada, a.lat, a.lon)
    print(f"{n} lugares no guia ({fora} não confirmados ficaram de fora)")


if __name__ == "__main__":
    main()
