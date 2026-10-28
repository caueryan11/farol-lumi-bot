"""Modo pousada (NEGOCIO=pousada): bot com IA simulada e webhook com banco simulado. Sem internet."""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import anthropic
import pytest
from fastapi.testclient import TestClient

from bot import pousada as bot_pousada
from bot.pousada import PousadaInfo, ResultadoPousada, atender_hospede, checar_mensagem, texto_faq, validar_pedido
from whatsapp.app import Config
from whatsapp.pousada_app import CONFIRMACAO_SAIDA, criar_app_pousada
from whatsapp.pousada_repo import Hospede, Pousada

FAQ_ARQUIVO = Path(__file__).resolve().parents[1] / "pousadas" / "exemplo-rosa.json"
DADOS = json.loads(FAQ_ARQUIVO.read_text(encoding="utf-8"))
INFO = PousadaInfo(DADOS["nome"], "Carla", DADOS["cidade"], DADOS["faq"])
AGORA = datetime(2026, 10, 2, 21, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))


# ---------------------------------------------------------------------
# FAQ de demonstração
# ---------------------------------------------------------------------
def test_faq_ficticio_tem_25_perguntas_em_pt_e_es():
    assert DADOS["ficticio"] is True and "FICTÍCIOS" in DADOS["aviso"]
    assert len(DADOS["faq"]) >= 25
    assert all(f["pt"]["p"] and f["pt"]["r"] and f["es"]["p"] and f["es"]["r"] for f in DADOS["faq"])
    ids = {f["id"] for f in DADOS["faq"]}
    for tema in ("reveillon", "cafe", "pet", "estacionamento", "checkin", "como-chegar-floripa", "como-chegar-poa",
                 "praia", "cancelamento", "sinal-pix"):
        assert tema in ids


def test_faq_vai_para_o_prompt_nos_dois_idiomas():
    t = texto_faq(DADOS["faq"])
    assert "[pt] P: Aceitam pet?" in t and "[es] P: ¿Aceptan mascotas?" in t


def test_prompt_pede_ressalva_de_valores_nos_tres_idiomas():
    for frase in ("Valores confirmados pela pousada na reserva.", "Valores confirmados por la posada al reservar.",
                  "Rates are confirmed by the property when booking."):
        assert frase in bot_pousada.INSTRUCOES


# ---------------------------------------------------------------------
# Travas em código
# ---------------------------------------------------------------------
@pytest.mark.parametrize("texto", [
    "Pronto, sua reserva está confirmada!", "¡Tu reserva quedó confirmada!", "Your booking is confirmed.",
    "Sim, temos vaga nessas datas!", "Sí, tenemos lugar para 2.", "Chave Pix: carla@pousada.com",
    "Te dou 10% de desconto", "Te hago un descuento de R$ 100", "Pague aqui: https://pag.ae/abc",
])
def test_trava_barra_confirmacao_cobranca_e_desconto(texto):
    assert checar_mensagem(texto) is not None


@pytest.mark.parametrize("texto", [
    "A Carla confirma a disponibilidade e envia os dados do sinal via Pix.",
    "Não trabalhamos com desconto, mas passei seu pedido para a Carla.",
    "Pasé tu pedido a Carla, que confirma la disponibilidad y te envía los datos de la seña.",
    "O sinal de 30% é pago via Pix depois que a Carla confirma.",
])
def test_trava_nao_barra_respostas_normais(texto):
    assert checar_mensagem(texto) is None


def test_desconto_que_o_faq_anuncia_pode_ser_repetido_e_outro_nao():
    """Exemplo do Rosa: "Pix com 5% de desconto" está no FAQ; antes a trava barrava e o bot escondia a informação."""
    faq = [{"id": "pagamento", "pt": {"p": "Formas de pagamento?", "r": "Cartão em 6x ou Pix com 5% de desconto."}}]
    permitidos = bot_pousada.descontos_do_faq(faq)
    assert permitidos == {"5"}
    for ok in ("No Pix você tem 5% de desconto.", "Pix con 5% de descuento.", "Pix: 5% off.", "Pix com desconto de 5%."):
        assert checar_mensagem(ok, permitidos) is None, ok
    for proibido in ("Te dou 10% de desconto", "desconto de R$ 5", "Pix com 5% de desconto e mais 10% de desconto"):
        assert checar_mensagem(proibido, permitidos) is not None, proibido
    assert checar_mensagem("No Pix você tem 5% de desconto.") is not None    # sem FAQ, continua barrado


def test_trava_barra_portugues_em_resposta_em_espanhol_ou_ingles():
    """Avaliação de 04/10: "Lá el sitio tiene la mejor tarifa" numa resposta em espanhol."""
    assert checar_mensagem("Lá el sitio tiene la mejor tarifa garantizada.", idioma="es") is not None
    assert checar_mensagem("You can book there, não?", idioma="en") is not None
    assert checar_mensagem("Allá el sitio tiene la mejor tarifa. La Pousada Exemplo do Rosa está a 300 m.", idioma="es") is None
    assert checar_mensagem("Farmácia Nossa Senhora dos Navegantes, en Ibiraquera.", idioma="es") is None
    assert checar_mensagem("Lá você vê a disponibilidade.", idioma="pt") is None
    assert checar_mensagem("R$ 991 por noche, con mínimo de 3 noites.", idioma="es") is not None
    assert checar_mensagem("El sitio tiene la mejor tarifa garantida.", idioma="es") is not None
    # ensaio com a demo Natribu's (dono = "a equipe da pousada"): "la équipe de la posada te envía los datos"
    assert checar_mensagem("La équipe de la posada te envía los datos.", idioma="es") is not None
    assert checar_mensagem("El equipo de la posada te envía los datos.", idioma="es") is None


def test_apresentacao_em_ingles_sem_possessivo_no_nome():
    """Ensaio de 04/10: "This is Pousada Natribu's's virtual assistant"."""
    assert "{pousada}'s" not in bot_pousada.INSTRUCOES and "virtual assistant of {pousada}" in bot_pousada.INSTRUCOES


def test_trava_barra_perto_sem_distancia():
    """Avaliação de 04/10: "Farmácia Nossa Senhora dos Navegantes em Ibiraquera, bem pertinho" (o guia não tem a distância)."""
    assert checar_mensagem("Tem a Farmácia Navegantes em Ibiraquera, bem pertinho.") is not None
    assert checar_mensagem("La farmacia está muy cerca de la posada.", idioma="es") is not None
    assert checar_mensagem("O centrinho fica bem pertinho, a 300 metros.") is None
    assert checar_mensagem("O Mercado Meia Praia fica a 630 m, bem perto (8 min a pé).") is None
    assert checar_mensagem("A Farmácia Navegantes fica em Ibiraquera.") is None


