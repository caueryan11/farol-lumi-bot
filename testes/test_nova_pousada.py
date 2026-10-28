"""scripts/nova_pousada.py: SQL de implantação (escape, idempotência) e o fluxo guia -> mapas -> SQL. Sem banco.
O SQL também roda contra um Postgres local descartável quando POUSADAS_TESTE_DSN está definido."""

import importlib.util
import json
import os
import pathlib
import sys

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("nova_pousada", RAIZ / "scripts" / "nova_pousada.py")
nova = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nova)

POUSADA = {
    "slug": "dona-ze", "nome": "Pousada D'Ávila", "cidade": "Cidade Exemplo/SC", "dono": "Zé",
    "faq": [{"id": "pix", "pt": {"p": "Aceita Pix?", "r": "Sim, o sinal é via Pix; custa R$ 50 ($faq$ não quebra)."},
             "es": {"p": "¿Pix?", "r": "Sí; 'comillas' y \"dobles\" y \\ barra."}},
            {"tipo": "guia", "categoria": "Mercados", "nome": "Mercado do Seu Zé", "km": 0.4, "lat": -28.0, "lon": -48.6}],
}
ARGS = dict(lat=-28.0251, lon=-48.6189, instancia="pousada-ze", telefone_aviso="5548999990000")


def test_sql_escapa_aspas_e_troca_a_etiqueta_do_dollar_quote():
    sql = nova.gerar_sql(POUSADA, **ARGS, reserva_url="https://ze.com.br/?a=1&b='x'")
    assert "'Pousada D''Ávila'" in sql and "'https://ze.com.br/?a=1&b=''x'''" in sql
    assert "$faq1$[" in sql and sql.count("$faq1$") == 2          # o FAQ tem "$faq$": a etiqueta muda
    corpo = sql.split("$faq1$")[1]
    assert json.loads(corpo) == POUSADA["faq"]                    # o JSON vai intacto (sem escape de aspas)
    assert "-28.0251" in sql and "-48.6189" in sql and "'5548999990000'" in sql and "'pousada-ze'" in sql


def test_sql_e_idempotente_e_sem_reserva_url_vira_null():
    sql = nova.gerar_sql(POUSADA, **ARGS)
    assert sql == nova.gerar_sql(POUSADA, **ARGS)                  # mesma entrada, mesmo arquivo
    assert "on conflict (slug) do update set" in sql and "faq = excluded.faq" in sql
    assert "slug = excluded.slug" not in sql and "ativo" not in sql.split("-- Conferência")[0]  # não religa pousada desligada
    valores = sql.split("values (")[1].split("\n)")[0]
    assert valores.rstrip().endswith("null")                       # reserva_url vazia = null (bot coleta pedido)


@pytest.mark.parametrize("campo, valor, erro", [
    ("telefone_aviso", "48 9999", "telefone"), ("reserva_url", "http://inseguro.com", "https"),
    ("instancia", " ", "instância"), ("slug", "Dona Zé", "slug"),
])
def test_validacao_recusa_dados_errados(campo, valor, erro):
    pousada, args = dict(POUSADA), {**ARGS, "reserva_url": ""}
    if campo == "slug":
        pousada["slug"] = valor
    else:
        args[campo] = valor
    erros = nova.validar(pousada, args["lat"], args["lon"], args["instancia"], args["telefone_aviso"], args["reserva_url"])
    assert any(erro in e for e in erros)


def test_token_da_instancia_so_entra_quando_informado():
    sem = nova.gerar_sql(POUSADA, **ARGS)
    assert "uazapi" not in sem                         # roda antes da 007 e não apaga o token já cadastrado
    com = nova.gerar_sql(POUSADA, **ARGS, uazapi_token="tok-ze-12345678", uazapi_url="https://outro.uazapi.com")
    assert "'tok-ze-12345678'" in com and "uazapi_token = excluded.uazapi_token" in com
    assert "'https://outro.uazapi.com'" in com and "uazapi_token is not null as tem_token" in com
    assert "select slug" in com and "uazapi_token," not in com.split("-- Conferência")[1]   # conferência sem o token
    so_token = nova.gerar_sql(POUSADA, **ARGS, uazapi_token="tok-ze-12345678")
    assert so_token.split("values (")[1].split("\n)")[0].rstrip().endswith("null")   # url vazia = UAZAPI_URL do env
    erros = nova.validar(POUSADA, -28.0, -48.6, "pousada-ze", "5548999990000", "", "tem espaço", "http://x")
    assert any("uazapi_token" in e for e in erros) and any("uazapi_url" in e for e in erros)
    assert any("só faz sentido" in e for e in nova.validar(POUSADA, -28.0, -48.6, "p", "5548999990000", "", "",
                                                           "https://x.uazapi.com"))


