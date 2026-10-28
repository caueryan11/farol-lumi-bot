"""Casos do QA das pousadas (ideias-2026-10-02/10-pousadas-qa.md e 19-revisao-pousadas.md, 02/10/2026), trazidos do
branch feat/pousadas-salvaguardas do nutri-assistente e adaptados ao código da Lumi. Sem internet: IA, uazapi e banco
simulados. `corrigido` marca os casos que falhavam na Lumi antes deste porte."""

import json
import re
from pathlib import Path

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from bot import pousada as bot_pousada
from bot.pousada import ResultadoPousada, atender_hospede, checar_mensagem, texto_faq, texto_guia, validar_pedido
from testes.test_pousada import (AGORA, DONO, HOSPEDE, INFO, SEGREDO, TOKEN, URL, Api, Cliente, Repo, _resp,
                                 _resposta, evento)
from whatsapp.app import Config
from whatsapp.pousada_app import CONFIRMACAO_SAIDA, criar_app_pousada

RAIZ = Path(__file__).resolve().parents[1]
PEDIDO = {"entrada": "2027-01-10", "saida": "2027-01-15", "noites": 5, "adultos": 2, "criancas_idades": [],
          "nome": "Martín Gómez", "contato": "", "acomodacao": "", "observacoes": ""}
REGISTRO = ("registrar_pedido_reserva", {"entrada": "2027-01-10", "saida": "2027-01-15", "adultos": 2,
                                         "criancas_idades": [], "nome": "Martín Gómez", "contato": "",
                                         "acomodacao": "", "observacoes": ""})


class RepoComPausa(Repo):
    """Repo simulado com a pausa do dono (o Postgres faz isso com pausado_ate; aqui é um booleano)."""

    def __init__(self):
        super().__init__()
        self.pausas, self.retomadas = [], []

    def pausar_hospede(self, pousada_id, telefone, horas):
        self.pausas.append((telefone, horas))
        nova, self.h.pausado = not self.h.pausado, True
        return nova

    def retomar_hospede(self, pousada_id, telefone):
        self.retomadas.append(telefone)
        self.h.pausado = False

    def pousadas_ativas(self):
        return [self.pousada]


def _app(resultado=None, cliente=None, repo=None):
    repo, cliente, chamadas = repo or RepoComPausa(), cliente or Cliente(), []

    def atender(texto, info, **kw):
        chamadas.append(texto)
        return resultado or ResultadoPousada(acao="responder", mensagem="Hola!", idioma="es")

    a = criar_app_pousada(Config("https://x.uazapi.com", TOKEN, SEGREDO, rotinas=False, negocio="pousada"),
                          cliente=cliente, repo=repo, atender_fn=atender)
    return TestClient(a), cliente, repo, chamadas


# --------------------------------------------------------------------- desconto / fora do FAQ / ressalva
def test_desconto_em_espanhol_e_encaminhado_e_dono_recebe_resumo_em_pt():
    api = Api(_resp(_resposta("¡Gracias, Martín! Ya se lo pasé a Carla, que te responde en breve.", acao="encaminhar",
                              idioma="es", resumo="Pede desconto para 7 noites em janeiro.")))
    r = atender_hospede("Hola! ¿Me hacés un descuento si me quedo 7 noches?", INFO, cliente=api, agora=AGORA)
    assert r.acao == "encaminhar" and r.idioma == "es" and r.pedido is None
    c, cliente, _, _ = _app(r)
    c.post(URL, json=evento("¿Me hacés un descuento?"))
    assert cliente.enviadas[0] == (HOSPEDE, r.mensagem)
    assert cliente.enviadas[1][0] == DONO and "Pede desconto" in cliente.enviadas[1][1]
    assert "espanhol" in cliente.enviadas[1][1]


@pytest.mark.parametrize("texto", [
    "Te hago un descuento del 10%.",            # corrigido: "del" escapava do regex
    "Faço por R$ 400 em vez de R$ 500.",         # corrigido
    "Posso fazer um preço especial para você.",  # corrigido
    "I can give you 10 percent off.",            # corrigido
])
def test_trava_barra_desconto_em_outras_formas(texto):
    assert checar_mensagem(texto) is not None


def test_pergunta_fora_do_faq_encaminha_com_resumo():
    api = Api(_resp(_resposta("Essa eu não sei te dizer, mas já passei para a Carla; a resposta chega em breve.",
                              acao="encaminhar", resumo="Pergunta se a pousada faz transfer do aeroporto de Floripa.")))
    r = atender_hospede("Vocês fazem transfer do aeroporto?", INFO, cliente=api, agora=AGORA)
    assert r.acao == "encaminhar" and "transfer" in r.resumo_para_dono


