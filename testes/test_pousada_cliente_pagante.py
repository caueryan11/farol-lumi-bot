"""Correções para o 1º cliente pagante (Refúgio do Exemplo), a partir do QA adversarial de 09/10/2026
(ideias-2026-10-09/teste-bot-refugio.md): datas e preços em datas distantes, pedido com nome placeholder e FAQ
que mandava o hóspede para o WhatsApp do próprio bot. Sem internet: IA simulada."""

import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bot import pousada as bot_pousada
from bot.pousada import (PousadaInfo, atender_hospede, calendario, checar_mensagem, conferir_data_passada,
                         conferir_dias_da_semana, noites_da_estadia, nome_de_hospede_valido, promete_encaminhar,
                         texto_faq, texto_guia, validar_pedido, valores_em_reais)
from testes.test_pousada import AGORA, INFO, Api, _resp, _resposta

RAIZ = Path(__file__).resolve().parents[1]
REFUGIO = json.loads((RAIZ / "pousadas" / "exemplo-refugio.json").read_text(encoding="utf-8"))
INFO_REFUGIO = PousadaInfo(REFUGIO["nome"], REFUGIO["dono"], REFUGIO["cidade"], REFUGIO["faq"], reserva_url="")
FAQ_REFUGIO = texto_faq(REFUGIO["faq"])
HOJE_QA = datetime(2026, 10, 9, 14, 30, tzinfo=ZoneInfo("America/Sao_Paulo"))   # sexta, data do QA


# --------------------------------------------------------------------- 1. datas distantes e preços
def test_calendario_cobre_13_meses_com_os_fins_de_semana():
    cal = calendario(date(2026, 10, 9))
    assert "sex 09/10; sáb 10/10; dom 11/10" in cal                   # os 21 dias continuam
    assert "nov/2026: sex 6, 13, 20, 27 · sáb 7, 14, 21, 28 · dom 1, 8, 15, 22, 29" in cal   # 20/11/2026 é sexta
    assert "jan/2027: sex 1, 8, 15, 22, 29 · sáb 2, 9, 16, 23, 30 · dom 3, 10, 17, 24, 31" in cal
    assert "out/2027:" in cal and "nov/2027" not in cal                 # 13 meses: out/2026 a out/2027
    assert "inclusive dezembro de 2026 e o ano de 2027" in cal and "anteriores a 09/10/2026" in cal


def test_prompt_manda_informar_diaria_por_noite_e_nao_somar_total():
    api = Api(_resp(_resposta("Oi!")))
    atender_hospede("oi", INFO_REFUGIO, cliente=api, agora=HOJE_QA)
    fixo, dinamico = (b["text"] for b in api.chamadas[0]["system"])
    assert 'nada de "2 x R$ 750 = R$ 1.500"' in fixo and "Marcos confirma o valor final" in fixo
    assert "cruza dois períodos de tarifa" in fixo and 'nem diga que uma data futura "já passou"' in fixo
    assert "nov/2026: sex 6, 13, 20, 27" in dinamico


def test_valores_em_reais_entende_os_formatos():
    assert valores_em_reais("r$ 700, r$750,00, r$ 1.600 e r$ 1,600 e 190 reais") == {700, 750, 1600, 190}
    assert valores_em_reais("são 2 noites em 20/11") == set()


@pytest.mark.parametrize("texto", [
    "São 3 noites: sexta 20/11 e sábado 21/11 a R$ 750 e domingo 22/11 a R$ 700, mais R$ 190 de limpeza.",
    "Over New Year (Dec 26 to Jan 5) it is R$ 1,600 per night, plus the R$ 190 cleaning fee.",
    "En temporada alta la noche sale R$ 950,00 por casa.",
])
def test_valores_do_faq_podem_sair(texto):
    assert checar_mensagem(texto, idioma="pt", permitidos=FAQ_REFUGIO, hoje=HOJE_QA.date()) is None


@pytest.mark.parametrize("texto", [
    "3 noites a R$ 700, totalizando R$ 2.100, mais a taxa de limpeza de R$ 190.",    # pt02 do QA
    "De 12 a 18 de dezembro são 6 noites a R$ 950. Total: R$ 5.700.",                # pt04 do QA
    "2 × R$ 750 = R$ 1.500 + R$ 190 de limpeza = R$ 1.690.",                          # pt57 do QA
    "For 2 nights it comes to R$ 1,500.",
])
def test_total_ou_valor_fora_do_faq_e_barrado(texto):
    motivo = checar_mensagem(texto, permitidos=FAQ_REFUGIO)
    assert motivo and "não está no FAQ" in motivo and "não some o total" in motivo


