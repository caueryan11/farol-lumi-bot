"""Modo pousada com várias pousadas no mesmo servidor: uma instância da uazapi (número e token) por pousada.
IA, uazapi e banco simulados; sem internet."""

import logging
from datetime import date

from fastapi.testclient import TestClient

from bot.pousada import ResultadoPousada
from testes import test_pousada as base   # módulo inteiro: importar as funções test_* faria o pytest rodá-las de novo
from whatsapp.app import Config
from whatsapp.pousada_app import criar_app_pousada
from whatsapp.pousada_repo import Hospede, Pousada

TOKEN_ENV, SEGREDO = base.TOKEN, base.SEGREDO      # UAZAPI_TOKEN do ambiente
TOKEN_ROSA, TOKEN_MAR = "token-instancia-rosa", "token-instancia-mar"
URL_ENV = "https://x.uazapi.com"
DONO_ROSA, DONO_MAR, DONO_SOL = "5548999990000", "5548988880000", "5548977770000"
HOSPEDE = base.HOSPEDE


class Cliente(base.Cliente):
    def __init__(self, url="", token="", conectado=True):
        super().__init__()
        self.url, self.token, self.conectado = url, token, conectado

    def status(self):
        return {"conectado": self.conectado, "estado": "connected" if self.conectado else "disconnected"}


class Repo:
    """Três pousadas: rosa e mar com instância própria (token no banco); sol sem token (usa o do ambiente)."""

    def __init__(self):
        self.pousadas = {
            "pousada-rosa": Pousada(id="po-rosa", slug="rosa", nome="Pousada Rosa", dono="Carla",
                                    telefone_aviso=DONO_ROSA, instancia="pousada-rosa", uazapi_token=TOKEN_ROSA),
            "pousada-mar": Pousada(id="po-mar", slug="mar", nome="Pousada do Mar", dono="Léo",
                                   telefone_aviso=DONO_MAR, instancia="pousada-mar", uazapi_token=TOKEN_MAR,
                                   uazapi_url="https://outro.uazapi.com"),
            "pousada-sol": Pousada(id="po-sol", slug="sol", nome="Pousada do Sol", dono="Ana",
                                   telefone_aviso=DONO_SOL, instancia="pousada-sol"),
        }
        self.hospedes, self.msgs, self.pausas = {}, [], []

    def pousada_por_instancia(self, instancia):
        return self.pousadas.get(instancia)

    def pousadas_ativas(self):
        return list(self.pousadas.values())

    def pousadas_para_relatorio(self):
        return [{"id": p.id, "slug": p.slug, "nome": p.nome, "telefone_aviso": p.telefone_aviso,
                 "instancia": p.instancia, "uazapi_token": p.uazapi_token, "uazapi_url": p.uazapi_url}
                for p in self.pousadas.values()]

    def metricas_mes(self, pousada_id, inicio, fim):
        from bot.relatorio_pousada import MetricasMes

        return MetricasMes(mensagens=1, hospedes=1, respondidas=1)

    def hospede(self, pousada_id, telefone):
        return self.hospedes.setdefault((pousada_id, telefone), Hospede(id=f"h-{pousada_id}", telefone=telefone))

    def pausar_hospede(self, pousada_id, telefone, horas):
        self.pausas.append((pousada_id, telefone))
        self.hospede(pousada_id, telefone).pausado = True

    def atualizar_hospede(self, hospede_id, campos):
        pass

    def registrar_mensagem(self, pousada_id, hospede_id, direcao, wa_id, conteudo, acao=None, motivo=None):
        self.msgs.append((pousada_id, direcao, conteudo))
        return True

    def historico(self, hospede_id, antes_de_wa_id, limite=10):
        return []

    def registrar_pedido(self, pousada_id, hospede_id, pedido, idioma):
        return "pe1"


def evento(instancia, token, texto="Hola", msg_id="M1", tipo="Conversation"):
    return {**base.evento(texto, msg_id=msg_id, tipo=tipo), "instanceName": instancia, "token": token}


