"""Peças comuns do webhook: telefone, histórico, PARAR, formatação, uazapi, monitor e alerta. Sem internet."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from bot import avisos
from bot.assistente import formatar_whatsapp
from whatsapp import consentimento
from whatsapp.app import Config, _historico_para_api, _Vistos, criar_app_do_ambiente
from whatsapp.monitor import MonitorWhatsApp, enviar_alerta
from whatsapp.telefone import variantes_telefone
from whatsapp.uazapi import ClienteUazapi


def test_variantes_telefone_com_e_sem_nono_digito():
    v = variantes_telefone("5511999990001")
    assert "551199990001" in v and "+5511999990001" in v
    assert "5511999990001" in variantes_telefone("551199990001")


def test_historico_comeca_em_user_e_nao_termina_em_user():
    h = _historico_para_api([("saida", "x"), ("entrada", "a"), ("entrada", "b"), ("saida", "c"), ("entrada", "d")])
    assert h == [{"role": "user", "content": "a\nb"}, {"role": "assistant", "content": "c"}]


def test_vistos_descarta_repetido_e_esquece_os_mais_antigos():
    v = _Vistos(maximo=2)
    assert v.novo("a") and not v.novo("a")
    assert v.novo("b") and v.novo("c") and v.novo("a")  # "a" saiu da memória


@pytest.mark.parametrize("texto", ["PARAR", "parar.", "Sair", "stop", "Revogo meu consentimento", "não quero mais receber mensagens"])
def test_eh_revogacao(texto):
    assert consentimento.eh_revogacao(texto)


@pytest.mark.parametrize("texto", ["posso sair às 10h?", "parar o carro onde?", "cancelar a reserva", "aceito", ""])
def test_nao_e_revogacao(texto):
    assert not consentimento.eh_revogacao(texto)


def test_formatar_whatsapp():
    assert formatar_whatsapp("**Total: 2 noites**\n## Dica\nok") == "*Total: 2 noites*\nDica\nok"


def test_resumo_longo_e_cortado_em_uma_linha():
    t = avisos.curto("linha 1\n\nlinha 2 " + "x" * 400)
    assert "\n" not in t and len(t) <= avisos.LIMITE_RESUMO and t.endswith("…")


def test_config_do_ambiente_e_negocio(monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)   # não lê o .env local
    for nome in ("URL_PUBLICA", "NEGOCIO", "ROTINAS"):
        monkeypatch.delenv(nome, raising=False)
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "lumi.up.railway.app")
    c = Config.do_ambiente()
    assert c.negocio == "pousada" and c.rotinas is True and c.url_publica == "https://lumi.up.railway.app"
    monkeypatch.setenv("NEGOCIO", "nutri")
    with pytest.raises(ValueError, match="NEGOCIO"):
        criar_app_do_ambiente()


def _uazapi(resposta):
    def tratar(req):
        assert req.url.path == "/instance/status" and req.headers["token"] == "tok"
        if isinstance(resposta, Exception):
            raise resposta
        return resposta
    return ClienteUazapi("https://x.uazapi.com", "tok", http=httpx.Client(transport=httpx.MockTransport(tratar)))


def test_status_da_uazapi():
    ok = httpx.Response(200, json={"instance": {"status": "connected"}, "status": {"connected": True}})
    assert _uazapi(ok).status() == {"conectado": True, "estado": "connected"}
    fora = httpx.Response(200, json={"instance": {"status": "disconnected"}, "status": {"connected": False}})
    assert _uazapi(fora).status() == {"conectado": False, "estado": "disconnected"}
    assert "token inválido" in _uazapi(httpx.Response(401, json={"error": "Invalid token"})).status()["estado"]
    assert "sem resposta" in _uazapi(httpx.ConnectError("x")).status()["estado"]


def test_monitor_avisa_quando_cai_repete_e_quando_volta():
    estados = iter([True, False, False, False, True, True])
    agora = [datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc)]
    alertas = []
    m = MonitorWhatsApp(lambda: (lambda c: {"conectado": c, "estado": "connected" if c else "disconnected"})(next(estados)),
                        alertas.append, agora=lambda: agora[0])
    m.conferir()                                   # conectado: sem alerta
    assert alertas == [] and m.resumo()["conectado"] is True
    agora[0] += timedelta(minutes=10); m.conferir()  # caiu
    assert len(alertas) == 1 and "fora do ar" in alertas[0]
    agora[0] += timedelta(minutes=10); m.conferir()  # continua fora: não repete ainda
    assert len(alertas) == 1
    agora[0] += timedelta(hours=6); m.conferir()     # 6 h depois: repete
    assert len(alertas) == 2
    agora[0] += timedelta(minutes=10); m.conferir()  # voltou
    assert "reconectado" in alertas[-1] and m.resumo()["desde"] == agora[0].isoformat()
    agora[0] += timedelta(minutes=10); m.conferir()
    assert len(alertas) == 3


def test_monitor_ja_comeca_fora_e_alerta_com_erro_nao_quebra():
    def falha(texto):
        raise RuntimeError("ntfy fora")
    m = MonitorWhatsApp(lambda: {"conectado": False, "estado": "token inválido"}, falha)
    assert m.conferir()["conectado"] is False       # não levanta


@pytest.mark.parametrize("url, esperado", [
    ("https://discord.com/api/webhooks/1/abc", {"content": "oi"}),
    ("https://hooks.slack.com/services/T/B/X", {"text": "oi"}),
])
def test_formatos_de_alerta(url, esperado):
    import json

    recebidos = []
    enviar_alerta(url, "oi", http=httpx.Client(transport=httpx.MockTransport(lambda req: recebidos.append(req) or httpx.Response(200))))
    (req,) = recebidos
    assert json.loads(req.content) == esperado


def test_alerta_ntfy_queda_urgente_titulo_com_acento_e_produto_da_pousada():
    """O título "Recepção 24h" não cabe em cabeçalho HTTP (só ASCII): vai como parâmetro da URL."""
    recebidos = []
    http = httpx.Client(transport=httpx.MockTransport(lambda req: recebidos.append(req) or httpx.Response(200)))
    m = MonitorWhatsApp(lambda: {"conectado": False, "estado": "disconnected"},
                        lambda texto: enviar_alerta("https://ntfy.sh/recepcao-x7k2?x=1", texto, http=http,
                                                    titulo="Recepção 24h"),
                        produto="Recepção 24h")
    m.conferir()
    (req,) = recebidos
    assert req.url.params["title"] == "Recepção 24h" and req.url.params["x"] == "1"
    assert req.headers["priority"] == "urgent" and req.headers["tags"] == "warning"
    assert req.content.decode().startswith("⚠️ WhatsApp fora do ar (Recepção 24h)")


def test_sem_url_de_alerta_nao_envia():
    enviar_alerta("", "oi", http=None)  # não tenta rede