def test_sem_faq_para_conferir_o_valor_nao_e_barrado():
    assert checar_mensagem("A diária é R$ 280.") is None            # permitidos=None: não confere


def test_valor_da_busca_de_lugares_pode_sair():
    assert checar_mensagem("O almoço no Sabor do Mar custa R$ 65.", permitidos=FAQ_REFUGIO + '\n{"preco": "R$ 65"}') is None


def test_data_futura_dita_como_passada_e_barrada():
    hoje = HOJE_QA.date()
    assert "ainda não passou" in conferir_data_passada("O Réveillon (26/12 a 05/01) já passou.", hoje)   # es01 do QA
    assert "ainda não passou" in conferir_data_passada("New Year's (Dec 26 to Jan 5) has already passed.", hoje)   # Dec/Jan abreviados
    assert "ainda não passou" in conferir_data_passada("El Año Nuevo del 31 de diciembre de 2026 ya pasó.", hoje)
    assert conferir_data_passada("O feriado de 7 de setembro já passou.", hoje) is None        # passou mesmo
    assert conferir_data_passada("O feriado de 12/10/2025 já passou.", hoje) is None
    assert conferir_data_passada("O Réveillon (26/12 a 05/01) tem mínimo de 7 noites.", hoje) is None
    assert "ainda não passou" in checar_mensagem("O Natal de 25/12 já passou.", hoje=hoje)
    assert checar_mensagem("O Natal de 25/12 já passou.") is None                              # sem hoje, não confere


def test_total_inventado_volta_para_o_modelo_e_sai_sem_total():
    api = Api(_resp(_resposta("São 3 noites a R$ 700, total R$ 2.100 + R$ 190.")),
              _resp(_resposta("São 3 noites: sexta 20/11 e sábado 21/11 a R$ 750 e domingo 22/11 a R$ 700, mais "
                              "R$ 190 de limpeza. O Marcos confirma o valor final no pedido.")))
    r = atender_hospede("Quanto fica de 20 a 23 de novembro?", INFO_REFUGIO, cliente=api, agora=HOJE_QA)
    assert r.passos == 2 and "2.100" not in r.mensagem and not r.fallback
    assert "R$ 2100 não está no FAQ" in api.chamadas[1]["messages"][-1]["content"][0]["content"]


def test_ferramenta_noites_da_estadia_da_o_dia_da_semana_de_cada_noite():
    r = noites_da_estadia({"entrada": "2026-11-20", "saida": "2026-11-23"}, HOJE_QA.date())
    assert r["noites"] == 3 and r["lista"] == ["sexta 20/11/2026", "sábado 21/11/2026", "domingo 22/11/2026"]
    assert r["noites_de_sexta_e_sabado"] == ["sexta 20/11/2026", "sábado 21/11/2026"]
    assert r["noites_de_domingo_a_quinta"] == ["domingo 22/11/2026"] and r["saida"] == "segunda 23/11/2026"
    assert "aviso" not in r
    assert "já passou" in noites_da_estadia({"entrada": "2026-10-01", "saida": "2026-10-03"}, HOJE_QA.date())["aviso"]
    assert "erro" in noites_da_estadia({"entrada": "20/11", "saida": "23/11"}, HOJE_QA.date())
    assert "erro" in noites_da_estadia({"entrada": "2026-11-23", "saida": "2026-11-20"}, HOJE_QA.date())
    assert any(f["name"] == "noites_da_estadia" for f in bot_pousada.FERRAMENTAS)


def test_lista_de_noites_com_dia_da_semana_certo_nao_e_barrada():
    """Reteste de 13/10 (pt05, nd02, nd04, es04): "domingo 03/01, segunda 04/01" era lido como "03/01 = segunda" e o
    modelo caía em 6 recusas seguidas."""
    hoje = HOJE_QA.date()
    assert conferir_dias_da_semana("5 noites (domingo 03/01, segunda 04/01, terça 05/01, quarta 06/01 e quinta 07/01).", hoje) is None
    assert conferir_dias_da_semana("4 noches: miércoles 10/02, jueves 11/02, viernes 12/02 y sábado 13/02.", hoje) is None
    assert "03/01/2027 é domingo" in conferir_dias_da_semana("5 noites (segunda 03/01, terça 04/01).", hoje)
    assert "25/12/2026 é sexta" in conferir_dias_da_semana("saída 25/12, que cai numa quinta", hoje)   # pt72 no reteste
    assert conferir_dias_da_semana("March 12, which is a Friday", hoje) is None
    assert "12/03/2027 é sexta" in conferir_dias_da_semana("March 12, which is a Saturday", hoje)
    assert conferir_dias_da_semana("el 5 de febrero es viernes", hoje) is None