def test_resposta_com_valor_ganha_a_ressalva():  # corrigido: ressalva só no prompt
    api = Api(_resp(_resposta("A suíte de casal custa R$ 280 por noite na baixa temporada.")))
    r = atender_hospede("quanto custa?", INFO, cliente=api, agora=AGORA)
    assert r.mensagem.rstrip().endswith("Valores confirmados pela pousada na reserva.")


def test_resposta_com_valor_em_ingles_ganha_ressalva_em_ingles():  # corrigido
    api = Api(_resp(_resposta("The double suite is R$ 280 per night.", idioma="en")))
    r = atender_hospede("How much per night?", INFO, cliente=api, agora=AGORA)
    assert r.mensagem.rstrip().endswith("Rates are confirmed by the property when booking.")


def test_resposta_com_valor_em_espanhol_ganha_ressalva_em_espanhol():  # corrigido
    api = Api(_resp(_resposta("La suite doble cuesta R$ 280 por noche.", idioma="es")))
    r = atender_hospede("¿Cuánto sale?", INFO, cliente=api, agora=AGORA)
    assert r.mensagem.rstrip().endswith("Valores confirmados por la posada al reservar.")


@pytest.mark.parametrize("texto", [
    "Your reservation has been confirmed.", "Reservado! Te esperamos dia 10.", "Temos vaga sim para essas datas!",
    "Tenemos disponibilidad para ustedes.", "Nosso Pix é carla@pousada.com", "Pix: 48999990000",
    "Pague em https://buy.stripe.com/abc",
])
def test_trava_barra_evasoes_de_confirmacao_e_cobranca(texto):  # corrigido (todas passavam pela trava)
    assert checar_mensagem(texto) is not None


@pytest.mark.parametrize("texto", [
    "A Carla te manda a chave Pix em seguida.", "A dona te manda a chave Pix em seguida.",
    "A chave Pix é enviada pela Carla depois da confirmação.",
    "Não confirmamos sua reserva por aqui: a Carla confirma.",
])
def test_trava_nao_barra_frases_que_o_proprio_prompt_pede(texto):
    assert checar_mensagem(texto) is None


def test_apresentacao_continua_a_regra_da_lumi():
    """Não portado de propósito: no nutri-assistente a 1ª resposta ganhava "Aqui é o atendimento virtual" em código.
    Na Lumi a regra é responder direto (sem se apresentar), mas nunca fingir ser pessoa: a resposta sai como veio."""
    api = Api(_resp(_resposta("Aceitamos pet de até 10 kg nos chalés.")))
    r = atender_hospede("aceitam pet?", INFO, historico=[], cliente=api, agora=AGORA)
    assert r.mensagem == "Aceitamos pet de até 10 kg nos chalés."
    prompt = api.chamadas[0]["system"][0]["text"]
    assert "Não se apresente" in prompt and "Nunca diga que é uma pessoa" in prompt


# --------------------------------------------------------------------- laço: ordem das ferramentas
def test_resposta_antes_do_registro_no_mesmo_turno_nao_perde_o_pedido():  # corrigido
    api = Api(_resp(_resposta("¡Listo! Pasé tu pedido a Carla.", idioma="es"), REGISTRO),
              _resp(_resposta("¡Listo! Pasé tu pedido a Carla.", idioma="es")))
    r = atender_hospede("del 10 al 15, Martín Gómez", INFO, cliente=api, agora=AGORA)
    assert r.pedido is not None and r.passos == 1


def test_registro_invalido_com_resposta_no_mesmo_turno_nao_diz_que_passou():  # corrigido
    invalido = (REGISTRO[0], {**REGISTRO[1], "nome": ""})
    api = Api(_resp(invalido, _resposta("¡Listo! Pasé tu pedido a Carla.", idioma="es")),
              _resp(_resposta("¿Me decís tu nombre?", idioma="es")))
    r = atender_hospede("del 10 al 15", INFO, cliente=api, agora=AGORA)
    assert r.pedido is None and "pasé tu pedido" not in r.mensagem.lower()
    resultados = api.chamadas[1]["messages"][-1]["content"]
    assert [x["tool_use_id"] for x in resultados] == ["t0", "t1"]   # na ordem em que o modelo chamou