def test_pousada_so_pelo_site_ninguem_confirma_reserva_pelo_whatsapp():
    """Avaliação de 04/10: "Pasé tu solicitud a Carla para que confirmen tu reserva" (reserva só pelo site)."""
    for texto in ("Pasé tu solicitud a Carla para que confirmen tu reserva.", "A Carla vai confirmar sua reserva.",
                  "Carla will confirm your booking."):
        assert checar_mensagem(texto, so_site=True) is not None, texto
        assert checar_mensagem(texto) is None, texto          # com pedido de reserva, o dono confirma mesmo
    assert checar_mensagem("A reserva é feita e confirmada pelo site: https://www.exemplo.com.br", so_site=True) is None
    # quem confirma é o site: resposta certa, não pode voltar para reescrever
    assert checar_mensagem("Book at https://www.exemplo.com.br; the website will confirm it by email.", so_site=True) is None
    assert checar_mensagem("Reservá en https://www.exemplo.com.br, el sitio va a confirmar por e-mail.", so_site=True) is None
    assert checar_mensagem("Carla vão confirmar sua reserva.", so_site=True) is not None


def test_nome_do_dono_sem_rotulo_entre_parenteses():
    assert bot_pousada.nome_do_dono("Carla (donos)") == "Carla"
    assert bot_pousada.nome_do_dono("Carla") == "Carla"
    assert checar_mensagem("Pasé tu mensaje a Carla (dueños de la pousada).") is not None
    assert checar_mensagem("Passei para Carla (os donos).") is not None
    assert checar_mensagem("O check-in (a partir das 14h) é no quiosque.") is None
    api = Api(_resp(_resposta("oi")))
    atender_hospede("oi", PousadaInfo(INFO.nome, "Carla (donos)", INFO.cidade, INFO.faq), cliente=api, agora=AGORA)
    prompt = api.chamadas[0]["system"][0]["text"]
    assert "Carla (donos)" not in prompt and "Encaminhe para Carla" in prompt


def test_encaminhamento_automatico_nao_poe_portugues_nas_linhas_es_en():
    """As demos de 04/10 têm dono = "a equipe da pousada": "Ya se lo pasé a a equipe da pousada" ficava em português."""
    msg = bot_pousada.mensagem_encaminhamento("a equipe da pousada")
    pt, es, en = msg.splitlines()
    assert "Já passei para a equipe da pousada" in pt
    assert "Ya se lo pasé al equipo de la posada" in es and "the property's team" in en
    assert "Ya se lo pasé a Carla" in bot_pousada.mensagem_encaminhamento("Carla")


def test_resposta_em_espanhol_com_portugues_volta_para_o_modelo():
    api = Api(_resp(_resposta("Lá el sitio tiene la mejor tarifa.", idioma="es")),
              _resp(_resposta("Allá el sitio tiene la mejor tarifa.", idioma="es")))
    r = atender_hospede("¿Dónde reservo?", INFO, cliente=api, agora=AGORA)
    assert r.mensagem == "Allá el sitio tiene la mejor tarifa." and not r.fallback
    assert "português" in api.chamadas[1]["messages"][-1]["content"][0]["content"]


def test_distancia_e_da_pousada_nao_do_centrinho():
    """Fase 3 (04/10): "supermercados a 630 m y 1,4 km del centrinho" (as distâncias do guia são da pousada)."""
    assert checar_mensagem("Hay supermercados a 630 m y 1,4 km del centrinho.", idioma="es") is not None
    assert checar_mensagem("O Mercado Meia Praia fica a 630 m do centro.") is not None
    assert checar_mensagem("Hay supermercados a 630 m y 1,4 km de la posada.", idioma="es") is None
    assert checar_mensagem("O Mercado Meia Praia fica a 630 m da pousada (8 min a pé).") is None
    # a distância que o próprio FAQ escreve passa (Watu Kererê: "a 600 m do centrinho")
    faq = [{"id": "local", "pt": {"p": "Onde fica?", "r": "A pousada fica a 600 m do centrinho da Praia do Rosa."}}]
    do_centro = bot_pousada.distancias_do_centro_do_faq(faq)
    assert checar_mensagem("Ficamos a 600 m do centrinho.", distancias_do_centro=do_centro) is None
    assert checar_mensagem("O mercado fica a 900 m do centrinho.", distancias_do_centro=do_centro) is not None


@pytest.mark.parametrize("texto", ["Passei para a Carla, que entra em contato em poucos minutos.",
                                   "Ya se lo pasé a Carla; te responde en unos minutos.",
                                   "I've passed it to Carla; she'll get back to you in a few hours.",
                                   "Passei para a Carla, ela te responde hoje mesmo.",
                                   # fase 3, rodada 1 (es-queja)
                                   "Pasé tu mensaje a Carla para que puedan atenderte de inmediato.",
                                   "Ellos van a ver y responder enseguida."])
def test_trava_barra_prazo_de_resposta_do_dono(texto):
    assert "prazo" in checar_mensagem(texto, idioma="pt")


def test_prazo_nao_barra_distancia_nem_em_breve():
    assert checar_mensagem("Passei para a Carla; a resposta chega em breve.") is None
    assert checar_mensagem("A praia fica a 400 m (5 min a pé).") is None


def test_trava_barra_vaga_afirmada_com_disponivel():
    """es-reveillon (04/10): "tenemos bungalós disponibles con mínimo de 4 noches"."""
    assert checar_mensagem("Para Año Nuevo tenemos bungalós disponibles.", idioma="es") is not None
    assert checar_mensagem("We have 2 rooms available that weekend.", idioma="en") is not None
    assert checar_mensagem("Temos estacionamento disponível para hóspedes.") is None
    assert checar_mensagem("No site você vê quais bangalôs estão disponíveis.") is None


def test_dia_da_semana_errado_junto_da_data_e_barrado():
    hoje = AGORA.date()   # sexta, 02/10/2026
    assert checar_mensagem("Entrada sábado 03/10 e saída segunda 05/10.", hoje=hoje) is None
    assert checar_mensagem("This weekend: Friday, October 9 to Sunday, October 11.", idioma="en", hoje=hoje) is None
    assert checar_mensagem("Del viernes 9 de octubre al domingo 11 de octubre.", idioma="es", hoje=hoje) is None
    motivo = checar_mensagem("Seria de sábado 04/10 a segunda 06/10?", hoje=hoje)
    assert "04/10/2026 é domingo" in motivo
    assert "domingo" in checar_mensagem("This weekend (Saturday, October 4)", idioma="en", hoje=hoje)
    assert checar_mensagem("11 de outubro (sábado)", hoje=hoje) is not None
    assert checar_mensagem("Sábado 04/10", hoje=None) is None          # sem data de hoje, não confere
    assert checar_mensagem("A segunda opção é o Lótus, para 4 pessoas.", hoje=hoje) is None