def test_ferramenta_marca_a_temporada_de_cada_noite_e_as_bordas():
    """Reteste de 13/10: com a lista de noites na mão, o modelo cobrava tarifa normal em dezembro e março (alta)."""
    temporadas = bot_pousada.temporadas_do_faq(REFUGIO["faq"])
    assert [t["nome"] for t in temporadas] == ["alta temporada", "Réveillon"]
    r = noites_da_estadia({"entrada": "2026-12-12", "saida": "2026-12-18", "idioma": "pt"}, HOJE_QA.date(), temporadas)
    assert r["lista"][0] == "sábado 12/12/2026 · fora das temporadas (tarifa normal)"
    assert r["lista"][3] == "terça 15/12/2026 · borda de alta temporada: a pousada confirma a tarifa desta noite"
    assert r["lista"][4] == "quarta 16/12/2026 · alta temporada" and "borda" in r["aviso"]
    r = noites_da_estadia({"entrada": "2027-01-03", "saida": "2027-01-07", "idioma": "en"}, HOJE_QA.date(), temporadas)
    assert r["lista"][0] == "Sunday 03/01/2027 · alta temporada · Réveillon"
    assert r["lista"][2].startswith("Tuesday 05/01/2027 · alta temporada · edge of Réveillon")
    r = noites_da_estadia({"entrada": "2027-03-13", "saida": "2027-03-17", "idioma": "es"}, HOJE_QA.date(), temporadas)
    assert r["lista"] == ["sábado 13/03/2027 · alta temporada", "domingo 14/03/2027 · alta temporada",
                          "lunes 15/03/2027 · borde de alta temporada: la posada confirma la tarifa de esta noche",
                          "martes 16/03/2027 · fuera de temporada (tarifa normal)"]
    sem = noites_da_estadia({"entrada": "2027-03-13", "saida": "2027-03-15", "idioma": "pt"}, HOJE_QA.date(), [])
    assert sem["lista"] == ["sábado 13/03/2027", "domingo 14/03/2027"] and "aviso" not in sem
    assert bot_pousada.temporadas_do_faq([{"tipo": "temporadas", "periodos": [{"nome": "x", "de": "31/02", "ate": "1"}]}]) == []
    assert "temporadas" not in texto_faq(REFUGIO["faq"])          # não vai como texto para o prompt


def test_laco_passa_as_temporadas_do_faq_para_a_ferramenta():
    api = Api(_resp(("noites_da_estadia", {"entrada": "2026-12-16", "saida": "2026-12-18", "idioma": "pt"})),
              _resp(_resposta("2 noites na alta temporada (quarta 16/12 e quinta 17/12) a R$ 950, mais R$ 190.")))
    r = atender_hospede("Quanto fica de 16 a 18 de dezembro?", INFO_REFUGIO, cliente=api, agora=HOJE_QA)
    devolvido = json.loads(api.chamadas[1]["messages"][-1]["content"][0]["content"])
    assert devolvido["lista"] == ["quarta 16/12/2026 · alta temporada", "quinta 17/12/2026 · alta temporada"]
    assert r.passos == 2 and "950" in r.mensagem


@pytest.mark.parametrize("texto,errado", [
    ("De 20 a 23 de novembro são 3 noites: quinta (20), sexta (21) e sábado (22).", True),      # pt02 no reteste
    ("De 20 a 23 de novembro são 3 noites: sexta (20), sábado (21) e domingo (22).", False),
    ("March 13-17, 2027: Friday and Saturday (March 12–14) at R$ 750 per night.", True),  # nd11 (travessão)
    ("Em novembro: dia 22 (sábado).", True),
    ("Em novembro, dia 22 (domingo) é R$ 700.", False),
    ("Só me diz a quinta (2 noites?)", False),                       # sem mês: não confere
    ("A segunda opção é o Lótus, para 4 pessoas.", False),
])
def test_dia_da_semana_com_numero_solto_e_conferido_pelo_mes_da_frase(texto, errado):
    motivo = conferir_dias_da_semana(texto, HOJE_QA.date())
    assert bool(motivo) == errado, motivo
    if errado:
        assert "noites_da_estadia" in motivo


