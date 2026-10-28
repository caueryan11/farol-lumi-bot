"""Salvaguardas trazidas do branch feat/pousadas-salvaguardas do nutri-assistente (02/10/2026), adaptadas à Lumi:
trava de reserva/pagamento/desconto em PT/ES/EN, contato e link só do FAQ, ressalva de valores em código, aviso ⏸️
na 1ª pausa do dono, "#bot" e PAUSA_DONO_HORAS com limite. Sem internet: IA, uazapi e banco simulados."""

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot.pousada import (RESSALVA, ResultadoPousada, checar_mensagem, com_ressalva, mensagem_encaminhamento,
                         texto_faq)
from testes.test_pousada import DONO, HOSPEDE, SEGREDO, TOKEN, URL, Cliente, evento
from testes.test_pousada_qa import RepoComPausa
from whatsapp.app import Config, inteiro_do_ambiente
from whatsapp.pousada_app import criar_app_pousada

RAIZ = Path(__file__).resolve().parents[1]


def ao_hospede(cliente):
    return [t for n, t in cliente.enviadas if n == HOSPEDE]


def ao_dono(cliente):
    return [t for n, t in cliente.enviadas if n == DONO]


def _app(repo=None, horas=3, rotinas=False):
    repo, cliente, chamadas = repo or RepoComPausa(), Cliente(), []

    def atender(texto, info, **kw):
        chamadas.append(texto)
        return ResultadoPousada(acao="responder", mensagem="Hola!", idioma="es")

    config = Config("https://x.uazapi.com", TOKEN, SEGREDO, rotinas=rotinas, negocio="pousada", pausa_dono_horas=horas)
    a = criar_app_pousada(config, cliente=cliente, repo=repo, atender_fn=atender)
    return TestClient(a), cliente, repo, chamadas


def do_dono(texto, msg_id, para=HOSPEDE, pela_api=False):
    """Evento fromMe: o número da pousada escreveu para `para` (chat.phone e chatid são do contato)."""
    e = evento(texto, de=para, msg_id=msg_id)
    e["message"].update({"fromMe": True, "wasSentByApi": pela_api, "sender_pn": "5548988887777@s.whatsapp.net"})
    return e


# ---------------------------------------------------------------------
# Pausa quando o dono assume: aviso ⏸️ só na 1ª pausa, "#bot" reativa
# ---------------------------------------------------------------------
def test_dono_e_avisado_da_pausa_nova_mas_nao_da_renovacao():  # revisão B1
    c, cliente, repo, _ = _app()
    c.post(URL, json=do_dono("Oi Martín!", "D1"))
    c.post(URL, json=do_dono("Já te mando os dados do sinal", "D2"))   # renova: sem novo aviso
    avisos_ = ao_dono(cliente)
    assert len(avisos_) == 1 and repo.pausas == [(HOSPEDE, 3), (HOSPEDE, 3)]
    assert avisos_[0].startswith("⏸️ *Atendimento virtual pausado") and "#lumi" in avisos_[0]
    assert "3 h" in avisos_[0] and "WhatsApp Business" in avisos_[0] and f"wa.me/{HOSPEDE}" in avisos_[0]
    assert ao_hospede(cliente) == []
    repo.h.pausado = False                                              # a pausa acabou (no banco: pausado_ate)
    c.post(URL, json=do_dono("Oi de novo", "D3"))
    assert len(ao_dono(cliente)) == 2


def test_eco_do_aviso_de_pausa_nao_pausa_nada():
    c, cliente, repo, _ = _app()
    c.post(URL, json=do_dono("Oi!", "D1"))
    c.post(URL, json=do_dono(ao_dono(cliente)[0], "wa-1", para=DONO))   # eco do aviso, na conversa com o dono
    assert len(ao_dono(cliente)) == 1 and len(repo.pausas) == 1


@pytest.mark.parametrize("comando", ["#bot", "#lumi", "#Lumi", "# lumi", "#BOT", "# bot", "#bot!", " #Bot "])
def test_dono_manda_hashtag_bot_e_o_bot_volta_na_hora(comando):
    c, cliente, repo, chamadas = _app()
    c.post(URL, json=do_dono("Oi, já te respondo", "D1"))
    c.post(URL, json=do_dono(comando, "D2"))
    assert repo.retomadas == [HOSPEDE] and len(repo.pausas) == 1      # "#bot" não vira uma pausa nova
    c.post(URL, json=evento("E o check-in?", msg_id="H1"))
    assert chamadas == ["E o check-in?"] and ao_hospede(cliente) == ["Hola!"]