def test_calendario_vai_para_o_prompt_com_o_fim_de_semana():
    api = Api(_resp(_resposta("Oi!")))
    atender_hospede("oi", INFO, cliente=api, agora=AGORA)      # sexta, 02/10/2026
    dinamico = api.chamadas[0]["system"][1]["text"]
    assert "sex 02/10; sáb 03/10; dom 04/10" in dinamico
    assert "sexta 02/10 a domingo 04/10" in dinamico
    assert "sexta 09/10 a domingo 11/10" in bot_pousada.calendario(datetime(2026, 10, 4).date())   # domingo
    assert "pergunte as datas" in bot_pousada.calendario(datetime(2026, 10, 4).date())
    assert "sexta 09/10 a domingo 11/10" in bot_pousada.calendario(datetime(2026, 10, 10).date())  # sábado


def test_dia_da_semana_errado_volta_para_o_modelo():
    api = Api(_resp(_resposta("This weekend (Saturday, October 4) works?", idioma="en")),
              _resp(_resposta("Do you mean Friday, October 9 to Sunday, October 11?", idioma="en")))
    r = atender_hospede("2 nights this weekend?", INFO, cliente=api, agora=AGORA)
    assert r.mensagem.startswith("Do you mean") and r.passos == 2
    assert "dia da semana errado" in api.chamadas[1]["messages"][-1]["content"][0]["content"]


def test_validar_pedido():
    hoje = AGORA.date()
    ok, erro = validar_pedido({"entrada": "2027-01-10", "saida": "2027-01-15", "adultos": 2, "criancas_idades": [4],
                               "nombre": "Martín Gómez"}, hoje)
    assert erro is None and ok["noites"] == 5 and ok["nome"] == "Martín Gómez" and ok["criancas_idades"] == [4]
    assert validar_pedido({"entrada": "2027-01-15", "saida": "2027-01-10", "adultos": 2, "nome": "Ana"}, hoje)[1]
    assert "passou" in validar_pedido({"entrada": "2026-09-01", "saida": "2026-09-03", "adultos": 2, "nome": "Ana"}, hoje)[1]
    assert "nome" in validar_pedido({"entrada": "2027-01-10", "saida": "2027-01-12", "adultos": 2, "nome": ""}, hoje)[1]
    assert "AAAA-MM-DD" in validar_pedido({"entrada": "10/01", "saida": "15/01", "adultos": 2, "nome": "Ana"}, hoje)[1]


# ---------------------------------------------------------------------
# Laço do bot com a API simulada
# ---------------------------------------------------------------------
def _resp(*chamadas):
    return NS(content=[NS(type="tool_use", id=f"t{i}", name=n, input=e) for i, (n, e) in enumerate(chamadas)],
              stop_reason="tool_use", usage=NS(input_tokens=10, output_tokens=5))


class Api:
    def __init__(self, *respostas):
        self.respostas, self.chamadas = list(respostas), []
        self.messages = self

    def create(self, **kw):
        self.chamadas.append({**kw, "messages": list(kw["messages"])})  # cópia: o laço segue acrescentando
        return self.respostas.pop(0)


def _resposta(mensagem, acao="responder", idioma="pt", resumo=""):
    return ("enviar_resposta", {"acao": acao, "mensagem": mensagem, "idioma": idioma, "motivo": "",
                                "resumo_para_dono": resumo})


def test_pergunta_em_pt_responde_pelo_faq():
    api = Api(_resp(_resposta("Aceitamos pets de até 10 kg nos chalés, com taxa de R$ 50 por diária.")))
    r = atender_hospede("Aceitam cachorro?", INFO, cliente=api, agora=AGORA)
    assert r.acao == "responder" and r.idioma == "pt" and "10 kg" in r.mensagem and r.pedido is None
    system = api.chamadas[0]["system"]
    assert "Não se apresente" in system[0]["text"] and "Aceitam pet?" in system[0]["text"]
    assert "Nunca diga que é uma pessoa" in system[0]["text"]   # sem apresentação, mas sem fingir ser gente
    assert "02/10/2026" in system[1]["text"]               # data de hoje para resolver "janeiro" sem ano


def test_pedido_em_espanhol_registra_e_responde():
    api = Api(_resp(("registrar_pedido_reserva", {"entrada": "2027-01-10", "saida": "2027-01-15", "adultos": 2,
                                                  "criancas_idades": [], "nombre": "Martín Gómez", "contato": "",
                                                  "acomodacao": "", "observacoes": ""})),
              _resp(_resposta("¡Listo, Martín! Pasé tu pedido a Carla, que confirma la disponibilidad.", idioma="es")))
    r = atender_hospede("Hola, ¿tienen lugar para 2 del 10 al 15 de enero? Soy Martín Gómez", INFO, cliente=api, agora=AGORA)
    assert r.idioma == "es" and r.pedido["entrada"] == "2027-01-10" and r.pedido["noites"] == 5
    resultado_ferramenta = api.chamadas[1]["messages"][-1]["content"][0]
    assert '"registrado": true' in resultado_ferramenta["content"] and not resultado_ferramenta["is_error"]


def test_pedido_invalido_volta_para_o_modelo_perguntar():
    api = Api(_resp(("registrar_pedido_reserva", {"entrada": "2027-01-10", "saida": "2027-01-15", "adultos": 2,
                                                  "criancas_idades": [], "nome": "", "contato": "", "acomodacao": "",
                                                  "observacoes": ""})),
              _resp(_resposta("Perfeito! Qual é o seu nome?")))
    r = atender_hospede("quero 10 a 15 de janeiro, 2 adultos", INFO, cliente=api, agora=AGORA)
    assert r.pedido is None and r.mensagem == "Perfeito! Qual é o seu nome?"
    assert api.chamadas[1]["messages"][-1]["content"][0]["is_error"] is True


def test_trava_faz_o_modelo_reescrever_confirmacao():
    api = Api(_resp(_resposta("Sua reserva está confirmada!")),
              _resp(_resposta("Passei seu pedido para a Carla, que confirma a disponibilidade em breve.",
                              acao="encaminhar", resumo="Quer reservar.")))   # "passei" sem pedido: só encaminhando
    r = atender_hospede("pode reservar?", INFO, cliente=api, agora=AGORA)
    assert "confirmada" not in r.mensagem and r.passos == 2
    assert "não diga que a reserva está confirmada" in api.chamadas[1]["messages"][-1]["content"][0]["content"]


def test_desconto_e_encaminhado_ao_dono():
    api = Api(_resp(_resposta("Passei seu pedido para a Carla, que responde em breve.", acao="encaminhar", idioma="en",
                              resumo="Pede desconto para 7 noites.")))
    r = atender_hospede("Can I get a discount for 7 nights?", INFO, cliente=api, agora=AGORA)
    assert r.acao == "encaminhar" and r.idioma == "en" and r.resumo_para_dono == "Pede desconto para 7 noites."


