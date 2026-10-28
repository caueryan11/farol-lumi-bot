"""Implantação de uma pousada nova: guia local, mapas e o SQL para cadastrar no banco das pousadas.

    python scripts/nova_pousada.py pousadas/exemplo-rosa.json --lat -28.1300000 --lon -48.6500000 \
        --instancia pousada-rosa --telefone-aviso 5548999990000 \
        [--reserva-url https://www.pousadaexemplo.com.br] [--pesquisa pousadas/guia-exemplo-rosa-pesquisa.json] \
        [--saida pousadas/exemplo-rosa.implantacao.sql] [--uazapi-token <token da instância> [--uazapi-url https://...]]

--lat/--lon e --reserva-url, se omitidos, vêm do JSON ("lat", "lon", "reserva_url").
--uazapi-token/--uazapi-url (opcionais; precisam da 007_multi_instancia.sql): token e servidor da instância desta
pousada, para várias pousadas no mesmo servidor. Sem eles o SQL não mexe no token já cadastrado e a pousada sem
token usa o UAZAPI_TOKEN do ambiente. O token é segredo: só vai para o .sql (fora do git), nunca para a tela.

1. Com --pesquisa, junta os lugares ao FAQ como guia local (scripts/montar_guia.py); sem ela, só grava lat/lon
   no JSON e mantém o guia que já existir.
2. Gera os mapas em pousadas/mapas/<slug>.html e <slug>.google.html (scripts/gerar_mapa.py). Faça commit deles:
   o app serve /mapa/<slug> a partir do repositório.
3. Gera o .sql (padrão: pousadas/<slug>.implantacao.sql, fora do git porque tem o telefone do dono) com um
   insert ... on conflict (slug) do update: pode rodar de novo para atualizar FAQ, localização, link e telefone.
   NÃO executa nada no banco: cole o arquivo no SQL Editor do projeto Supabase das POUSADAS (não no das nutris).

Formato do JSON: o de pousadas/exemplo-rosa.json (slug, nome, cidade, dono, faq). O FAQ vai como jsonb entre
dollar-quotes ($faq$...$faq$), então aspas e acentos do texto não precisam de escape.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))                   # bot.lugares (mapa do Google)
sys.path.insert(0, str(RAIZ / "scripts"))

import gerar_mapa   # noqa: E402
import montar_guia  # noqa: E402


def texto(valor) -> str:
    """Texto SQL entre aspas simples (aspas internas dobradas; o Supabase usa standard_conforming_strings)."""
    t = str(valor)
    if chr(0) in t:  # o Postgres não aceita caractere nulo em texto
        raise ValueError("texto com caractere nulo")
    return "'" + t.replace("'", "''") + "'"


def literal(valor) -> str:
    """Número como número, vazio como null, o resto como texto."""
    if valor is None or valor == "":
        return "null"
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        return repr(valor)
    return texto(valor)


def dollar_quote(texto: str, base: str = "faq") -> str:
    """$faq$texto$faq$, trocando a etiqueta se o texto já contiver ela."""
    etiqueta, n = f"${base}$", 0
    while etiqueta in texto:
        n += 1
        etiqueta = f"${base}{n}$"
        if n > 1000:
            raise ValueError("não achei etiqueta de dollar-quote livre")
    return f"{etiqueta}{texto}{etiqueta}"


def validar(pousada: dict, lat: float, lon: float, instancia: str, telefone_aviso: str, reserva_url: str,
            uazapi_token: str = "", uazapi_url: str = "") -> list[str]:
    erros = []
    if not re.fullmatch(r"[a-z0-9-]{1,60}", str(pousada.get("slug") or "")):
        erros.append("slug: só letras minúsculas, números e hífen (vira o link /mapa/<slug>)")
    for campo in ("nome", "dono"):
        if not str(pousada.get(campo) or "").strip():
            erros.append(f"{campo} vazio no JSON")
    faq = pousada.get("faq")
    if not isinstance(faq, list) or not any(i.get("tipo") != "guia" for i in faq if isinstance(i, dict)):
        erros.append("faq vazio no JSON")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        erros.append("lat/lon fora do intervalo")
    if not instancia.strip():
        erros.append("instância da uazapi vazia")
    if not re.fullmatch(r"\d{12,13}", telefone_aviso):
        erros.append("telefone de aviso: só dígitos com DDI e DDD (ex.: 5548999990000)")
    if reserva_url and not re.fullmatch(r"https://\S+", reserva_url):
        erros.append("reserva_url precisa começar com https://")
    if uazapi_token and not re.fullmatch(r"[^\s'\\]{8,200}", uazapi_token):
        erros.append("uazapi_token: o token da instância, sem espaços (copie do painel da uazapi)")
    if uazapi_url and not re.fullmatch(r"https://[^\s']+", uazapi_url):
        erros.append("uazapi_url precisa começar com https://")
    if uazapi_url and not uazapi_token:
        erros.append("uazapi_url só faz sentido com --uazapi-token")
    return erros


def gerar_sql(pousada: dict, *, lat: float, lon: float, instancia: str, telefone_aviso: str,
              reserva_url: str = "", origem: str = "", uazapi_token: str = "", uazapi_url: str = "") -> str:
    """Insert ou update por slug, pronto para o SQL Editor. Mesma entrada, mesmo SQL (pode rodar duas vezes).
    uazapi_token/uazapi_url só entram quando o token é informado (colunas da 007): sem ele, o já cadastrado fica."""
    faq = json.dumps(pousada["faq"], ensure_ascii=False, indent=1)
    valores = {
        "slug": literal(pousada["slug"]), "nome": literal(pousada["nome"]), "cidade": texto(pousada.get("cidade") or ""),
        "dono": literal(pousada["dono"]), "telefone_aviso": literal(telefone_aviso), "instancia": literal(instancia),
        "faq": dollar_quote(faq) + "::jsonb", "lat": literal(float(lat)), "lon": literal(float(lon)),
        "reserva_url": literal(reserva_url),
    }
    if uazapi_token:
        valores["uazapi_token"] = literal(uazapi_token)
        valores["uazapi_url"] = literal(uazapi_url)   # vazio = null = UAZAPI_URL do ambiente
    colunas = list(valores)
    atualiza = ",\n  ".join(f"{c} = excluded.{c}" for c in colunas if c != "slug")
    return (
        f"-- Implantação da pousada {pousada['nome']} (slug {pousada['slug']}). Gerado por scripts/nova_pousada.py"
        f"{' a partir de ' + origem if origem else ''}.\n"
        "-- Cole no SQL Editor do Supabase das POUSADAS (não no das nutris). Pode rodar de novo: atualiza pelo slug.\n"
        f"insert into public.pousadas ({', '.join(colunas)})\nvalues (\n  "
        + ",\n  ".join(valores[c] for c in colunas)
        + f"\n)\non conflict (slug) do update set\n  {atualiza};\n\n"
        "-- Conferência:\n"
        "select slug, nome, instancia, telefone_aviso, lat, lon, reserva_url, ativo, jsonb_array_length(faq) as itens_faq"
        + (", uazapi_token is not null as tem_token, uazapi_url" if uazapi_token else "") + "\n"
        f"from public.pousadas where slug = {valores['slug']};\n"
    )


def main(argv: list[str] | None = None) -> Path:
    ap = argparse.ArgumentParser(description="Guia, mapas e SQL de implantação de uma pousada nova.")
    ap.add_argument("pousada", help="JSON da pousada (formato de pousadas/exemplo-rosa.json)")
    ap.add_argument("--lat", type=float, help="padrão: o lat do JSON")
    ap.add_argument("--lon", type=float, help="padrão: o lon do JSON")
    ap.add_argument("--instancia", required=True, help="nome da instância na uazapi")
    ap.add_argument("--telefone-aviso", required=True, help="WhatsApp do dono, só dígitos (ex.: 5548999990000)")
    ap.add_argument("--reserva-url", default=None,
                    help="pousada que só reserva pelo site (o bot manda o link). Padrão: o reserva_url do JSON; "
                         "--reserva-url '' força reserva pelo WhatsApp")
    ap.add_argument("--pesquisa", help="JSON da pesquisa do guia local (scripts/montar_guia.py)")
    ap.add_argument("--saida", help="arquivo .sql (padrão: pousadas/<slug>.implantacao.sql)")
    ap.add_argument("--uazapi-token", default="",
                    help="token da instância desta pousada na uazapi (várias pousadas no mesmo servidor; precisa da "
                         "007_multi_instancia.sql). Sem ele, o token já cadastrado não muda")
    ap.add_argument("--uazapi-url", default="", help="servidor da uazapi desta instância, se não for o UAZAPI_URL")
    a = ap.parse_args(argv)

    arquivo = Path(a.pousada)
    telefone = re.sub(r"\D", "", a.telefone_aviso)
    pousada = json.loads(arquivo.read_text(encoding="utf-8"))
    # sem o argumento, vale o que está no JSON: antes o reserva_url do JSON era ignorado e o SQL gravava null
    # (o bot passava a coletar pedido pelo WhatsApp numa pousada que só reserva pelo site)
    if a.reserva_url is None:
        a.reserva_url = str(pousada.get("reserva_url") or "")
    a.lat = a.lat if a.lat is not None else pousada.get("lat")
    a.lon = a.lon if a.lon is not None else pousada.get("lon")
    if not isinstance(a.lat, (int, float)) or not isinstance(a.lon, (int, float)):
        sys.exit("Não gerei nada:\n- informe --lat e --lon (o JSON não tem lat/lon)")
    a.uazapi_token, a.uazapi_url = a.uazapi_token.strip(), a.uazapi_url.strip().rstrip("/")
    erros = validar(pousada, a.lat, a.lon, a.instancia, telefone, a.reserva_url, a.uazapi_token, a.uazapi_url)
    if erros:
        sys.exit("Não gerei nada:\n- " + "\n- ".join(erros))
    if pousada.get("ficticio") or pousada.get("rascunho"):
        print("ATENÇÃO: o JSON está marcado como fictício/rascunho; confirme o FAQ com os donos antes de ligar o bot.")

    if a.pesquisa:
        n, fora = montar_guia.montar(a.pesquisa, arquivo, a.lat, a.lon)
        print(f"guia: {n} lugares ({fora} não confirmados ficaram de fora)")
    else:
        pousada["lat"], pousada["lon"] = a.lat, a.lon
        arquivo.write_text(json.dumps(pousada, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("guia: sem --pesquisa, mantive o guia do JSON")
    pousada = json.loads(arquivo.read_text(encoding="utf-8"))
    osm, google = gerar_mapa.salvar(arquivo)
    print(f"mapas: {osm} e {google}")

    saida = Path(a.saida) if a.saida else arquivo.parent / f"{pousada['slug']}.implantacao.sql"
    saida.write_text(gerar_sql(pousada, lat=a.lat, lon=a.lon, instancia=a.instancia.strip(), telefone_aviso=telefone,
                               reserva_url=a.reserva_url.strip(), origem=arquivo.name, uazapi_token=a.uazapi_token,
                               uazapi_url=a.uazapi_url), encoding="utf-8")
    print(f"SQL: {saida} (cole no SQL Editor do Supabase das pousadas; nada foi executado no banco)")
    return saida


if __name__ == "__main__":
    main()