def test_fluxo_completo_gera_guia_mapas_e_sql(tmp_path):
    arquivo = tmp_path / "dona-ze.json"
    arquivo.write_text(json.dumps({**POUSADA, "faq": POUSADA["faq"][:1]}, ensure_ascii=False), encoding="utf-8")
    pesquisa = tmp_path / "pesquisa.json"
    pesquisa.write_text(json.dumps([{"categoria": "farmacia", "nome": "Farmácia Boa", "lat": -28.026, "lon": -48.619},
                                    {"categoria": "mercado", "nome": "Não confirmado", "confirmado": False}]),
                        encoding="utf-8")
    saida = nova.main([str(arquivo), "--lat", "-28.0251", "--lon", "-48.6189", "--instancia", "pousada-ze",
                       "--telefone-aviso", "+55 (48) 99999-0000", "--pesquisa", str(pesquisa)])
    assert saida == tmp_path / "dona-ze.implantacao.sql"
    salvo = json.loads(arquivo.read_text(encoding="utf-8"))
    assert salvo["lat"] == -28.0251 and [i["nome"] for i in salvo["faq"] if i.get("tipo") == "guia"] == ["Farmácia Boa"]
    assert (tmp_path / "mapas" / "dona-ze.html").is_file() and (tmp_path / "mapas" / "dona-ze.google.html").is_file()
    sql = saida.read_text(encoding="utf-8")
    assert "'5548999990000'" in sql and "Farmácia Boa" in sql