def test_encaminhar_sem_resumo_e_recusado():
    api = Api(_resp(_resposta("Passei para a Carla.", acao="encaminhar")),
              _resp(_resposta("Passei para a Carla.", acao="encaminhar", resumo="Quer mudar a data da reserva.")))
    r = atender_hospede("preciso mudar minha reserva", INFO, cliente=api, agora=AGORA)
    assert r.resumo_para_dono == "Quer mudar a data da reserva." and r.passos == 2


def test_api_fora_encaminha_em_pt_e_es():
    class Quebrada:
        class messages:
            @staticmethod
            def create(**kw):
                raise anthropic.APIConnectionError(request=NS(method="POST", url="x", headers={}))
    r = atender_hospede("hola", INFO, cliente=Quebrada(), agora=AGORA)
    assert r.fallback and r.acao == "encaminhar" and "Carla" in r.mensagem and "¡Gracias" in r.mensagem
    assert "Thanks" in r.mensagem and "que te responde" not in r.mensagem   # dono pode ser "Carla"


@pytest.mark.parametrize("chave", ["mensaje", "message"])
def test_chave_mensagem_traduzida_e_aceita(chave):
    """Avaliação de 04/10: em espanhol o Haiku às vezes manda `mensaje`; antes virava 6 recusas e encaminhamento."""
    api = Api(_resp(("enviar_resposta", {"acao": "responder", chave: "En Carnaval, R$ 790 por noche.", "idioma": "es",
                                         "motivo": "", "resumo_para_dono": ""})))
    r = atender_hospede("¿Cuánto en Carnaval?", INFO, cliente=api, agora=AGORA)
    assert not r.fallback and r.passos == 1   # com valor em R$: a ressalva entra em código, no idioma
    assert r.mensagem == "En Carnaval, R$ 790 por noche.\nValores confirmados por la posada al reservar."


def test_mensagem_vazia_diz_o_nome_da_chave():
    api = Api(_resp(("enviar_resposta", {"acao": "responder", "texto": "oi", "idioma": "pt", "motivo": "",
                                         "resumo_para_dono": ""})), _resp(_resposta("Oi!")))
    atender_hospede("oi", INFO, cliente=api, agora=AGORA)
    assert "`mensagem`" in api.chamadas[1]["messages"][-1]["content"][0]["content"]


# ---------------------------------------------------------------------
# Webhook com banco e WhatsApp simulados
# ---------------------------------------------------------------------
TOKEN, SEGREDO = "token-pousada", "segredo-pousada-123456"
URL = f"/webhook/uazapi/{SEGREDO}"
DONO, HOSPEDE = "5548999990000", "5491155550000"


class Cliente:
    def __init__(self, transcricao=""):
        self.enviadas, self.transcricao, self.transcritos = [], transcricao, []

    def transcrever_audio(self, message_id, chave):
        self.transcritos.append((message_id, chave))
        if isinstance(self.transcricao, Exception):
            raise self.transcricao
        return self.transcricao

    def enviar_texto(self, numero, texto, digitando_ms=1200):
        self.enviadas.append((numero, texto))
        self.digitando = getattr(self, "digitando", []) + [digitando_ms]
        return f"wa-{len(self.enviadas)}"


class Repo:
    def __init__(self):
        self.pousada = Pousada(id="po1", nome=INFO.nome, dono="Carla", telefone_aviso=DONO, cidade=INFO.cidade,
                               faq=INFO.faq)
        self.h = Hospede(id="h1", telefone=HOSPEDE)
        self.msgs, self.pedidos, self.ids = [], [], set()

    def pousada_por_instancia(self, instancia):
        return self.pousada if instancia == "pousada-rosa" else None

    def hospede(self, pousada_id, telefone):
        return self.h

    def atualizar_hospede(self, hospede_id, campos):
        for k, v in campos.items():
            setattr(self.h, k, v)

    def registrar_mensagem(self, pousada_id, hospede_id, direcao, wa_id, conteudo, acao=None, motivo=None):
        if direcao == "entrada" and wa_id in self.ids:
            return False
        self.ids.add(wa_id)
        self.msgs.append((direcao, conteudo, acao))
        return True

    def historico(self, hospede_id, antes_de_wa_id, limite=10):
        return [(d, c) for d, c, _ in self.msgs[:-1]]

    def registrar_pedido(self, pousada_id, hospede_id, pedido, idioma):
        self.pedidos.append((pedido, idioma))
        return "pe1"


def evento(texto, de=HOSPEDE, msg_id="M1", tipo="Conversation"):
    return {"EventType": "messages", "token": TOKEN, "instanceName": "pousada-rosa",
            "message": {"messageid": msg_id, "chatid": f"{de}@s.whatsapp.net", "sender_pn": f"{de}@s.whatsapp.net",
                        "fromMe": False, "isGroup": False, "messageType": tipo, "text": texto, "wasSentByApi": False},
            "chat": {"phone": de}}


def app(resultado=None, chave_openai="", transcricao=""):
    repo, cliente, chamadas = Repo(), Cliente(transcricao), []

    def atender(texto, info, **kw):
        chamadas.append((texto, info.nome, kw.get("historico")))
        return resultado or ResultadoPousada(acao="responder", mensagem="Hola! Te escribe la atención virtual.", idioma="es")

    a = criar_app_pousada(Config("https://x.uazapi.com", TOKEN, SEGREDO, rotinas=False, negocio="pousada",
                                 openai_api_key=chave_openai),
                          cliente=cliente, repo=repo, atender_fn=atender)
    return TestClient(a), cliente, repo, chamadas


def test_pergunta_e_respondida_e_registrada_sem_aviso_ao_dono():
    c, cliente, repo, chamadas = app()
    assert c.post(URL, json=evento("Hola, ¿tienen lugar para 2 en enero?")).status_code == 200
    assert cliente.enviadas == [(HOSPEDE, "Hola! Te escribe la atención virtual.")]
    assert chamadas[0][:2] == ("Hola, ¿tienen lugar para 2 en enero?", INFO.nome)
    assert [d for d, _, _ in repo.msgs] == ["entrada", "saida"] and repo.h.idioma == "es"