def test_fallback_em_ingles_tem_texto_em_ingles():  # corrigido: as 3 línguas juntas, sem saber o idioma
    class Quebrada:
        class messages:
            @staticmethod
            def create(**kw):
                raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
    r = atender_hospede("Hi, do you have rooms in January?", INFO, hospede={"idioma": "en"}, cliente=Quebrada(),
                        agora=AGORA)
    assert r.mensagem.startswith("Thanks") and "Obrigado" not in r.mensagem and r.idioma == "en"


@pytest.mark.parametrize("dono", ["a equipe da pousada", "A equipe da pousada", "Equipe Natribu's"])
def test_dono_generico_nao_quebra_a_frase_em_espanhol(dono):  # corrigido: "Ya se lo pasé a A equipe da pousada"
    texto = bot_pousada.mensagem_encaminhamento(dono)
    assert not re.search(r"\ba a equipe", texto, re.I) and "pasé al equipo de la posada" in texto
    assert "equipe" not in bot_pousada.mensagem_encaminhamento(dono, "en")


@pytest.mark.parametrize("extra,deve_recusar", [
    ({"adultos": True}, True),                          # corrigido: bool passava como int
    ({"adultos": 2, "criancas_idades": ["4"]}, False),  # corrigido: idade "4" sumia calada
    ({"adultos": 2, "criancas_idades": ["quatro"]}, True),
])
def test_validar_pedido_tipos(extra, deve_recusar):
    ok, erro = validar_pedido({"entrada": "2027-01-10", "saida": "2027-01-12", "nome": "Ana", **extra}, AGORA.date())
    if deve_recusar:
        assert erro
    else:
        assert erro is None and ok["criancas_idades"] == [4]


def test_grupo_grande_fica_marcado_no_pedido():  # corrigido: regra de 8 pessoas só no prompt
    ok, _ = validar_pedido({"entrada": "2027-01-10", "saida": "2027-01-12", "adultos": 12, "nome": "Ana"}, AGORA.date())
    assert ok["grupo_grande"] is True
    from bot import avisos
    assert "Grupo grande" in avisos.pedido_reserva(ok, HOSPEDE)


def test_pedido_com_datas_absurdas_volta_para_o_modelo():
    hoje = AGORA.date()
    assert "noites" in validar_pedido({"entrada": "2027-01-10", "saida": "2027-06-10", "adultos": 2, "nome": "Ana"}, hoje)[1]
    assert "ano" in validar_pedido({"entrada": "2029-01-10", "saida": "2029-01-12", "adultos": 2, "nome": "Ana"}, hoje)[1]


# --------------------------------------------------------------------- webhook: mídia
def test_audio_nao_chama_ia_e_nao_avisa_dono():
    c, cliente, _, chamadas = _app()
    c.post(URL, json=evento("", tipo="AudioMessage", msg_id="A1"))
    assert chamadas == [] and cliente.enviadas == [(HOSPEDE, bot_pousada.MENSAGEM_NAO_TEXTO)]


def test_dois_audios_seguidos_nao_repete_o_pedido_de_texto():  # corrigido (mandava 2x e o dono não sabia)
    c, cliente, _, _ = _app()
    c.post(URL, json=evento("", tipo="AudioMessage", msg_id="A1"))
    c.post(URL, json=evento("", tipo="AudioMessage", msg_id="A2"))
    assert sum(1 for n, t in cliente.enviadas if n == HOSPEDE and t == bot_pousada.MENSAGEM_NAO_TEXTO) == 1
    assert any(n == DONO for n, _ in cliente.enviadas)


def test_pedido_de_texto_no_idioma_conhecido():  # corrigido: só PT e ES
    c, cliente, repo, _ = _app()
    repo.h.idioma = "en"
    c.post(URL, json=evento("", tipo="AudioMessage", msg_id="A1"))
    assert cliente.enviadas == [(HOSPEDE, bot_pousada.NAO_TEXTO["en"])]


def test_imagem_com_legenda_de_comprovante_avisa_o_dono():  # corrigido: pedia "por texto" e ninguém ficava sabendo
    c, cliente, repo, chamadas = _app()
    c.post(URL, json=evento("Segue o comprovante do sinal", tipo="ImageMessage", msg_id="I1"))
    ao_dono = [t for n, t in cliente.enviadas if n == DONO]
    assert chamadas == [] and len(ao_dono) == 1 and "comprovante do sinal" in ao_dono[0]
    assert repo.msgs[0][:2] == ("entrada", "[imagem] Segue o comprovante do sinal")


def test_documento_sem_legenda_tambem_vai_para_o_dono():
    c, cliente, _, _ = _app()
    c.post(URL, json=evento("", tipo="DocumentMessage", msg_id="D1"))
    assert any(n == DONO and "documento" in t for n, t in cliente.enviadas)


