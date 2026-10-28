"""Lumi para negócios de serviços: agenda (horários, marcar, remarcar, cancelar), bot e webhook. Sem internet:
Google Calendar, IA, uazapi e banco simulados."""

from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from agenda.horarios import sugerir, todos_os_horarios
from agenda.servicos import AgendaNegocio, HorarioOcupado, ler_config, telefone_no_texto
from bot import negocio
from bot.negocio import NegocioInfo, ResultadoNegocio, atender_cliente, checar_mensagem
from whatsapp.app import Config
from whatsapp.pousada_app import criar_app_pousada
from whatsapp.pousada_repo import Hospede, Pousada

FUSO = ZoneInfo("America/Sao_Paulo")
AGORA = datetime(2026, 10, 6, 10, 0, tzinfo=FUSO)   # terça
CLIENTE = "5551999990000"
SEMANA = {str(d): ["09:00-12:00", "13:00-19:00"] for d in range(2, 7)}   # terça a sábado
CONFIG = {
    "antecedencia_horas": 2, "janela_dias": 14, "passo_min": 30,
    "servicos": [{"id": "corte", "nome": "Corte", "duracao_min": 30},
                 {"id": "escova", "nome": "Escova", "duracao_min": 60}],
    "profissionais": [{"id": "carla", "nome": "Carla", "calendar_id": "cal-carla", "atendimento": SEMANA},
                      {"id": "ze", "nome": "Zé", "calendar_id": "cal-ze", "atendimento": SEMANA, "servicos": ["corte"]}],
}


def h(dia, hora, minuto=0):
    return datetime(2026, 10, dia, hora, minuto, tzinfo=FUSO)


# ---------------------------------------------------------------------
# Simulações
# ---------------------------------------------------------------------
class Calendario:
    def __init__(self):
        self.eventos = {"cal-carla": {}, "cal-ze": {}}
        self.n = 0

    def ocupados(self, calendar_id, inicio, fim):
        return [(e["inicio"], e["fim"]) for e in self.eventos[calendar_id].values()]

    def criar_evento(self, calendar_id, inicio, fim, titulo, descricao="", fuso="America/Sao_Paulo"):
        self.n += 1
        ev = f"ev{self.n}"
        self.eventos[calendar_id][ev] = {"id": ev, "inicio": inicio, "fim": fim, "titulo": titulo, "descricao": descricao}
        return ev

    def cancelar_evento(self, calendar_id, evento_id):
        del self.eventos[calendar_id][evento_id]

    def listar_eventos(self, calendar_id, inicio, fim):
        return list(self.eventos[calendar_id].values())


class Banco:
    def __init__(self):
        self.ags, self.recusar = {}, False

    def gravar(self, ag):
        if self.recusar:
            raise HorarioOcupado("sobreposição")
        ag_id = f"a{len(self.ags) + 1}"
        self.ags[ag_id] = {"id": ag_id, "status": "confirmado", **ag}
        return ag_id

    def listar(self):
        return [a for a in self.ags.values() if a["status"] == "confirmado"]

    def cancelar(self, ag_id):
        self.ags[ag_id]["status"] = "cancelado"


def agenda(cal=None, banco=None):
    cal, banco = cal or Calendario(), banco or Banco()
    return AgendaNegocio(cal, ler_config(CONFIG), cliente_telefone=CLIENTE, gravar=banco.gravar, listar=banco.listar,
                         cancelar_registro=banco.cancelar, agora=lambda: AGORA), cal, banco


# ---------------------------------------------------------------------
# Horários
# ---------------------------------------------------------------------
def test_horarios_respeitam_duracao_passo_antecedencia_e_ocupado():
    ocupado = [(h(6, 14), h(6, 15))]
    livres = todos_os_horarios(SEMANA, ocupado, AGORA, FUSO, duracao_min=60, passo_min=30, antecedencia_horas=2,
                               janela_dias=0)
    # hoje: antecedência de 2 h (a partir de 12:00, mas 11:00-12:00 é o último da manhã), almoço e o ocupado
    assert [t.strftime("%H:%M") for t in livres] == ["13:00", "15:00", "15:30", "16:00", "16:30", "17:00", "17:30", "18:00"]


def test_sugestoes_espalhadas_por_dia():
    livres = [h(6, 9), h(6, 9, 30), h(6, 13), h(6, 15), h(7, 9), h(7, 10)]
    assert sugerir(livres) == [h(6, 9), h(6, 13), h(7, 9)]