def test_pedido_de_reserva_chega_pronto_para_o_dono():
    pedido = {"entrada": "2027-01-10", "saida": "2027-01-15", "noites": 5, "adultos": 2, "criancas_idades": [4],
              "nome": "Martín Gómez", "contato": "", "acomodacao": "chalé", "observacoes": "viajam com um pet pequeno"}
    c, cliente, repo, _ = app(ResultadoPousada(acao="responder", mensagem="¡Listo, Martín!", idioma="es", pedido=pedido))
    c.post(URL, json=evento("del 10 al 15, Martín Gómez"))
    assert cliente.enviadas[0] == (HOSPEDE, "¡Listo, Martín!")
    numero, aviso = cliente.enviadas[1]
    assert numero == DONO and aviso.split("\n") == [
        "🛎️ *Pedido de reserva · Martín Gómez*",
        "📅 10/01 a 15/01/2027 · 5 noite(s)",
        "👥 2 adulto(s) + 1 criança(s) (4 anos)",
        "🌐 escreve em espanhol · prefere chalé",
        "📝 Viajam com um pet pequeno.",
        f"👉 Confirme a disponibilidade e mande o sinal: wa.me/{HOSPEDE}"]
    assert repo.pedidos == [(pedido, "es")] and repo.h.nome == "Martín Gómez"


def test_encaminhamento_avisa_o_dono():
    c, cliente, _, _ = app(ResultadoPousada(acao="encaminhar", mensagem="Ya se lo pasé a Carla.", idioma="es",
                                            motivo="desconto", resumo_para_dono="Pede desconto para 7 noites."))
    c.post(URL, json=evento("¿me hacés un descuento?"))
    assert cliente.enviadas[1] == (DONO, "🟡 *Responder · hóspede*\nPede desconto para 7 noites.\n"
                                         f"🌐 Escreve em espanhol\n👉 Responda: wa.me/{HOSPEDE}")


def test_mensagem_do_dono_e_ignorada():
    c, cliente, repo, chamadas = app()
    c.post(URL, json=evento("teste", de=DONO))
    assert cliente.enviadas == [] and chamadas == [] and repo.msgs == []


def test_parar_confirma_uma_vez_e_voltar_a_escrever_reativa():
    c, cliente, repo, chamadas = app()
    c.post(URL, json=evento("PARAR", msg_id="P1"))
    c.post(URL, json=evento("parar", msg_id="P2"))
    assert cliente.enviadas == [(HOSPEDE, CONFIRMACAO_SAIDA)] and repo.h.saiu is True and chamadas == []
    c.post(URL, json=evento("hola de nuevo", msg_id="P3"))
    assert repo.h.saiu is False and len(chamadas) == 1


def test_audio_pede_texto_sem_chamar_a_ia():
    c, cliente, _, chamadas = app()
    c.post(URL, json=evento("", tipo="AudioMessage"))
    assert chamadas == [] and cliente.enviadas == [(HOSPEDE, bot_pousada.MENSAGEM_NAO_TEXTO)]


def test_audio_transcrito_vira_pergunta_para_a_ia():
    c, cliente, repo, chamadas = app(chave_openai="sk-teste", transcricao="Hola, ¿aceptan mascotas?")
    c.post(URL, json=evento("", msg_id="A1", tipo="AudioMessage"))
    assert cliente.transcritos == [("A1", "sk-teste")]
    assert chamadas[0][0] == "(mensagem de áudio, transcrita automaticamente; pode ter erros) Hola, ¿aceptan mascotas?"
    assert repo.msgs[0] == ("entrada", "[áudio] Hola, ¿aceptan mascotas?", None)
    assert cliente.enviadas == [(HOSPEDE, "Hola! Te escribe la atención virtual.")]


@pytest.mark.parametrize("transcricao", ["", RuntimeError("uazapi fora")])
def test_audio_sem_fala_ou_com_falha_pede_texto(transcricao):
    c, cliente, _, chamadas = app(chave_openai="sk-teste", transcricao=transcricao)
    c.post(URL, json=evento("", tipo="AudioMessage"))
    assert chamadas == [] and cliente.enviadas == [(HOSPEDE, bot_pousada.MENSAGEM_NAO_TEXTO)]


def test_sem_chave_nao_tenta_transcrever():
    c, cliente, _, _ = app(transcricao="oi")
    c.post(URL, json=evento("", tipo="AudioMessage"))
    assert cliente.transcritos == []


def test_evento_repetido_responde_uma_vez():
    c, cliente, _, _ = app()
    c.post(URL, json=evento("oi"))
    assert c.post(URL, json=evento("oi")).json()["repetido"] is True
    assert len(cliente.enviadas) == 1


def test_instancia_desconhecida_e_token_errado():
    c, cliente, _, _ = app()
    assert c.post(URL, json={**evento("oi"), "token": "outro"}).status_code == 401
    c.post(URL, json={**evento("oi", msg_id="X9"), "instanceName": "outra"})
    assert cliente.enviadas == []


# --- guia local ------------------------------------------------------------------------------------
def test_guia_fica_fora_do_faq_e_vira_secao_propria():
    faq = INFO.faq + [
        {"tipo": "guia", "categoria": "Mercados", "nome": "Mercado Longe", "km": 3.2, "descricao": "supermercado"},
        {"tipo": "guia", "categoria": "Mercados", "nome": "Mercadinho Perto", "km": 0.35,
         "descricao": "conveniência", "temporada": True, "opiniao_dono": "caro, só para emergência"},
        {"tipo": "guia", "categoria": "Farmácias", "nome": "Farmácia Sem Coordenada", "km": None},
    ]
    assert "Mercadinho" not in bot_pousada.texto_faq(faq)
    guia = bot_pousada.texto_guia(faq)
    assert guia.index("Mercadinho Perto") < guia.index("Mercado Longe")
    assert "cerca de 350 m (4 min a pé)" in guia and "cerca de 3,2 km (~6 min de carro)" in guia
    assert "só na temporada" in guia and "opinião de quem é da pousada: caro, só para emergência" in guia
    assert "Farmácia Sem Coordenada · distância não informada" in guia


def test_sem_guia_manda_encaminhar():
    assert "sem guia local" in bot_pousada.texto_guia(INFO.faq)


def test_montar_guia_calcula_distancia_e_descarta_nao_confirmado():
    import importlib.util, pathlib

    spec = importlib.util.spec_from_file_location("montar_guia", pathlib.Path(__file__).parents[1] / "scripts" / "montar_guia.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    item = m.item_do_guia({"categoria": "Mercados", "nome": "X", "lat": -28.13074, "lon": -48.66000, "fonte": "u"},
                          -28.13074, -48.66448)
    assert item["tipo"] == "guia" and 0.5 < item["km"] < 0.65 and item["fonte"] == "u"
    assert m.item_do_guia({"categoria": "C", "nome": "Y", "lat": "", "lon": ""}, 0, 0)["km"] is None


def test_mapa_e_servido_so_para_slug_valido(tmp_path, monkeypatch):
    import whatsapp.pousada_app as modulo

    (tmp_path / "rosa-teste.html").write_text("<h1>mapa</h1>", encoding="utf-8")
    monkeypatch.setattr(modulo, "MAPAS", tmp_path)
    c, _, _, _ = app()
    assert c.get("/mapa/rosa-teste").text == "<h1>mapa</h1>"
    assert c.get("/mapa/nao-existe").status_code == 404
    assert c.get("/mapa/..%2Fsegredo").status_code == 404