def envio_do_celular(instancia, token, para=HOSPEDE, msg_id="D1"):
    return {**base._envio_do_celular(para=para, msg_id=msg_id), "instanceName": instancia, "token": token}


def app(resultado=None, uazapi_token=TOKEN_ENV, rotinas=False, chave_openai="", transcricao=""):
    """Devolve (TestClient, repo, clientes): clientes[token] é o cliente da uazapi daquela instância."""
    repo, atendidos = Repo(), []
    padrao = Cliente(URL_ENV, uazapi_token)
    padrao.transcricao = transcricao
    clientes = {uazapi_token: padrao}

    def fabrica(url, token):
        c = Cliente(url, token)
        c.transcricao = transcricao
        clientes[token] = c
        return c

    def atender(texto, info, **kw):
        atendidos.append(info.nome)
        return resultado or ResultadoPousada(acao="responder", mensagem=f"Oi, aqui é a {info.nome}.", idioma="pt")

    a = criar_app_pousada(Config(URL_ENV, uazapi_token, SEGREDO, rotinas=rotinas, negocio="pousada",
                                 openai_api_key=chave_openai),
                          cliente=padrao, repo=repo, atender_fn=atender, fabrica_cliente=fabrica)
    t = TestClient(a)
    t.atendidos = atendidos
    return t, repo, clientes


def enviadas(clientes, token):
    return clientes[token].enviadas if token in clientes else []


# ---------------------------------------------------------------------
# Webhook: cada instância só com o próprio token
# ---------------------------------------------------------------------
def test_cada_pousada_aceita_so_o_token_da_propria_instancia():
    c, _, _ = app()
    assert c.post(base.URL, json=evento("pousada-rosa", TOKEN_ROSA, msg_id="A1")).status_code == 200
    assert c.post(base.URL, json=evento("pousada-mar", TOKEN_MAR, msg_id="B1")).status_code == 200
    # token trocado entre as pousadas, ou o do ambiente numa pousada que tem token próprio: recusado
    assert c.post(base.URL, json=evento("pousada-rosa", TOKEN_MAR, msg_id="A2")).status_code == 401
    assert c.post(base.URL, json=evento("pousada-mar", TOKEN_ROSA, msg_id="B2")).status_code == 401
    assert c.post(base.URL, json=evento("pousada-mar", TOKEN_ENV, msg_id="B3")).status_code == 401
    assert c.post(base.URL, json=evento("pousada-mar", "", msg_id="B4")).status_code == 401
    assert c.atendidos == ["Pousada Rosa", "Pousada do Mar"]


def test_instancia_desconhecida_e_recusada_e_o_token_nao_vai_para_o_log(caplog):
    c, repo, clientes = app()
    with caplog.at_level(logging.WARNING, logger="pousada"):
        assert c.post(base.URL, json=evento("pousada-fantasma", TOKEN_ROSA)).status_code == 401
        assert c.post(base.URL, json=evento("", TOKEN_ENV)).status_code == 401
        assert c.post(base.URL, json=evento("pousada-rosa", TOKEN_MAR)).status_code == 401
    assert c.atendidos == [] and repo.msgs == [] and all(not cl.enviadas for cl in clientes.values())
    assert "pousada-fantasma" in caplog.text
    for segredo in (TOKEN_ROSA, TOKEN_MAR, TOKEN_ENV):
        assert segredo not in caplog.text


def test_token_nao_aparece_no_repr_da_pousada():
    assert TOKEN_ROSA not in repr(Repo().pousadas["pousada-rosa"])