def test_modelo_usa_a_ferramenta_de_noites_e_a_trava_confere_pelas_datas_dela():
    noites = ("noites_da_estadia", {"entrada": "2026-11-20", "saida": "2026-11-23"})
    api = Api(_resp(noites),
              _resp(_resposta("São 3 noites: quinta (20), sexta (21) e sábado (22) a R$ 750.")),     # sem mês na frase
              _resp(_resposta("São 3 noites: sexta (20) e sábado (21) a R$ 750 e domingo (22) a R$ 700, mais R$ 190. "
                              "O Marcos confirma o valor final.")))
    r = atender_hospede("Quanto fica de 20 a 23 de novembro?", INFO_REFUGIO, cliente=api, agora=HOJE_QA)
    assert r.passos == 3 and r.mensagem.startswith("São 3 noites: sexta (20)")
    devolvido = json.loads(api.chamadas[1]["messages"][-1]["content"][0]["content"])
    assert devolvido["lista"][0].startswith("sexta 20/11/2026")
    assert "20/11/2026 é sexta, não quinta" in api.chamadas[2]["messages"][-1]["content"][0]["content"]
    assert "noites_da_estadia" in api.chamadas[0]["system"][0]["text"]


# --------------------------------------------------------------------- 2. pedido com nome placeholder
@pytest.mark.parametrize("nome", ["<UNKNOWN>", "unknown", "Desconhecido", "desconocido", "n/a", "N/A", "none", "null",
                                  "-", "?", "...", "hóspede", "Huésped", "guest", "Guest (unknown)", "sem nome",
                                  "sin nombre", "no name", "Nome", "", "   ", "!!!", "123"])
def test_nome_placeholder_nao_vale(nome):
    assert not nome_de_hospede_valido(nome)
    ok, erro = validar_pedido({"entrada": "2026-11-20", "saida": "2026-11-22", "adultos": 2, "nome": nome}, HOJE_QA.date())
    assert ok is None and "pergunte o nome" in erro and "<UNKNOWN>" in erro


@pytest.mark.parametrize("nome", ["Ana Souza", "Martín Gómez", "John Smith", "Lucía Fernández", "Zé", "Ana"])
def test_nome_de_verdade_vale(nome):
    assert nome_de_hospede_valido(nome)
    ok, erro = validar_pedido({"entrada": "2026-11-20", "saida": "2026-11-22", "adultos": 2, "nome": nome}, HOJE_QA.date())
    assert erro is None and ok["nome"] == nome


def test_formulario_do_site_sem_nome_faz_o_bot_perguntar_o_nome():
    """site01 do QA: o pedido ia para o dono como "🛎️ Pedido de reserva · <UNKNOWN>"."""
    registro = ("registrar_pedido_reserva", {"entrada": "2026-11-20", "saida": "2026-11-22", "adultos": 2,
                                             "criancas_idades": [4], "nome": "<UNKNOWN>", "contato": "",
                                             "acomodacao": "Espaço Lagoa", "observacoes": "Pet: cachorro pequeno"})
    api = Api(_resp(registro),
              _resp(_resposta("Recebi seu pedido para o Espaço Lagoa de 20 a 22/11, 2 adultos e 1 criança de 4 anos. "
                              "Só me diz seu nome para eu passar para o Marcos?")))
    formulario = ("Olá! Gostaria de consultar a disponibilidade no Refúgio do Exemplo 😊\nEspaço (Lagoa ou Bosque): "
                  "Lagoa\nEntrada: 20/11/2026\nSaída: 22/11/2026\nHóspedes (adultos e crianças, com a idade): 2 adultos "
                  "e 1 criança de 4 anos\nAnimal de estimação (sim/não): sim, cachorro pequeno\nOrigem: site-rodapé")
    r = atender_hospede(formulario, INFO_REFUGIO, cliente=api, agora=HOJE_QA)
    assert r.pedido is None and "seu nome" in r.mensagem and r.passos == 2
    erro = api.chamadas[1]["messages"][-1]["content"][0]
    assert erro["is_error"] and "placeholder" in erro["content"]
    assert "formulário do site" in api.chamadas[0]["system"][0]["text"]     # o prompt já avisa
    registrar = next(f for f in bot_pousada.FERRAMENTAS if f["name"] == "registrar_pedido_reserva")
    assert "<UNKNOWN>" in registrar["input_schema"]["properties"]["nome"]["description"]