def test_gerar_mapa_escapa_html_e_lista_lugares():
    import importlib.util, pathlib

    spec = importlib.util.spec_from_file_location("gerar_mapa", pathlib.Path(__file__).parents[1] / "scripts" / "gerar_mapa.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    pagina = m.gerar({"nome": "Pousada <Teste>", "lat": -28.1, "lon": -48.6, "faq": [
        {"tipo": "guia", "categoria": "Mercados", "nome": "Mercado </script>", "km": 0.4, "lat": -28.1, "lon": -48.6}]})
    assert "Pousada &lt;Teste&gt;" in pagina and "</script>\"" not in pagina and "400 m · 5 min a pé" in pagina


# --- busca de lugares no Google Maps (bot/lugares.py) ------------------------------------------------
def _places(*lugares, status=200):
    import httpx

    pedidos = []

    def responder(req):
        pedidos.append(req)
        return httpx.Response(status, json={"places": list(lugares)})

    return httpx.Client(transport=httpx.MockTransport(responder)), pedidos


def test_buscar_lugares_ordena_filtra_fechados_e_junta_opiniao():
    from bot import lugares

    http, pedidos = _places(
        {"id": "longe", "displayName": {"text": "Super Longe"}, "location": {"latitude": -28.16, "longitude": -48.65},
         "businessStatus": "OPERATIONAL", "rating": 4.2, "userRatingCount": 80, "priceLevel": "PRICE_LEVEL_MODERATE"},
        {"id": "perto", "displayName": {"text": "Mercadinho"}, "location": {"latitude": -28.1295, "longitude": -48.6505},
         "businessStatus": "OPERATIONAL", "currentOpeningHours": {"openNow": True}},
        {"id": "fechou", "displayName": {"text": "Fechado"}, "location": {"latitude": -28.1293, "longitude": -48.6501},
         "businessStatus": "CLOSED_PERMANENTLY"})
    r = lugares.buscar("k", -28.1300000, -48.6500000, "mercado", idioma="es", opinioes={"perto": "caro"}, http=http)
    assert [l["nome"] for l in r["lugares"]] == ["Mercadinho", "Super Longe"] and r["fonte"] == "Google Maps"
    assert r["lugares"][0]["aberto_agora"] is True and r["lugares"][0]["opiniao_do_dono"] == "caro"
    assert r["lugares"][1]["faixa_de_preco_google"] == "$$ (moderado)"
    corpo = json.loads(pedidos[0].content)
    assert corpo["includedPrimaryTypes"] == ["supermarket", "grocery_store", "convenience_store", "market"] and corpo["languageCode"] == "es"
    assert pedidos[0].headers["X-Goog-Api-Key"] == "k" and "places.priceLevel" in pedidos[0].headers["X-Goog-FieldMask"]


def test_buscar_lugares_erro_e_categoria_invalida():
    from bot import lugares

    http, _ = _places(status=403)
    assert "erro" in lugares.buscar("k", 0, 0, "mercado", http=http)
    assert "erro" in lugares.buscar("k", 0, 0, "cassino", http=http)


def test_bot_usa_buscar_lugares_quando_disponivel():
    chamadas = []

    def buscar(categoria, idioma, termo=""):
        chamadas.append((categoria, idioma))
        return {"lugares": [{"nome": "Mercadinho", "distancia_km": 0.3}], "fonte": "Google Maps"}

    api = Api(_resp(("buscar_lugares", {"categoria": "mercado"})),
              _resp(_resposta("O mais perto é o Mercadinho, a 300 m. Fonte: Google Maps")))
    r = atender_hospede("onde tem mercado?", INFO, hospede={"idioma": "es"}, cliente=api, agora=AGORA,
                        buscar_lugares=buscar)
    assert chamadas == [("mercado", "es")] and "Mercadinho" in r.mensagem
    assert any(f["name"] == "buscar_lugares" for f in api.chamadas[0]["tools"])
    assert "Mercadinho" in api.chamadas[1]["messages"][-1]["content"][0]["content"]


def test_sem_busca_a_ferramenta_nem_aparece():
    api = Api(_resp(_resposta("oi")))
    atender_hospede("oi", INFO, cliente=api, agora=AGORA)
    assert all(f["name"] != "buscar_lugares" for f in api.chamadas[0]["tools"])


def test_mapa_do_google_so_com_chave_de_navegador(tmp_path, monkeypatch):
    import whatsapp.pousada_app as modulo

    (tmp_path / "rosa.html").write_text("osm", encoding="utf-8")
    (tmp_path / "rosa.google.html").write_text("key={{CHAVE_MAPS}}", encoding="utf-8")
    monkeypatch.setattr(modulo, "MAPAS", tmp_path)
    c, _, _, _ = app()
    assert c.get("/mapa/rosa").text == "osm"
    com_chave = criar_app_pousada(Config("https://x.uazapi.com", TOKEN, SEGREDO, rotinas=False, negocio="pousada",
                                         google_maps_browser_key="chave-navegador"),
                                  cliente=Cliente(), repo=Repo(), atender_fn=lambda *a, **k: None)
    assert TestClient(com_chave).get("/mapa/rosa").text == "key=chave-navegador"


def test_buscar_lugares_traz_os_mais_conhecidos_mais_longe():
    import httpx
    from bot import lugares

    def lugar(i, lat):
        return {"id": i, "displayName": {"text": i}, "location": {"latitude": lat, "longitude": -48.65}}

    perto = [lugar(f"mini{n}", -28.1293 - n * 0.0001) for n in range(20)]
    longe = [lugar("Exemplo", -28.17), lugar("mini0", -28.1293)]

    def responder(req):
        ordem = json.loads(req.content)["rankPreference"]
        return httpx.Response(200, json={"places": perto if ordem == "DISTANCE" else longe})

    r = lugares.buscar("k", -28.1293000, -48.6500000, "mercado",
                       http=httpx.Client(transport=httpx.MockTransport(responder)))
    assert [l["nome"] for l in r["lugares"]] == ["mini0", "mini1", "mini2", "mini3", "mini4"]
    assert [l["nome"] for l in r["mais_conhecidos_da_regiao"]] == ["Exemplo"]


def test_pousada_que_reserva_so_pelo_site_manda_o_link_e_nao_registra_pedido():
    info = PousadaInfo(INFO.nome, INFO.dono, INFO.cidade, INFO.faq, reserva_url="https://www.exemplo.com.br")
    api = Api(_resp(_resposta("As reservas são pelo site: https://www.exemplo.com.br")))
    r = atender_hospede("quero reservar de 10 a 15 de janeiro", info, cliente=api, agora=AGORA)
    assert r.pedido is None and "exemplo.com.br" in r.mensagem
    chamada = api.chamadas[0]
    assert all(f["name"] != "registrar_pedido_reserva" for f in chamada["tools"])
    assert "só pelo site da pousada" in chamada["system"][0]["text"] and "https://www.exemplo.com.br" in chamada["system"][0]["text"]