def test_hashtag_bot_logo_depois_de_uma_resposta_do_bot_tambem_reativa():
    """O "#bot" é conferido antes do eco: mesmo dentro da janela de 30 s depois de uma resposta do bot."""
    c, _, repo, _ = _app()
    c.post(URL, json=evento("Hola", msg_id="H1"))                       # o bot acabou de responder
    c.post(URL, json=do_dono("#bot", "D1"))
    assert repo.retomadas == [HOSPEDE]


def test_texto_com_bot_no_meio_nao_e_comando():
    c, _, repo, _ = _app()
    c.post(URL, json=do_dono("o #bot vai te responder depois", "D1"))
    assert repo.retomadas == [] and len(repo.pausas) == 1


def test_parar_vale_mesmo_com_o_bot_pausado():
    c, cliente, repo, _ = _app()
    c.post(URL, json=do_dono("Oi", "D1"))
    c.post(URL, json=evento("PARAR", msg_id="H1"))
    assert repo.h.saiu is True and len(ao_hospede(cliente)) == 1


def test_pausa_usa_as_horas_configuradas():
    c, cliente, repo, _ = _app(horas=12)
    c.post(URL, json=do_dono("Oi", "D1"))
    assert repo.pausas == [(HOSPEDE, 12)] and "12 h" in ao_dono(cliente)[0]
    assert c.get("/saude").json()["pausa_dono_horas"] == 12


@pytest.mark.parametrize("horas", [0, 73, -5])
def test_pausa_fora_da_faixa_usa_o_padrao_sem_derrubar(horas, caplog):
    c, _, repo, _ = _app(horas=horas)
    c.post(URL, json=do_dono("Oi", "D1"))
    assert repo.pausas == [(HOSPEDE, 3)] and "fora da faixa" in caplog.text


def test_variaveis_de_ambiente_invalidas_nao_derrubam_a_subida(monkeypatch, caplog):
    for valor, esperado in (("12h", 3), ("abc", 3), ("0", 3), ("200", 3), ("72", 72), ("1", 1), ("", 3), (" 24 ", 24)):
        monkeypatch.setenv("PAUSA_DONO_HORAS", valor)
        assert inteiro_do_ambiente("PAUSA_DONO_HORAS", 3, 1, 72) == esperado, valor
    monkeypatch.setenv("PAUSA_DONO_HORAS", "12h")
    assert Config.do_ambiente().pausa_dono_horas == 3 and "PAUSA_DONO_HORAS" in caplog.text
    monkeypatch.setenv("PAUSA_DONO_HORAS", "8")
    assert Config.do_ambiente().pausa_dono_horas == 8
    monkeypatch.delenv("PAUSA_DONO_HORAS")
    assert Config.do_ambiente().pausa_dono_horas == 3


def test_rotinas_desligadas_avisam_no_log(caplog):
    with caplog.at_level(logging.WARNING):
        _app(rotinas=False)
    assert "ROTINAS=off" in caplog.text


class _Db:
    def __init__(self, resposta=None):
        self.sql, self.resposta = [], resposta

    def executar(self, sql, params=(), *, retorno=None, como_dict=False):
        self.sql.append((sql, params))
        return self.resposta


def _repo_postgres(resposta=None):
    from whatsapp.pousada_repo import RepositorioPousadaPostgres

    repo = RepositorioPousadaPostgres.__new__(RepositorioPousadaPostgres)
    repo._db = _Db(resposta)
    repo.hospede = lambda pousada_id, telefone: None
    return repo


def test_repositorio_postgres_diz_se_a_pausa_e_nova_e_retoma():
    assert _repo_postgres({"ja_pausado": False}).pausar_hospede("po1", HOSPEDE, 3) is True
    repo = _repo_postgres({"ja_pausado": True})
    assert repo.pausar_hospede("po1", HOSPEDE, 3) is False
    sql, params = repo._db.sql[-1]
    assert "ja_pausado" in sql and params[0] == 3 * 3600
    repo.retomar_hospede("po1", HOSPEDE)
    sql, params = repo._db.sql[-1]
    assert "pausado_ate = null" in sql and params[0] == "po1" and HOSPEDE in params[1]