# --------------------------------------------------------------------- 3. FAQ do Refúgio sem o número do bot
def test_faq_do_refugio_nao_tem_telefone_nem_dado_sensivel():
    texto = json.dumps(REFUGIO, ensure_ascii=False)
    assert REFUGIO["reserva_url"] == "" and REFUGIO["dono"] == "Marcos"
    assert "99965" not in texto and "+55" not in texto
    assert not re.search(r"\bwa\.me\b|senha:|\bical\b|\.ics|pix:|[\w.]+@\w+\.(com|br)", texto, re.I)
    ids = [i["id"] for i in REFUGIO["faq"] if i.get("tipo") not in ("guia", "temporadas")]
    assert len(ids) == len(set(ids))
    for esperado in ("reservar", "contato", "transfer", "bebe", "localizacao", "como-chegar", "acesso-senhas",
                     "checkin-antecipado", "site"):
        assert esperado in ids
    assert "reserva-direta" not in ids
    # o guia continua com os telefones dos restaurantes e da policlínica (são do guia, não do dono)
    assert "(48) 3000-0000" in texto_guia(REFUGIO["faq"])


@pytest.mark.parametrize("idioma", ["pt", "es", "en"])
def test_faq_do_refugio_passa_na_trava_nos_tres_idiomas(idioma):
    faq = REFUGIO["faq"]
    permitidos = "\n".join([texto_faq(faq), texto_guia(faq)])
    for item in faq:
        if item.get("tipo") in ("guia", "temporadas"):
            continue
        r = item[idioma]["r"]
        motivo = checar_mensagem(r, bot_pousada.descontos_do_faq(faq), idioma=idioma, permitidos=permitidos,
                                 hoje=HOJE_QA.date(), distancias_do_centro=bot_pousada.distancias_do_centro_do_faq(faq))
        assert motivo is None, f"{item['id']} ({idioma}): {motivo}"


def test_contato_e_transfer_mandam_passar_o_recado_em_vez_de_dar_o_numero():
    por_id = {i["id"]: i for i in REFUGIO["faq"] if i.get("tipo") not in ("guia", "temporadas")}
    for idioma in ("pt", "es", "en"):
        assert "Marcos" in por_id["contato"][idioma]["r"] and "@refugioexemplo" in por_id["contato"][idioma]["r"]
        assert "Marcos" in por_id["transfer"][idioma]["r"]
        assert "WhatsApp" not in por_id["bebe"][idioma]["r"]
    assert "refugioexemplo.com.br/reservar/" in por_id["reservar"]["pt"]["r"]
    assert "Airbnb" in por_id["reservar"]["pt"]["r"]


def test_telefone_do_bot_nao_sai_porque_nao_esta_mais_no_faq():
    permitidos = "\n".join([FAQ_REFUGIO, texto_guia(REFUGIO["faq"])])
    assert "telefone" in checar_mensagem("Fala com o Marcos pelo WhatsApp +55 51 90000-0023.", permitidos=permitidos)
    assert checar_mensagem("Tasca dos Açores: (48) 90000-0020.", permitidos=permitidos) is None


# --------------------------------------------------------------------- bônus: "vou passar para o dono" sem encaminhar
@pytest.mark.parametrize("texto", [
    "Vou passar seu interesse para o Marcos, que confirma tudo isso com você direto.",     # pt07
    "Ótima pergunta! Vou passar para o Marcos responder com os detalhes.",                # pt45
    "Se você já pagou, vou passar a mensagem para o Marcos agora mesmo!",                  # pt59
    "Para confirmar detalles como horno, te paso con Marcos.",                             # es08
    "Já passei para o Marcos.", "I've passed your question to Marcos.",
    "I'll let Marcos know about the early check-in.",
    "Não tenho essas informações aqui. Me deixa passar para o Marcos que ele te responde!",   # pt45 no reteste
    "Melhor eu passo para o Marcos te orientar direitinho.",                                    # pt61 no reteste
])
def test_promessa_de_encaminhar_e_reconhecida(texto):
    assert promete_encaminhar(texto)