def test_pousada_com_pedido_mantem_o_fluxo_de_reserva():
    api = Api(_resp(_resposta("oi")))
    atender_hospede("oi", INFO, cliente=api, agora=AGORA)
    assert any(f["name"] == "registrar_pedido_reserva" for f in api.chamadas[0]["tools"])
    assert "## Pedido de reserva" in api.chamadas[0]["system"][0]["text"]


def test_busca_por_texto_livre_filtra_raio_e_ordena():
    from bot import lugares

    http, pedidos = _places(
        {"id": "a", "displayName": {"text": "Sorveteria Longe Demais"}, "location": {"latitude": -28.5, "longitude": -48.65}},
        {"id": "b", "displayName": {"text": "Gelato do Rosa"}, "location": {"latitude": -28.1300, "longitude": -48.6490}},
        {"id": "c", "displayName": {"text": "Açaí da Praia"}, "location": {"latitude": -28.1350, "longitude": -48.6450}})
    r = lugares.buscar_texto("k", -28.1300000, -48.6500000, "sorveteria", http=http)
    assert [l["nome"] for l in r["lugares"]] == ["Gelato do Rosa", "Açaí da Praia"]
    corpo = json.loads(pedidos[0].content)
    assert corpo["textQuery"] == "sorveteria" and str(pedidos[0].url).endswith("places:searchText")
    assert "erro" in lugares.buscar_texto("k", 0, 0, "  ", http=http)


# --- relatório mensal (bot/relatorio_pousada.py) ----------------------------------------------------
def test_relatorio_mensal_mostra_so_numeros_agregados():
    from datetime import date

    from bot.relatorio_pousada import MetricasMes, mes_anterior, montar

    assert mes_anterior(date(2027, 1, 1)) == (date(2026, 12, 1), date(2027, 1, 1))
    assert mes_anterior(date(2026, 11, 1)) == (date(2026, 10, 1), date(2026, 11, 1))
    m = MetricasMes(mensagens=62, hospedes=21, fora_do_horario=25, respondidas=48, encaminhadas=9, audios=4,
                    pedidos_reserva=0, idiomas={"pt": 14, "es": 6, "en": 1},
                    buscas=[("sorveteria", 5), ("farmacia", 3), ("aluguel de prancha", 1)])
    texto = montar("Pousada Exemplo do Rosa", date(2026, 10, 1), m)
    assert texto.split("\n") == [
        "📊 *Pousada Exemplo do Rosa · outubro de 2026*",
        "💬 62 mensagens de 21 pessoa(s)",
        "🤖 O assistente resolveu 48 sozinho; 9 vieram para você",
        "🌙 25 chegaram entre 22h e 8h (40%)",
        "🌐 6 em espanhol · 1 em inglês",
        "🎙️ 4 áudio(s) entendido(s)",
        "📍 Mais procurados na região: sorveteria (5), farmácia (3), aluguel de prancha (1)"]
    assert "Nenhum hóspede" in montar("X", date(2026, 10, 1), MetricasMes())


def test_busca_do_hospede_e_registrada_para_o_relatorio(monkeypatch):
    import whatsapp.pousada_app as modulo

    repo, cliente = Repo(), Cliente()
    repo.pousada.lat, repo.pousada.lon = -28.13, -48.65
    repo.buscas = []
    repo.registrar_busca = lambda *a: repo.buscas.append(a)
    monkeypatch.setattr(modulo.lugares, "buscar_texto", lambda *a, **k: {"lugares": [{"nome": "Gelato"}], "fonte": "Google Maps"})

    def atender(texto, info, **kw):
        kw["buscar_lugares"]("outro", "es", "sorveteria")
        return ResultadoPousada(acao="responder", mensagem="Gelato a 400 m.", idioma="es")

    a = criar_app_pousada(Config("https://x.uazapi.com", TOKEN, SEGREDO, rotinas=False, negocio="pousada",
                                 google_places_key="k"), cliente=cliente, repo=repo, atender_fn=atender)
    TestClient(a).post(URL, json=evento("¿hay heladería?"))
    assert repo.buscas == [("po1", "outro", "sorveteria", "es", 1)]


def test_previa_do_relatorio_exige_segredo():
    from bot.relatorio_pousada import MetricasMes

    repo = Repo()
    repo.pousada_por_slug = lambda slug: {"id": "po1", "slug": slug, "nome": "Pousada X", "telefone_aviso": DONO} if slug == "x" else None
    repo.metricas_mes = lambda pid, ini, fim: MetricasMes(mensagens=3, hospedes=2, respondidas=3)
    a = TestClient(criar_app_pousada(Config("https://x.uazapi.com", TOKEN, SEGREDO, rotinas=False, negocio="pousada"),
                                     cliente=Cliente(), repo=repo, atender_fn=lambda *a, **k: None))
    assert a.get(f"/relatorio/{SEGREDO}/x?mes=2026-10").text.startswith("📊 *Pousada X · outubro de 2026*")
    assert a.get("/relatorio/errado-errado-errado/x").status_code == 404
    assert a.get(f"/relatorio/{SEGREDO}/nao-existe").status_code == 404


# --- proteções: contato salvo e dono que assume a conversa pelo celular -------------------------------
def _envio_do_celular(para=HOSPEDE, msg_id="D1", pela_api=False):
    return {"EventType": "messages", "token": TOKEN, "instanceName": "pousada-rosa",
            "message": {"messageid": msg_id, "chatid": f"{para}@s.whatsapp.net", "sender_pn": f"{DONO}@s.whatsapp.net",
                        "fromMe": True, "isGroup": False, "messageType": "Conversation", "text": "oi, aqui é a Carla",
                        "wasSentByApi": pela_api},
            "chat": {"phone": para}}


def _com_pausa(repo):
    repo.pausas = []

    def pausar(pousada_id, telefone, horas):
        repo.pausas.append((telefone, horas))
        nova, repo.h.pausado = not repo.h.pausado, True
        return nova   # True = pausa nova (o dono recebe o aviso ⏸️)

    repo.pausar_hospede = pausar
    return repo


def test_extrai_envio_do_celular_so_quando_nao_foi_a_api():
    from whatsapp.uazapi import extrair_envio_do_celular

    assert extrair_envio_do_celular(_envio_do_celular()) == ("pousada-rosa", HOSPEDE)
    assert extrair_envio_do_celular(_envio_do_celular(pela_api=True)) is None
    assert extrair_envio_do_celular(evento("oi")) is None   # mensagem do hóspede não é envio do dono