def test_config_invalida_explica_o_problema():
    with pytest.raises(ValueError, match="sem serviços"):
        ler_config({"profissionais": CONFIG["profissionais"]})
    with pytest.raises(ValueError, match="não está na lista"):
        ler_config({**CONFIG, "profissionais": [{**CONFIG["profissionais"][1], "servicos": ["barba"]}]})
    with pytest.raises(ValueError, match="atendimento inválido"):
        ler_config({**CONFIG, "profissionais": [{**CONFIG["profissionais"][0], "atendimento": {"2": ["9h-12h"]}}]})


def test_telefone_no_texto_com_e_sem_o_9():
    assert telefone_no_texto("Corte – Ana (51) 99999-0000", CLIENTE)
    assert telefone_no_texto("Ana 51 9999 0000", CLIENTE)
    assert not telefone_no_texto("Ana 99999-0000", CLIENTE)   # sem DDD não vale


# ---------------------------------------------------------------------
# Agenda do negócio
# ---------------------------------------------------------------------
def test_ver_livres_so_com_quem_faz_o_servico():
    ag, _, _ = agenda()
    r = ag.ver_horarios_livres("escova")
    assert r["duracao_min"] == 60 and {x["profissional"] for x in r["horarios"]} == {"carla"}
    assert "não faz Escova" in ag.ver_horarios_livres("Escova", "Zé")["erro"]
    assert "Corte, Escova" in ag.ver_horarios_livres("progressiva")["erro"]
    assert r["horarios"][0] == {"inicio": "2026-10-06T13:00", "profissional": "carla", "profissional_nome": "Carla",
                                "descricao": "ter 06/10 às 13:00"}


def test_marcar_cria_evento_grava_e_avisa():
    ag, cal, banco = agenda()
    r = ag.marcar_horario("escova", "carla", "2026-10-07T09:00", "Juliana")
    assert r["marcado"] and r["descricao"] == "qua 07/10 às 09:00" and r["profissional"] == "Carla"
    ev = next(iter(cal.eventos["cal-carla"].values()))
    assert ev["titulo"] == "Escova – Juliana" and CLIENTE in ev["descricao"] and ev["fim"] == h(7, 10)
    assert banco.ags["a1"]["servico_id"] == "escova" and ag.eventos[0]["tipo"] == "marcado"
    # o mesmo horário não aparece mais e não pode ser marcado de novo
    assert not ag.marcar_horario("corte", "carla", "2026-10-07T09:30", "Outra")["marcado"]


def test_marcar_fora_do_atendimento_ou_sem_nome_e_recusado():
    ag, cal, _ = agenda()
    assert not ag.marcar_horario("corte", "ze", "2026-10-07T12:00", "Ana")["marcado"]   # almoço
    assert not ag.marcar_horario("corte", "ze", "2026-10-11T10:00", "Ana")["marcado"]   # domingo
    assert "nome" in ag.marcar_horario("corte", "ze", "2026-10-07T10:00", " ")["erro"]
    assert cal.eventos == {"cal-carla": {}, "cal-ze": {}}


def test_banco_recusa_sobreposicao_e_o_evento_e_desfeito():
    ag, cal, banco = agenda()
    banco.recusar = True
    r = ag.marcar_horario("corte", "ze", "2026-10-07T10:00", "Ana")
    assert not r["marcado"] and "acabou de ser ocupado" in r["erro"] and cal.eventos["cal-ze"] == {}


def test_remarcar_marca_o_novo_antes_de_cancelar_o_antigo():
    ag, cal, banco = agenda()
    ag.marcar_horario("corte", "ze", "2026-10-07T10:00", "Ana")
    r = ag.remarcar_horario("a1", "2026-10-08T15:00", "carla")
    assert r["remarcado"] and r["de"] == "qua 07/10 às 10:00" and r["para"] == "qui 08/10 às 15:00"
    assert cal.eventos["cal-ze"] == {} and len(cal.eventos["cal-carla"]) == 1
    assert banco.ags["a1"]["status"] == "cancelado" and banco.ags["a2"]["profissional_id"] == "carla"


def test_meus_horarios_inclui_o_que_foi_marcado_a_mao_com_o_telefone():
    ag, cal, _ = agenda()
    cal.criar_evento("cal-carla", h(9, 14), h(9, 15), "Escova – Ana", "tel (51) 99999-0000")
    cal.criar_evento("cal-carla", h(9, 16), h(9, 17), "Escova – Bia", "tel (51) 98888-0000")
    r = ag.meus_horarios()
    assert [x["descricao"] for x in r["horarios"]] == ["sex 09/10 às 14:00"]
    c = ag.cancelar_horario(r["horarios"][0]["agendamento_id"])
    assert c["cancelado"] and len(cal.eventos["cal-carla"]) == 1