@pytest.mark.parametrize("texto", [
    "Me conta as datas e quantas pessoas que eu passo o pedido para o Marcos.",     # FAQ valores
    "Pasame las fechas y cuántas personas y le paso el pedido a Marcos.",
    "Tell me your dates and number of guests and I will pass the request to Marcos.",
    "Quer que eu passe para o Marcos?",
    "O Marcos confirma a disponibilidade e envia os dados do sinal.",
    "É só me dizer o assunto que eu passo o recado para ele.",                 # FAQ contato
])
def test_pedido_de_dado_ou_pergunta_nao_e_promessa(texto):
    assert not promete_encaminhar(texto)


def test_responder_prometendo_passar_ao_dono_volta_para_o_modelo():
    api = Api(_resp(_resposta("Não tenho essa informação. Vou passar para o Marcos responder.")),
              _resp(_resposta("Não tenho essa informação. Passei para o Marcos; a resposta chega em breve.",
                              acao="encaminhar", resumo="Pergunta se a banheira é aquecida.")))
    r = atender_hospede("A banheira é aquecida?", INFO_REFUGIO, cliente=api, agora=HOJE_QA)
    assert r.acao == "encaminhar" and r.passos == 2 and r.resumo_para_dono == "Pergunta se a banheira é aquecida."
    erro = api.chamadas[1]["messages"][-1]["content"][0]["content"]
    assert "acao='encaminhar'" in erro and "Marcos" in erro


def test_pedido_registrado_pode_dizer_que_passou_com_acao_responder():
    registro = ("registrar_pedido_reserva", {"entrada": "2026-11-20", "saida": "2026-11-22", "adultos": 2,
                                             "criancas_idades": [], "nome": "Ana Souza", "contato": "",
                                             "acomodacao": "Espaço Lagoa", "observacoes": ""})
    api = Api(_resp(registro, _resposta("Perfeito, Ana! Passei seu pedido para o Marcos, que confirma a "
                                        "disponibilidade e envia os dados do Pix.")))
    r = atender_hospede("Quero reservar o Lagoa de 20 a 22/11, 2 adultos, Ana Souza", INFO_REFUGIO, cliente=api,
                        agora=HOJE_QA)
    assert r.pedido and r.pedido["nome"] == "Ana Souza" and r.acao == "responder" and r.passos == 1


def test_pedido_registrado_e_6_recusas_nao_viram_nao_consegui_responder():
    """pt58 no reteste de 09/10: pedido registrado, mas 6 respostas com total somado recusadas -> o hóspede recebia
    "o atendimento virtual não conseguiu responder" em 3 idiomas, mesmo com o dono já avisado pelo 🛎️."""
    registro = ("registrar_pedido_reserva", {"entrada": "2026-11-20", "saida": "2026-11-22", "adultos": 2,
                                             "criancas_idades": [], "nome": "Ana Souza", "contato": "",
                                             "acomodacao": "Espaço Lagoa", "observacoes": ""})
    com_total = _resposta("Pronto, Ana! 2 noites a R$ 750 = R$ 1.500 + R$ 190.")
    api = Api(_resp(registro, com_total), *[_resp(com_total) for _ in range(6)])
    r = atender_hospede("Quero o Lagoa de 20 a 22/11, 2 adultos, Ana Souza", INFO_REFUGIO, cliente=api, agora=HOJE_QA)
    assert r.pedido and r.fallback and r.acao == "responder" and r.passos == 6
    assert r.mensagem == "Pronto! Seu pedido de reserva foi passado para Marcos, que confirma a disponibilidade e responde em breve."
    assert "não conseguiu" not in r.mensagem and r.resumo_para_dono == ""


def test_fallback_sem_pedido_sai_no_idioma_das_respostas_recusadas():
    api = Api(*[_resp(_resposta("Total: R$ 1.500.", idioma="en")) for _ in range(6)])
    r = atender_hospede("How much for 2 nights?", INFO_REFUGIO, cliente=api, agora=HOJE_QA)
    assert r.fallback and r.acao == "encaminhar" and r.idioma == "pt"
    assert r.mensagem.startswith("Thanks for your message!") and "Obrigado" not in r.mensagem


def test_encaminhamento_automatico_continua_valendo_com_o_faq_de_exemplo():
    """Garantia de que as travas novas não mexem no fluxo normal de outra pousada (exemplo-rosa)."""
    api = Api(_resp(_resposta("Aceitamos pets de até 10 kg nos chalés, com taxa de R$ 50 por diária.")))
    r = atender_hospede("Aceitam cachorro?", INFO, cliente=api, agora=AGORA)
    assert r.acao == "responder" and r.passos == 1