@pytest.mark.parametrize("tipo", ["ReactionMessage", "StickerMessage"])
def test_reacao_ou_figurinha_nao_gera_resposta(tipo):  # corrigido: pedia "por texto"
    c, cliente, _, chamadas = _app()
    c.post(URL, json=evento("", tipo=tipo, msg_id="R1"))
    assert cliente.enviadas == [] and chamadas == []


def test_video_pede_texto_e_nao_e_ignorado():
    c, cliente, _, chamadas = _app()
    c.post(URL, json=evento("", tipo="VideoMessage", msg_id="V1"))
    assert chamadas == [] and cliente.enviadas == [(HOSPEDE, bot_pousada.MENSAGEM_NAO_TEXTO)]


# --------------------------------------------------------------------- webhook: dono
def test_dono_escrevendo_do_numero_sem_o_9_tambem_e_ignorado():
    c, cliente, repo, chamadas = _app()
    c.post(URL, json=evento("oi", de="554899990000"))   # DONO 5548999990000 sem o 9
    assert cliente.enviadas == [] and chamadas == [] and repo.msgs == []


def test_dono_responde_pelo_numero_da_pousada_e_o_bot_pausa():
    c, cliente, _, chamadas = _app()
    dono_no_chat = evento("Oi Martín, aqui é a Carla! Tenho o chalé sim.", msg_id="D1")
    dono_no_chat["message"]["fromMe"] = True
    c.post(URL, json=dono_no_chat)
    c.post(URL, json=evento("Perfeito, Carla! Mando o sinal agora?", msg_id="H2"))
    assert chamadas == []  # o dono assumiu a conversa: o bot fica quieto por um tempo
    assert [n for n, _ in cliente.enviadas] == [DONO]   # e o dono recebe o aviso ⏸️ (1ª pausa)


# --------------------------------------------------------------------- webhook: falhas e idempotência
class _ClienteQueFalhaComHospede(Cliente):
    def enviar_texto(self, numero, texto, digitando_ms=1200):
        if numero == HOSPEDE:
            raise httpx.HTTPStatusError("500", request=httpx.Request("POST", "https://x"),
                                        response=httpx.Response(500))
        return super().enviar_texto(numero, texto, digitando_ms)


def test_uazapi_fora_ao_responder_nao_perde_o_pedido():  # corrigido: pedido sumia e o dono não sabia
    c, cliente, repo, _ = _app(ResultadoPousada(acao="responder", mensagem="¡Listo!", idioma="es", pedido=PEDIDO),
                               cliente=_ClienteQueFalhaComHospede())
    assert c.post(URL, json=evento("del 10 al 15, Martín Gómez")).status_code == 200
    ao_dono = [t for n, t in cliente.enviadas if n == DONO]
    assert repo.pedidos and len(ao_dono) == 2
    assert ao_dono[0].startswith("🛎️") and "não chegou ao hóspede" in ao_dono[1]


def test_banco_fora_ao_gravar_resposta_ainda_avisa_o_dono():
    repo = RepoComPausa()
    original = repo.registrar_mensagem

    def falha_na_saida(pousada_id, hospede_id, direcao, *a, **k):
        if direcao == "saida":
            raise RuntimeError("banco fora")
        return original(pousada_id, hospede_id, direcao, *a, **k)

    repo.registrar_mensagem = falha_na_saida
    c, cliente, _, _ = _app(ResultadoPousada(acao="responder", mensagem="¡Listo!", idioma="es", pedido=PEDIDO),
                            repo=repo)
    c.post(URL, json=evento("del 10 al 15, Martín Gómez"))
    assert repo.pedidos and any(n == DONO and t.startswith("🛎️") for n, t in cliente.enviadas)


def test_pousada_sem_telefone_aviso_sobe_mas_avisa_no_log_e_no_saude(caplog):  # revisão B2, adaptado
    """Na Lumi várias pousadas dividem o servidor: uma sem telefone_aviso não derruba as outras (no nutri-assistente o
    app recusava subir). Fica no log (ERROR) e no /saude, e o bot fica calado nela."""
    repo = RepoComPausa()
    repo.pousada.telefone_aviso = None
    repo.pousada.slug = "sem-aviso"
    c, _, _, _ = _app(repo=repo)
    assert "sem telefone_aviso" in caplog.text and "sem-aviso" in caplog.text
    assert c.get("/saude").json()["sem_telefone_aviso"] == ["sem-aviso"]