# ---------------------------------------------------------------------
# Envio: tudo sai pela instância da pousada certa
# ---------------------------------------------------------------------
def test_resposta_e_aviso_ao_dono_saem_pela_instancia_da_pousada():
    encaminhar = ResultadoPousada(acao="encaminhar", mensagem="Já passei para o Léo.", idioma="pt",
                                  motivo="desconto", resumo_para_dono="Pede desconto.")
    c, _, clientes = app(encaminhar)
    c.post(base.URL, json=evento("pousada-mar", TOKEN_MAR, "tem desconto?"))
    mar = clientes[TOKEN_MAR]
    assert (mar.url, mar.token) == ("https://outro.uazapi.com", TOKEN_MAR)   # URL própria da pousada
    assert [n for n, _ in mar.enviadas] == [HOSPEDE, DONO_MAR]                 # resposta e aviso 🟡, pelo número dela
    assert mar.enviadas[1][1].startswith("🟡")
    assert enviadas(clientes, TOKEN_ROSA) == [] and enviadas(clientes, TOKEN_ENV) == []


def test_pedido_de_reserva_avisa_o_dono_pelo_numero_da_pousada():
    pedido = {"entrada": "2027-01-10", "saida": "2027-01-12", "noites": 2, "adultos": 2, "criancas_idades": [],
              "nome": "Ana Souza", "contato": "", "acomodacao": "", "observacoes": ""}
    c, _, clientes = app(ResultadoPousada(acao="responder", mensagem="Anotado!", idioma="pt", pedido=pedido))
    c.post(base.URL, json=evento("pousada-rosa", TOKEN_ROSA, "quero reservar"))
    assert [n for n, _ in clientes[TOKEN_ROSA].enviadas] == [HOSPEDE, DONO_ROSA]
    assert clientes[TOKEN_ROSA].enviadas[1][1].startswith("🛎️")
    assert enviadas(clientes, TOKEN_MAR) == [] and enviadas(clientes, TOKEN_ENV) == []


def test_pousada_sem_token_cadastrado_usa_o_token_do_ambiente():
    c, _, clientes = app()
    assert c.post(base.URL, json=evento("pousada-sol", TOKEN_ENV)).status_code == 200
    assert clientes[TOKEN_ENV].enviadas == [(HOSPEDE, "Oi, aqui é a Pousada do Sol.")]
    assert c.post(base.URL, json=evento("pousada-sol", TOKEN_ROSA, msg_id="S2")).status_code == 401
    assert TOKEN_ROSA not in clientes   # nada saiu pela instância da rosa


def test_sem_token_no_ambiente_so_pousadas_com_token_proprio():
    c, _, clientes = app(uazapi_token="")
    assert c.post(base.URL, json=evento("pousada-sol", "", msg_id="S1")).status_code == 401
    assert c.post(base.URL, json=evento("pousada-rosa", TOKEN_ROSA, msg_id="R1")).status_code == 200
    assert clientes[TOKEN_ROSA].enviadas == [(HOSPEDE, "Oi, aqui é a Pousada Rosa.")]


def test_parar_e_audio_saem_pela_instancia_da_pousada():
    c, _, clientes = app(chave_openai="sk-teste", transcricao="tem café da manhã?")
    c.post(base.URL, json=evento("pousada-mar", TOKEN_MAR, "", msg_id="AU1", tipo="AudioMessage"))
    assert clientes[TOKEN_MAR].transcritos == [("AU1", "sk-teste")]   # o áudio só existe na instância que recebeu
    c.post(base.URL, json=evento("pousada-mar", TOKEN_MAR, "PARAR", msg_id="P1"))
    assert [t for _, t in clientes[TOKEN_MAR].enviadas][-1] == base.CONFIRMACAO_SAIDA
    assert enviadas(clientes, TOKEN_ENV) == []


# ---------------------------------------------------------------------
# Eco (fromMe) por instância
# ---------------------------------------------------------------------
def test_eco_e_reconhecido_por_instancia():
    c, repo, _ = app()
    c.post(base.URL, json=evento("pousada-rosa", TOKEN_ROSA, msg_id="A1"))          # o bot da rosa responde
    c.post(base.URL, json=envio_do_celular("pousada-rosa", TOKEN_ROSA, msg_id="E1"))  # eco da rosa: não pausa
    assert repo.pausas == []
    # o dono do mar escreve pelo celular para o mesmo hóspede: não é eco (outra instância), pausa só no mar
    assert c.post(base.URL, json=envio_do_celular("pousada-mar", TOKEN_MAR, msg_id="E2")).json()["dono"] is True
    assert repo.pausas == [("po-mar", HOSPEDE)]