# ---------------------------------------------------------------------
# Bot (IA simulada)
# ---------------------------------------------------------------------
INFO = NegocioInfo("Studio Bela", "Carla", "Porto Alegre",
                   [{"id": "cancelamento", "pt": {"p": "Posso cancelar?", "r": "Sim, até 24 h antes, sem custo."}}],
                   servicos=[{"nome": "Corte", "duracao_min": 30}, {"nome": "Escova", "duracao_min": 60}],
                   profissionais=[{"nome": "Carla", "servicos": []}, {"nome": "Zé", "servicos": ["Corte"]}])


def _resp(*chamadas):
    return NS(content=[NS(type="tool_use", id=f"t{i}", name=n, input=e) for i, (n, e) in enumerate(chamadas)],
              stop_reason="tool_use", usage=NS(input_tokens=10, output_tokens=5))


class Api:
    def __init__(self, *respostas):
        self.respostas, self.chamadas = list(respostas), []
        self.messages = self

    def create(self, **kw):
        self.chamadas.append({**kw, "messages": list(kw["messages"])})
        return self.respostas.pop(0)


def _resposta(mensagem, acao="responder", idioma="pt", resumo=""):
    return ("enviar_resposta", {"acao": acao, "mensagem": mensagem, "idioma": idioma, "motivo": "",
                                "resumo_para_dono": resumo})


def test_bot_ve_livres_marca_e_confirma():
    ag, cal, _ = agenda()
    api = Api(_resp(("marcar_horario", {"servico": "Escova", "profissional": "carla", "inicio": "2026-10-07T09:00",
                                        "nome": "Juliana"})),
              _resp(_resposta("Pronto, Juliana! Escova com a Carla marcada para quarta, 07/10, às 9h.")))
    r = atender_cliente("pode ser quarta 9h", INFO, agenda=ag, cliente=api, agora=AGORA)
    assert r.acao == "responder" and r.nome == "Juliana" and [e["tipo"] for e in r.agenda] == ["marcado"]
    assert len(cal.eventos["cal-carla"]) == 1
    system = api.chamadas[0]["system"][0]["text"]
    assert "Escova (60 min)" in system and "Zé (Corte)" in system and "até 24 h antes" in system
    assert [f["name"] for f in api.chamadas[0]["tools"]][:2] == ["ver_horarios_livres", "marcar_horario"]


def test_bot_nao_pode_dizer_que_marcou_sem_a_agenda():
    ag, _, _ = agenda()
    api = Api(_resp(_resposta("Prontinho, seu horário está marcado para quarta às 9h!")),
              _resp(_resposta("Quer que eu veja os horários livres de quarta?")))
    r = atender_cliente("quero escova quarta", INFO, agenda=ag, cliente=api, agora=AGORA)
    assert r.mensagem == "Quer que eu veja os horários livres de quarta?" and r.agenda == []
    assert "não marcou" in api.chamadas[1]["messages"][-1]["content"][0]["content"]


def test_sem_agenda_o_prompt_manda_anotar_e_encaminhar():
    api = Api(_resp(_resposta("Anotei! Passei para a Carla confirmar o horário; ainda não está marcado.",
                              acao="encaminhar", resumo="Escova, quarta de manhã, Juliana.")))
    r = atender_cliente("quero escova quarta de manhã, sou a Juliana", INFO, cliente=api, agora=AGORA)
    assert r.acao == "encaminhar" and not r.fallback
    assert "Você não vê a agenda" in api.chamadas[0]["system"][0]["text"]
    assert [f["name"] for f in api.chamadas[0]["tools"]] == ["enviar_resposta"]


@pytest.mark.parametrize("texto", ["Chave Pix: 51999990000", "Te dou 10% de desconto",
                                   "Seu horário está confirmado!", "You're booked for Wednesday."])
def test_trava_barra(texto):
    assert checar_mensagem(texto) is not None


@pytest.mark.parametrize("texto", ["Quer que eu agende a escova?", "Ainda não está marcado: a Carla confirma.",
                                   "A escova custa R$ 60 e leva 1 hora."])
def test_trava_deixa_passar(texto):
    assert checar_mensagem(texto) is None


def test_agenda_fora_do_ar_nao_derruba_o_bot():
    class Quebrada:
        eventos = []

        def ver_horarios_livres(self, **kw):
            raise RuntimeError("google fora")
    api = Api(_resp(("ver_horarios_livres", {"servico": "Corte", "profissional": "", "a_partir_de": "", "periodo": "qualquer"})),
              _resp(_resposta("Passei para a Carla ver o horário com você.", acao="encaminhar", resumo="Corte.")))
    r = atender_cliente("tem horário?", INFO, agenda=Quebrada(), cliente=api, agora=AGORA)
    assert r.acao == "encaminhar"
    assert "agenda indisponível" in api.chamadas[1]["messages"][-1]["content"][0]["content"]