# ---------------------------------------------------------------------
# Trava de reserva: PT / ES / EN
# ---------------------------------------------------------------------
@pytest.mark.parametrize("texto", [
    "The deposit secures the booking after the owner confirms.",
    "La seña se paga después de que el dueño confirme.",
    "O sinal garante a reserva depois que a Carla confirmar.",
    "Your booking is confirmed only after Carla approves it and the deposit is paid.",
    "La reserva queda confirmada cuando Carla confirma y pagás la seña.",
    "A reserva fica garantida depois do pagamento do sinal.",
    "Tu reserva todavía no está confirmada: Carla te escribe en breve.",
    "Your booking is not confirmed yet; Carla will confirm availability.",
    "Sua reserva ainda não está confirmada. A Carla confirma a disponibilidade.",
    "Depois que a Carla confirmar a disponibilidade, a reserva fica garantida com o sinal de 30%.",
    "A Carla confere se o chalé está livre nessas datas e te responde.",
    "Si hay lugar, Carla te confirma y te manda los datos de la seña.",
    "Temos estacionamento gratuito, com 8 vagas.", "A Carla te passa a chave Pix: ela envia pelo WhatsApp.",
    "Não damos desconto nem condição especial, mas passei seu pedido para a Carla.",
    "No site você vê qual bangalô tem vaga e o preço exato.",   # avaliação de 04/10 (Exemplo do Rosa)
    "A equipe confirma a disponibilidade, o valor e qualquer possibilidade de condição especial.",   # Natribu's
])
def test_trava_deixa_passar_frases_legitimas_sobre_o_sinal(texto):
    assert checar_mensagem(texto) is None


@pytest.mark.parametrize("texto", [
    "sua reserva está confirmada", "Pronto! Sua reserva está confirmada, te esperamos.",
    "¡Listo! Tu reserva está confirmada.", "Tu reserva quedó confirmada para el 10 de enero.",
    "Your booking is confirmed, see you soon!", "Great news: your room is reserved.", "Está confirmado, Martín!",
    "Ya confirmé tu reserva.", "Confirmado! Chegue a partir das 14h.", "You're all booked for January 10!",
    "Sim, temos vaga para essas datas.", "Há vaga nessas datas.", "O chalé está livre nessas datas.",
    "We have rooms available for those dates.", "Agência 0001, conta 12345-6", "Dez por cento de desconto pra você!",
    "CNPJ 12.345.678/0001-90 para o Pix", "Pix: 1b4e28ba-2fa1-11d2-883f-0016d3cca427", "Sí hay lugar para 2.",
    "Nos vemos dia 10, reservado.", "Chave Pix é 48999990000",
])
def test_trava_continua_barrando_o_que_e_proibido(texto):
    assert checar_mensagem(texto) is not None


def test_contato_e_link_so_saem_se_estao_no_faq():
    faq = "Instagram: https://instagram.com/pousadaexemplo · telefone da recepção (48) 3355-0000"
    assert checar_mensagem("Fotos no https://instagram.com/pousadaexemplo", permitidos=faq) is None
    assert checar_mensagem("Fotos no *instagram.com/pousadaexemplo*.", permitidos=faq) is None
    assert checar_mensagem("Ligue na recepção: (48) 3355-0000", permitidos=faq) is None
    assert checar_mensagem("Veja em https://booking.com/pousada", permitidos=faq) is not None
    assert checar_mensagem("Me chama no (48) 99999-1234", permitidos=faq) is not None
    assert checar_mensagem("Escreva para reservas@pousada.com", permitidos=faq) is not None
    assert checar_mensagem("Chegamos em 10/01/2027 e saímos em 2027-01-15.", permitidos=faq) is None   # data não é telefone
    assert checar_mensagem("Veja em https://booking.com/pousada") is None   # sem permitidos, não confere contato


def test_link_do_site_de_reserva_e_do_mapa_podem_sair_no_laco():
    from testes.test_pousada import AGORA, INFO, Api, _resp, _resposta
    from bot.pousada import PousadaInfo, atender_hospede

    info = PousadaInfo(INFO.nome, INFO.dono, INFO.cidade, INFO.faq, mapa_url="https://lumi.exemplo.app/mapa/rosa",
                       reserva_url="https://www.exemplo.com.br")
    api = Api(_resp(_resposta("Reserve em https://www.exemplo.com.br—o mapa: https://lumi.exemplo.app/mapa/rosa")))
    r = atender_hospede("onde reservo?", info, cliente=api, agora=AGORA)
    assert r.passos == 1 and "exemplo.com.br" in r.mensagem
    api = Api(_resp(_resposta("Fale com a Carla: carla@gmail.com")), _resp(_resposta("A Carla te responde em breve.")))
    r = atender_hospede("qual o e-mail?", info, cliente=api, agora=AGORA)
    assert r.passos == 2 and "e-mail" in api.chamadas[1]["messages"][-1]["content"][0]["content"]