def test_envio_do_celular_com_token_de_outra_pousada_e_recusado():
    c, repo, _ = app()
    assert c.post(base.URL, json=envio_do_celular("pousada-mar", TOKEN_ROSA)).status_code == 401
    assert repo.pausas == []


# ---------------------------------------------------------------------
# Rotinas: relatório mensal e monitor de conexão por instância
# ---------------------------------------------------------------------
def test_relatorio_mensal_sai_pelo_numero_de_cada_pousada():
    c, _, clientes = app(rotinas=True)
    c.app.state.agendador.get_job("relatorio_mensal").func()
    assert [n for n, _ in clientes[TOKEN_ROSA].enviadas] == [DONO_ROSA]
    assert [n for n, _ in clientes[TOKEN_MAR].enviadas] == [DONO_MAR]
    assert [n for n, _ in clientes[TOKEN_ENV].enviadas] == [DONO_SOL]   # sem token próprio: instância do ambiente


def test_monitor_confere_cada_instancia_e_diz_qual_pousada_caiu(monkeypatch):
    import whatsapp.monitor as monitor

    alertas = []
    titulos = []
    monkeypatch.setattr(monitor, "enviar_alerta",
                        lambda url, texto, *a, **k: (alertas.append(texto), titulos.append(k.get("titulo"))))
    c, repo, clientes = app(rotinas=True)
    clientes[TOKEN_ENV].conectado = True
    c.app.state.agendador.get_job("monitor_whatsapp").func()
    clientes[TOKEN_MAR].conectado = False
    c.app.state.agendador.get_job("monitor_whatsapp").func()
    assert len(alertas) == 1 and alertas[0].startswith("⚠️") and alertas[0].endswith("Pousada: Pousada do Mar.")
    assert "(Recepção 24h)" in alertas[0] and titulos == ["Recepção 24h"]   # nome do produto (feat/pousadas-noite)
    whatsapp = c.get("/saude").json()["whatsapp"]
    assert set(whatsapp) == {"rosa", "mar", "sol"}
    assert whatsapp["mar"]["conectado"] is False and whatsapp["rosa"]["conectado"] is True
    # evento de conexão da instância confere só ela
    clientes[TOKEN_MAR].conectado = True
    assert c.post(base.URL, json={"EventType": "connection", "instanceName": "pousada-mar",
                                  "token": TOKEN_MAR}).json()["conexao"] is True
    assert c.get("/saude").json()["whatsapp"]["mar"]["conectado"] is True
    assert alertas[-1].startswith("✅") and "Pousada do Mar" in alertas[-1]


def test_pousadas_sem_token_dividem_um_monitor_so(monkeypatch):
    c, repo, clientes = app(rotinas=True)
    repo.pousadas["pousada-lua"] = Pousada(id="po-lua", slug="lua", nome="Pousada da Lua", dono="Bia",
                                           telefone_aviso=None, instancia="pousada-lua")
    consultas = []
    clientes[TOKEN_ENV].status = lambda: consultas.append(1) or {"conectado": True, "estado": "connected"}
    c.app.state.agendador.get_job("monitor_whatsapp").func()
    assert len(consultas) == 1 and set(c.get("/saude").json()["whatsapp"]) == {"rosa", "mar", "sol"}


def test_relatorio_previa_nao_depende_da_instancia():
    c, repo, _ = app()
    repo.pousada_por_slug = lambda slug: {"id": "po-mar", "slug": "mar", "nome": "Pousada do Mar",
                                          "telefone_aviso": DONO_MAR}
    assert c.get(f"/relatorio/{SEGREDO}/mar?mes={date(2026, 9, 1):%Y-%m}").text.startswith("📊 *Pousada do Mar")