def test_reserva_url_e_lat_lon_do_json_valem_sem_argumento(tmp_path):
    """Ensaio de 04/10 (demo watu-kerere): o JSON tinha reserva_url e o SQL saía com null."""
    arquivo = tmp_path / "dona-ze.json"
    arquivo.write_text(json.dumps({**POUSADA, "lat": -28.0251, "lon": -48.6189, "reserva_url": "https://ze.com.br/reservas"},
                                  ensure_ascii=False), encoding="utf-8")
    base = [str(arquivo), "--instancia", "demo-ze", "--telefone-aviso", "5551900000022"]
    sql = nova.main(base).read_text(encoding="utf-8")
    assert "'https://ze.com.br/reservas'" in sql and "-28.0251" in sql
    sql = nova.main(base + ["--reserva-url", "", "--saida", str(tmp_path / "b.sql")]).read_text(encoding="utf-8")
    assert sql.split("values (")[1].split("\n)")[0].rstrip().endswith("null")   # '' força reserva pelo WhatsApp
    sem_lat = tmp_path / "sem.json"
    sem_lat.write_text(json.dumps(POUSADA, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit):
        nova.main([str(sem_lat), "--instancia", "i", "--telefone-aviso", "5551900000022"])


def test_fluxo_com_erro_nao_grava_nada(tmp_path):
    arquivo = tmp_path / "x.json"
    arquivo.write_text(json.dumps(POUSADA, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit):
        nova.main([str(arquivo), "--lat", "-28", "--lon", "-48", "--instancia", "i", "--telefone-aviso", "123"])
    assert not (tmp_path / "mapas").exists() and not list(tmp_path.glob("*.sql"))


def _avaliador():
    spec = importlib.util.spec_from_file_location("avaliar_pousada", RAIZ / "scripts" / "avaliar_pousada.py")
    av = importlib.util.module_from_spec(spec)
    sys.modules["avaliar_pousada"] = av   # o @dataclass procura o módulo em sys.modules
    spec.loader.exec_module(av)
    return av


def test_avaliacao_tem_32_perguntas_e_criterios_que_pegam_erro():
    av = _avaliador()
    idiomas = [c.idioma for c in av.CASOS]
    assert (idiomas.count("pt"), idiomas.count("es"), idiomas.count("en")) == (16, 13, 3)
    assert len({c.id for c in av.CASOS}) == 32
    reveillon = next(c for c in av.CASOS if c.id == "pt-reveillon")
    bom = "Aqui é o atendimento virtual. Disponibilidade e valor no site www.pousadaexemplo.com.br."
    assert av.avaliar(reveillon, "responder", "pt", bom, False) == []
    assert av.avaliar(reveillon, "responder", "pt", "Sim, temos vaga! Reserve em pousadaexemplo.com.br", False)
    assert av.avaliar(reveillon, "responder", "es", bom, False) == ["idioma es (esperado pt)"]


def test_criterios_da_fase_2_pegam_as_respostas_ruins_da_fase_1():
    """As 4 respostas ruins da avaliação de 04/10 (relatorios/avaliacao-exemplo-rosa-2026-10-04-depois.md)."""
    av = _avaliador()
    caso = {c.id: c for c in av.CASOS}
    aeroporto = ("Do Aeroporto de Florianópolis são cerca de 96 km (1h30 de carro). A pousada não oferece transfer, "
                 "mas Carla podem indicar opções.")
    assert any("transfer" in f for f in av.avaliar(caso["pt-aeroporto"], "responder", "pt", aeroporto, False))
    farmacia = "Sim! Tem a Farmácia Nossa Senhora dos Navegantes em Ibiraquera, bem pertinho."
    assert "disse que é perto sem dar a distância" in av.avaliar(caso["pt-farmacia"], "responder", "pt", farmacia, False)
    assert av.avaliar(caso["pt-farmacia"], "responder", "pt", "A Farmácia Navegantes fica em Ibiraquera.", False) == []
    pix = "Las reservas se hacen solo por el sitio www.pousadaexemplo.com.br. Lá el sitio tiene la mejor tarifa."
    assert any("português" in f for f in av.avaliar(caso["es-pix"], "responder", "es", pix, False))
    confirmar = "¡Hola Martín! Pasé tu solicitud a Carla (dueña de la Pousada Exemplo do Rosa) para que confirmen tu reserva."
    falhas = av.avaliar(caso["es-confirmar"], "encaminhar", "es", confirmar, False)
    assert "rótulo entre parênteses no nome do dono" in falhas and any("pousadaexemplo" in f for f in falhas)
    bom = ("La reserva se hace y se confirma solo por el sitio https://www.pousadaexemplo.com.br. Si ya pagaste y "
           "no te llegó la confirmación, le pasé tu mensaje a Carla.")
    assert av.avaliar(caso["es-confirmar"], "encaminhar", "es", bom, False) == []


def test_criterios_da_fase_3_pegam_farmacia_e_vaga_da_fase_2():
    """Respostas da fase 2, rodada 2 (relatorios/avaliacao-exemplo-rosa-2026-10-04-fase2-rodada2.md) que passaram."""
    av = _avaliador()
    caso = {c.id: c for c in av.CASOS}
    farmacia = ("Sim, tem a Farmácia Nossa Senhora dos Navegantes em Ibiraquera. Todos os supermercados que ficam a até "
                "1,4 km também costumam vender medicamentos básicos.")
    assert av.avaliar(caso["pt-farmacia"], "responder", "pt", farmacia, False)
    es = "Hay una farmacia: la Farmácia Nossa Senhora dos Navegantes. También hay supermercados a 630 m y 1,4 km del centrinho."
    assert av.avaliar(caso["es-farmacia"], "responder", "es", es, False)
    reveillon = "Para Año Nuevo, tenemos bungalós disponibles. Mirá en https://www.pousadaexemplo.com.br."
    assert av.avaliar(caso["es-reveillon"], "responder", "es", reveillon, False)


def test_reavaliar_respeita_pousada_que_reserva_pelo_whatsapp(tmp_path):
    """--reavaliar passava so_site=True sempre: "para que confirmen" reprovava pousada que reserva pelo WhatsApp."""
    av = _avaliador()
    casos = av.casos_genericos("", [])
    resposta = "¡Hola Martín! Le pasé tu mensaje al equipo de la posada para que confirmen la reserva."
    md = tmp_path / "r.md"
    md.write_text(f"| 1 | es-confirmar | x | es | encaminhar | 1 | ✅ | p | {resposta} |\n", encoding="utf-8")
    assert av.reavaliar(md, casos, frozenset(), so_site=False)[0]["falhas"] == []
    assert av.reavaliar(md, casos, frozenset(), so_site=True)[0]["falhas"]


def test_perguntas_genericas_para_outra_pousada():
    av = _avaliador()
    casos = av.casos_genericos("https://hbook.hsystem.com.br/booking?companyId=1", [])
    assert len(casos) == 10 and all(c.pergunta for c in casos)
    reserva = next(c for c in casos if c.id == "pt-reserva")
    assert av.avaliar(reserva, "responder", "pt", "Reserve em https://hbook.hsystem.com.br/booking?companyId=1", False) == []
    sem_site = av.casos_genericos("", [{"id": "t", "pt": {"p": "Tem transfer?", "r": "Não temos transfer."}}])
    assert next(c for c in sem_site if c.id == "pt-transfer").nunca == [] and not next(c for c in sem_site if c.id == "pt-reserva").todos


@pytest.mark.skipif(not os.environ.get("POUSADAS_TESTE_DSN"), reason="defina POUSADAS_TESTE_DSN (Postgres local descartável)")
def test_sql_roda_duas_vezes_no_postgres():
    import psycopg2

    conn = psycopg2.connect(os.environ["POUSADAS_TESTE_DSN"])
    try:
        with conn.cursor() as cur:
            for m in sorted((RAIZ / "supabase_pousadas").glob("*.sql")):
                cur.execute(m.read_text(encoding="utf-8"))
            cur.execute(nova.gerar_sql(POUSADA, **ARGS))
            cur.execute(nova.gerar_sql({**POUSADA, "nome": "Pousada D'Ávila 2"}, **ARGS, reserva_url="https://ze.com.br"))
            cur.execute("select nome, reserva_url, faq, ativo from public.pousadas where slug = 'dona-ze'")
            linhas = cur.fetchall()
            # token da instância (007): grava; rodar de novo sem o token não apaga o já cadastrado
            cur.execute(nova.gerar_sql(POUSADA, **ARGS, uazapi_token="tok-ze-12345678"))
            cur.execute(nova.gerar_sql(POUSADA, **ARGS))
            cur.execute("select uazapi_token, uazapi_url from public.pousadas where slug = 'dona-ze'")
            token = cur.fetchone()
        assert len(linhas) == 1
        nome, url, faq, ativo = linhas[0]
        assert nome == "Pousada D'Ávila 2" and url == "https://ze.com.br" and faq == POUSADA["faq"] and ativo is True
        assert token == ("tok-ze-12345678", None)
    finally:
        conn.rollback()
        conn.close()