def test_aviso_apagado_depois_da_subida_cala_o_bot_em_vez_de_prometer():  # revisão B2
    c, cliente, repo, chamadas = _app(ResultadoPousada(acao="encaminhar", mensagem="Carla ya lo recibió", idioma="es",
                                                       resumo_para_dono="r", pedido=PEDIDO))
    repo.pousada.telefone_aviso = None
    c.post(URL, json=evento("del 10 al 15"))
    assert cliente.enviadas == [] and chamadas == []      # ninguém ouve "já passei" sem ninguém avisado


def test_fallback_nao_apaga_idioma_conhecido():  # corrigido: gravava 'pt' por cima de 'es'
    c, _, repo, _ = _app(ResultadoPousada(acao="encaminhar", mensagem="x", resumo_para_dono="r", fallback=True))
    repo.h.idioma = "es"
    c.post(URL, json=evento("hola"))
    assert repo.h.idioma == "es"


def test_evento_antigo_reenviado_depois_do_restart_nao_reativa_quem_mandou_parar():  # corrigido
    c, _, repo, chamadas = _app()
    repo.ids.add("ANTIGO")          # já está no banco; o app reiniciou e perdeu a memória de vistos
    repo.h.saiu = True
    c.post(URL, json=evento("mensagem antiga", msg_id="ANTIGO"))
    assert repo.h.saiu is True and chamadas == []


@pytest.mark.parametrize("texto", ["basta", "no quiero más mensajes", "unsubscribe", "Stop messaging me"])
def test_parar_em_espanhol_e_ingles(texto):  # corrigido
    c, cliente, repo, chamadas = _app()
    c.post(URL, json=evento(texto, msg_id="S1"))
    assert repo.h.saiu is True and chamadas == [] and cliente.enviadas == [(HOSPEDE, CONFIRMACAO_SAIDA)]


def test_confirmacao_de_parar_no_idioma_conhecido():  # corrigido: só PT e ES
    c, cliente, repo, _ = _app()
    repo.h.idioma = "en"
    c.post(URL, json=evento("unsubscribe", msg_id="S1"))
    assert cliente.enviadas == [(HOSPEDE, "All right, I won't message you anymore. If you want to talk to the "
                                          "property again, just write.")]


def test_segredo_errado_404_sem_token_401_json_ruim_400():
    c, cliente, _, _ = _app()
    assert c.post("/webhook/uazapi/segredo-errado-0000000", json=evento("oi")).status_code == 404
    sem_token = evento("oi", msg_id="T1")
    del sem_token["token"]
    assert c.post(URL, json=sem_token).status_code == 401
    assert c.post(URL, content=b"nao e json", headers={"content-type": "application/json"}).status_code == 400
    assert cliente.enviadas == []


def test_mensagem_gigante_e_cortada_antes_da_ia():  # corrigido: texto inteiro ia para a IA (custo)
    c, _, _, chamadas = _app()
    c.post(URL, json=evento("a" * 20000, msg_id="G1"))
    assert len(chamadas[0]) <= 2000


# --------------------------------------------------------------------- FAQs (só leitura)
FAQS = [p for p in sorted((RAIZ / "pousadas").glob("*.json"))
        if isinstance(json.loads(p.read_text(encoding="utf-8")), dict)
        and "dono" in json.loads(p.read_text(encoding="utf-8"))]


@pytest.mark.parametrize("arquivo", FAQS, ids=lambda p: p.name)
def test_faq_estrutura_e_trava_nao_barra_o_proprio_faq(arquivo):
    d = json.loads(arquivo.read_text(encoding="utf-8"))
    for campo in ("slug", "nome", "dono", "faq"):
        assert d.get(campo), f"falta {campo}"
    faq = d["faq"]
    ids = [f["id"] for f in faq if f.get("tipo") != "guia"]
    assert len(ids) == len(set(ids))
    permitidos = "\n".join([texto_faq(faq), texto_guia(faq), d.get("reserva_url") or ""])
    for f in faq:
        if f.get("tipo") in ("guia", "temporadas"):
            continue
        for idioma in ("pt", "es", "en"):   # en é opcional, mas quando existe a trava também não pode barrar
            r = (f.get(idioma) or {}).get("r")
            if idioma == "en" and not f.get("en"):
                continue
            assert r and r.strip(), f"{f['id']} sem {idioma}"
            motivo = checar_mensagem(r, bot_pousada.descontos_do_faq(faq), idioma=idioma, permitidos=permitidos,
                                     distancias_do_centro=bot_pousada.distancias_do_centro_do_faq(faq))
            assert motivo is None, f"a trava barra a própria resposta do FAQ: {f['id']} ({idioma}): {motivo}"