def test_contato_salvo_na_agenda_nao_e_atendido_salvo_se_a_pousada_quiser():
    c, cliente, repo, chamadas = app()
    salvo = evento("oi, é o fornecedor do gás")
    salvo["chat"]["wa_contactName"] = "Zé do Gás"
    c.post(URL, json=salvo)
    assert cliente.enviadas == [] and chamadas == [] and repo.msgs == []
    repo.pousada.responder_contatos_salvos = True
    c.post(URL, json={**salvo, "message": {**salvo["message"], "messageid": "M2"}})
    assert len(chamadas) == 1


def test_dono_responde_pelo_celular_e_o_bot_fica_quieto_no_chat():
    c, cliente, repo, chamadas = app()
    _com_pausa(repo)
    assert c.post(URL, json=_envio_do_celular()).json()["dono"] is True
    assert repo.pausas == [(HOSPEDE, 3)]
    c.post(URL, json=evento("e o café da manhã?", msg_id="M5"))
    assert chamadas == [] and [n for n, _ in cliente.enviadas] == [DONO]   # só o aviso ⏸️ ao dono
    assert cliente.enviadas[0][1].startswith("⏸️ *Atendimento virtual pausado")
    assert repo.msgs[-1][:2] == ("entrada", "e o café da manhã?")   # a mensagem fica registrada mesmo assim


def test_eco_da_resposta_do_bot_nao_pausa():
    c, cliente, repo, chamadas = app()
    _com_pausa(repo)
    c.post(URL, json=evento("oi"))                       # o bot responde agora
    c.post(URL, json=_envio_do_celular(msg_id="ECO"))    # a uazapi devolve a resposta do bot como fromMe
    assert repo.pausas == []


def test_digitando_proporcional_ao_texto_e_aviso_ao_dono_sem_espera():
    from whatsapp.pousada_app import tempo_digitando

    assert tempo_digitando("Sim!", sorteio=1.0) == 2000            # curto: mínimo de 2 s
    assert tempo_digitando("x" * 120, sorteio=1.0) == 3360         # médio: proporcional
    assert tempo_digitando("x" * 2000, sorteio=1.15) == 6000       # longo: teto de 6 s
    c, cliente, _, _ = app(ResultadoPousada(acao="encaminhar", mensagem="Ya se lo pasé a Carla.", idioma="es",
                                            motivo="desconto", resumo_para_dono="Pede desconto."))
    c.post(URL, json=evento("¿descuento?"))
    assert cliente.digitando[0] >= 2000 and cliente.digitando[1] == 0   # hóspede espera; o aviso ao dono sai na hora


def test_eco_com_o_numero_em_outro_formato_nao_pausa():
    """O bot respondeu para 5548988887777 e o eco chega como 554888887777 (sem o 9): antes pausava o próprio chat."""
    c, cliente, repo, chamadas = app()
    _com_pausa(repo)
    c.post(URL, json=evento("oi", de="5548988887777"))
    assert cliente.enviadas
    c.post(URL, json=_envio_do_celular(para="554888887777", msg_id="ECO"))
    assert repo.pausas == []
    c.post(URL, json=_envio_do_celular(para="5548911112222", msg_id="D9"))   # outro chat: é o dono mesmo
    assert repo.pausas == [("5548911112222", 3)]


# --- prazo de guarda (LGPD) ---------------------------------------------------------------------------
def test_prazo_de_guarda_chama_o_repo_e_falha_nao_derruba():
    from whatsapp.pousada_app import aplicar_prazo_de_guarda

    repo = Repo()
    repo.aplicar_prazo_de_guarda = lambda dias: {"mensagens": 12, "pedidos": 1, "hospedes": 3, "dias": dias}
    assert aplicar_prazo_de_guarda(repo)["dias"] == 365

    def falha(dias):
        raise RuntimeError("banco fora")

    repo.aplicar_prazo_de_guarda = falha
    assert aplicar_prazo_de_guarda(repo) is None


def test_agendador_apaga_conversas_as_3h_de_brasilia():
    cliente = Cliente()
    cliente.status = lambda: {"conectado": True, "estado": "open"}
    a = criar_app_pousada(Config("https://x.uazapi.com", TOKEN, SEGREDO, rotinas=True, negocio="pousada"),
                          cliente=cliente, repo=Repo(), atender_fn=lambda *a, **k: None)
    for job_id, hora in (("prazo_de_guarda", "3"), ("relatorio_mensal", "9")):
        trigger = a.state.agendador.get_job(job_id).trigger
        assert str(trigger.timezone) == "America/Sao_Paulo"           # não o fuso do servidor (UTC no Railway)
        assert str(trigger.fields[trigger.FIELD_NAMES.index("hour")]) == hora
    rotinas = TestClient(a).get("/saude").json()["rotinas"]
    assert rotinas == ["monitor_whatsapp", "prazo_de_guarda", "relatorio_mensal"]


class _DbFalso:
    def __init__(self):
        self.sqls = []

    def executar(self, sql, params=(), *, retorno=None, como_dict=False):
        self.sqls.append((sql, params))
        return {"n": len(self.sqls)}


def _repo_postgres_falso():
    from whatsapp.pousada_repo import RepositorioPousadaPostgres

    repo = RepositorioPousadaPostgres.__new__(RepositorioPousadaPostgres)
    repo._db = _DbFalso()
    return repo


def test_prazo_de_guarda_sql_funciona_sem_a_coluna_e_respeita_pedidos():
    repo = _repo_postgres_falso()
    assert repo.aplicar_prazo_de_guarda() == {"mensagens": 1, "pedidos": 2, "hospedes": 3}
    (msgs, params), (pedidos, _), (hospedes, _) = repo._db.sqls
    assert params == (365, 30)                                          # padrão e mínimo contra apagar tudo
    for sql in (msgs, pedidos, hospedes):
        assert "to_jsonb(p) ->> 'retencao_dias'" in sql                 # sem a coluna (006 não aplicada): usa 365
    assert "delete from public.mensagens" in msgs
    assert "greatest(prazo.dias, 365)" in pedidos                      # pedido de reserva fica pelo menos 1 ano
    assert "not exists (select 1 from public.pedidos_reserva" in hospedes and "public.mensagens m" in hospedes


def test_pausar_hospede_existe_no_repositorio_do_postgres():
    """Antes ficava só no Protocol: no Postgres dava AttributeError e a pausa de 3 h nunca valia."""
    repo = _repo_postgres_falso()
    repo.hospede = lambda pousada_id, telefone: None
    repo.pausar_hospede("po1", HOSPEDE, 3)
    sql, params = repo._db.sqls[-1]
    assert "pausado_ate = now() + make_interval(secs => %s)" in sql and params[0] == 3 * 3600