# ---------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------
TOKEN, SEGREDO, DONO = "token-salao", "segredo-salao-123456", "5551988880000"


class Cliente:
    def __init__(self):
        self.enviadas = []

    def enviar_texto(self, numero, texto, digitando_ms=1200):
        self.enviadas.append((numero, texto))
        return f"wa-{len(self.enviadas)}"


class Repo:
    def __init__(self, agenda_config=CONFIG):
        self.pousada = Pousada(id="s1", nome="Studio Bela", dono="Carla", telefone_aviso=DONO, slug="studio-bela",
                               faq=INFO.faq, tipo="servicos", agenda_config=agenda_config)
        self.h = Hospede(id="c1", telefone=CLIENTE)
        self.msgs, self.ags = [], {}

    def pousada_por_instancia(self, instancia):
        return self.pousada

    def pousadas_ativas(self):
        return [self.pousada]

    def hospede(self, pousada_id, telefone):
        return self.h

    def atualizar_hospede(self, hospede_id, campos):
        for k, v in campos.items():
            setattr(self.h, k, v)

    def registrar_mensagem(self, pousada_id, hospede_id, direcao, wa_id, conteudo, acao=None, motivo=None):
        self.msgs.append((direcao, conteudo))
        return True

    def historico(self, hospede_id, antes_de_wa_id, limite=10):
        return []

    def gravar_agendamento(self, pousada_id, hospede_id, ag):
        self.ags[f"a{len(self.ags) + 1}"] = ag
        return f"a{len(self.ags)}"

    def agendamentos_do_cliente(self, pousada_id, telefone):
        return []

    def cancelar_agendamento(self, pousada_id, ag_id):
        pass


def evento(texto):
    return {"EventType": "messages", "token": TOKEN, "instanceName": "studio-bela",
            "message": {"messageid": "M1", "chatid": f"{CLIENTE}@s.whatsapp.net", "sender_pn": f"{CLIENTE}@s.whatsapp.net",
                        "fromMe": False, "isGroup": False, "messageType": "Conversation", "text": texto,
                        "wasSentByApi": False},
            "chat": {"phone": CLIENTE}}


def test_webhook_de_servicos_usa_o_bot_de_negocio_e_avisa_o_dono_do_horario():
    repo, cliente, cal, recebido = Repo(), Cliente(), Calendario(), {}

    def atender(texto, info, **kw):
        recebido.update(info=info, agenda=kw["agenda"])
        kw["agenda"]._agora = lambda: AGORA
        kw["agenda"].marcar_horario("corte", "ze", "2026-10-07T10:00", "Ana")
        return ResultadoNegocio(acao="responder", mensagem="Pronto, Ana!", nome="Ana", agenda=list(kw["agenda"].eventos))

    app = criar_app_pousada(Config("https://x.uazapi.com", TOKEN, SEGREDO, rotinas=False), cliente=cliente, repo=repo,
                            atender_negocio_fn=atender, calendario=cal)
    assert TestClient(app).post(f"/webhook/uazapi/{SEGREDO}", json=evento("corte amanhã 10h, Ana")).status_code == 200
    assert recebido["info"].servicos[0] == {"nome": "Corte", "duracao_min": 30}
    assert recebido["info"].profissionais[1] == {"id": "ze", "nome": "Zé", "servicos": ["Corte"]}
    assert cliente.enviadas[0] == (CLIENTE, "Pronto, Ana!") and repo.h.nome == "Ana"
    assert cliente.enviadas[1] == (DONO, "📅 *Horário marcado · Ana*\n✂️ Corte com Zé\n🕐 qua 07/10 às 10:00\n"
                                         f"👉 Conversa: wa.me/{CLIENTE}")
    assert repo.ags["a1"]["calendar_id"] == "cal-ze"


def test_webhook_sem_conta_do_google_atende_sem_agenda():
    repo, cliente, recebido = Repo(), Cliente(), {}

    def atender(texto, info, **kw):
        recebido["agenda"] = kw["agenda"]
        return ResultadoNegocio(acao="encaminhar", mensagem="Passei para a Carla.", resumo_para_dono="Corte amanhã.")

    app = criar_app_pousada(Config("https://x.uazapi.com", TOKEN, SEGREDO, rotinas=False), cliente=cliente, repo=repo,
                            atender_negocio_fn=atender)
    TestClient(app).post(f"/webhook/uazapi/{SEGREDO}", json=evento("corte amanhã"))
    assert recebido["agenda"] is None
    assert cliente.enviadas[1][0] == DONO and "(cliente)" not in cliente.enviadas[1][1]   # sem nome: "cliente"
    assert cliente.enviadas[1][1].startswith("🟡 *Responder · cliente*")