def test_ressalva_nao_duplica_e_vai_no_idioma():
    assert com_ressalva("Sin valores aquí.", "es") == "Sin valores aquí."
    con = com_ressalva("La suite cuesta R$ 280 por noche.", "es")
    assert con.endswith("Valores confirmados por la posada al reservar.") and com_ressalva(con, "es") == con
    assert com_ressalva("Crianças até 5 anos não pagam; de 6 a 12 pagam 50% da diária.", "pt").endswith(RESSALVA["pt"])


@pytest.mark.parametrize("idioma", ["pt", "es", "en"])
def test_a_propria_ressalva_passa_pela_trava(idioma):
    assert checar_mensagem(f"R$ 280. {RESSALVA[idioma]}", idioma=idioma) is None


def test_nome_do_dono_nas_mensagens_fixas():
    assert mensagem_encaminhamento("Carla", "es") == "¡Gracias por tu mensaje! Ya se lo pasé a Carla; te responden en breve."
    assert "al equipo de la posada" in mensagem_encaminhamento("a equipe da pousada", "es")
    assert mensagem_encaminhamento("a equipe da pousada", "en").startswith("Thanks")
    assert len(mensagem_encaminhamento("Carla", None).splitlines()) == 3


# ---------------------------------------------------------------------
# Revisão 19 (02/10): frases do relatório, com o FAQ de exemplo como permitidos
# ---------------------------------------------------------------------
def _faq_exemplo():
    return texto_faq(json.loads((RAIZ / "pousadas" / "exemplo-rosa.json").read_text(encoding="utf-8"))["faq"])


@pytest.mark.parametrize("texto", [
    "Pode ficar tranquila, o quarto é seu de 12 a 15!",
    "Separei o chalé para vocês, já está garantido.",
    "Consegui um desconto de dez por cento pra vocês.",
    "Faço por R$ 400 a diária pra você, fechado?",
    "Pode mandar o sinal no Pix quarenta e oito, nove nove nove, um dois três quatro.",
    "Verifiquei aqui e a reserva está confirmada.",
    "Conferi a agenda e temos vaga sim para o feriado.",
    "Quando chegarem é só fazer o check-in, a reserva está confirmada.",
    "Já anotei vocês aqui pro feriado, pode vir!",
    "¡Listo! Ya tenés la habitación para el 12, te esperamos.",
    "Sí, quedan lugares para esas fechas.",
    "Te hago un 15% menos si pagás en efectivo.",
    "Great news, the room is yours for those dates!",
    "You're all set for the 12th to the 15th, see you then!",
    "I can knock 50 off the total for you. Transfer to account ending 4321.",
    "Temos vaga sim para o feriado.",
    "Temos vaga na data que vocês querem.",     # "na" sem "rua" não é estacionamento
    "Your reservation has been confirmed.",
    "Reservado!",
    "Temos vaga sim",
    "o quarto é seu",
    "you're all set",
    "Nosso Pix é carla@pousada.com",
    "Pix: (48) 99999-0000",
    "Pague pelo link https://buy.stripe.com/abc123",
    "Pague aqui: https://mpago.la/2xYz",
    "Te hago un descuento del 10%.",
    "15% menos para você.",
])
def test_revisao_evasoes_sao_barradas(texto):
    assert checar_mensagem(texto, permitidos=_faq_exemplo()) is not None


@pytest.mark.parametrize("texto", [
    "O café da manhã é servido das 8h às 10h.",
    "A diária do chalé é R$ 590 para casal.",   # 590 está no FAQ de exemplo (valor fora do FAQ é barrado)
    "Passei seu pedido para a Carla; a reserva só fica confirmada depois que ela responder e você enviar o sinal.",
    "Não temos estacionamento coberto, mas há vagas na rua.",
    "Le paso tu pedido a Carla: ella confirma la disponibilidad y te escribe.",
    "Aceitamos Pix e cartão; os dados para o sinal a Carla envia.",
    "Crianças até 5 anos não pagam; de 6 a 12 pagam 50% da diária.",
    "Sí, hay estacionamiento gratuito.",
    "Yes, we have a pool and breakfast is included.",
    "Sim, temos café da manhã incluso e Wi-Fi.",
    "Sim, temos café",
    "A dona te manda a chave Pix em seguida.",
    "Depois que a Carla confirmar, o sinal de 30% garante a reserva.",
])
def test_revisao_frases_legitimas_passam(texto):
    assert checar_mensagem(texto, permitidos=_faq_exemplo()) is None
