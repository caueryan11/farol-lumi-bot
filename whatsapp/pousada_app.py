"""Webhook da Lumi (NEGOCIO=pousada): WhatsApp pela uazapi, segredo no caminho da URL e token da instância no corpo,
com o fluxo do hóspede. Banco: o projeto Supabase das pousadas (supabase_pousadas/).

    hóspede escreve  -> bot/pousada.py responde pelo FAQ (PT/ES/EN)
    hóspede manda áudio -> transcrito pela uazapi/OpenAI (OPENAI_API_KEY) e tratado como texto; sem chave, pede texto
                     -> pedido de reserva completo: aviso 🛎️ pronto para o dono confirmar
                     -> desconto, reclamação, reserva existente, fora do FAQ: aviso 🟡 para o dono
    hóspede manda imagem/documento (comprovante do sinal) -> encaminhado ao dono (aviso 🟡); reação/figurinha: ignorada
    dono escreve do número de aviso -> ignorado (o dono responde o hóspede direto pelo link do aviso)
    dono responde o hóspede pelo celular da pousada -> bot quieto nesse chat por PAUSA_DONO_HORAS (padrão 3 h); na
                     1ª pausa o dono recebe o aviso ⏸️; "#lumi" do dono na conversa reativa na hora
    "PARAR" (PT/ES/EN) -> confirma e marca o hóspede; voltar a escrever reativa

Pedido que não se perde: o pedido é gravado antes de responder; falha da uazapi ao responder o hóspede avisa o dono;
pousada sem telefone_aviso (ninguém seria avisado) fica calada em vez de dizer "já passei para a Carla".

Várias pousadas no mesmo servidor: cada uma tem a própria instância da uazapi (número e token), todas com o webhook
apontando para a mesma URL. O `instanceName` do evento identifica a pousada (pousadas.instancia), o `token` do corpo
é conferido contra o token DESSA pousada (pousadas.uazapi_token, 007_multi_instancia.sql) e tudo o que sai (resposta,
avisos ao dono, relatório) vai pela instância dela. Pousada sem token cadastrado usa UAZAPI_TOKEN/UAZAPI_URL do
ambiente: é o modo de uma pousada só, como antes da migração.
"""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from typing import Callable

import re
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from starlette.concurrency import run_in_threadpool

from bot import avisos, lugares, negocio, relatorio_pousada
from bot.pousada import (MENSAGEM_NAO_TEXTO, NAO_TEXTO, PousadaInfo, ResultadoPousada, atender_hospede,
                         mensagem_encaminhamento, no_idioma, nome_do_dono)
from whatsapp import consentimento
from whatsapp.app import PAUSA_DONO_HORAS_PADRAO, PAUSA_MAX_HORAS, PAUSA_MIN_HORAS, Config, _historico_para_api, _Vistos
from whatsapp.pousada_repo import RETENCAO_PADRAO_DIAS, Pousada, RepositorioPousada
from whatsapp.telefone import variantes_telefone
from whatsapp.uazapi import ClienteUazapi, MensagemRecebida, extrair_envio_do_celular, extrair_mensagem, token_valido

log = logging.getLogger("pousada")

MAPAS = Path(__file__).resolve().parents[1] / "pousadas" / "mapas"   # gerados por scripts/gerar_mapa.py
FUSO = "America/Sao_Paulo"
PRODUTO = "Recepção 24h"      # nome no alerta de queda do WhatsApp (whatsapp/monitor.py)
PAUSA_DONO_HORAS = PAUSA_DONO_HORAS_PADRAO   # o dono respondeu pelo celular: bot quieto nesse chat (PAUSA_DONO_HORAS)
JANELA_ENVIO_BOT_S = 30    # envio do celular até 30 s depois de uma resposta do bot é eco do próprio bot
# "digitando..." antes de responder o hóspede: proporcional ao texto, com variação, para parecer gente e proteger o
# número (a IA já leva uns 3 a 8 s). Avisos para o dono saem na hora.
DIGITANDO_MIN_MS, DIGITANDO_MAX_MS, DIGITANDO_MS_POR_CARACTERE = 2000, 6000, 18


def tempo_digitando(texto: str, sorteio: float | None = None) -> int:
    import random

    base = min(DIGITANDO_MAX_MS, max(DIGITANDO_MIN_MS, 1200 + DIGITANDO_MS_POR_CARACTERE * len(texto or "")))
    fator = sorteio if sorteio is not None else random.uniform(0.85, 1.15)
    return int(min(DIGITANDO_MAX_MS, max(DIGITANDO_MIN_MS, base * fator)))


SAIDA = {
    "pt": "Tudo bem, não vou mais te mandar mensagens. Se quiser falar com a pousada de novo, é só escrever.",
    "es": "Listo, no te voy a escribir más. Si querés hablar con la posada de nuevo, solo escribí.",
    "en": "All right, I won't message you anymore. If you want to talk to the property again, just write.",
}
CONFIRMACAO_SAIDA = "\n".join(SAIDA.values())   # idioma do hóspede desconhecido
# PARAR além de consentimento._REVOGACOES (PT e "stop"), para hóspede em espanhol ou inglês
REVOGACOES_EXTRA = {"basta", "no quiero mas mensajes", "no quiero recibir mas mensajes", "no me escribas mas",
                    "dejar de recibir mensajes", "darme de baja", "baja", "unsubscribe", "stop messages",
                    "stop messaging me", "no more messages"}
MAX_CARACTERES = 2000     # texto maior é cortado antes da IA (custo)
MIDIAS_PARA_O_DONO = {"imagem", "documento"}   # comprovante do sinal, foto, PDF: vão para o dono
ROTULO_MIDIA = {"imagem": "[imagem]", "documento": "[documento]", "video": "[vídeo]"}   # como fica no histórico
REATIVAR_BOT = re.compile(r"#\s*(?:lumi|bot)\W*", re.IGNORECASE)   # o dono manda na conversa: o bot volta antes do fim da pausa


def eh_parar(texto: str) -> bool:
    return consentimento.eh_revogacao(texto) or consentimento.normalizar(texto) in REVOGACOES_EXTRA


def aplicar_prazo_de_guarda(repo: RepositorioPousada, padrao_dias: int = RETENCAO_PADRAO_DIAS) -> dict | None:
    """Job diário (3h): apaga conversas e hóspedes que passaram do prazo de guarda (LGPD). Falha só vai para o log."""
    try:
        apagados = repo.aplicar_prazo_de_guarda(padrao_dias)
    except Exception:
        log.exception("falha ao aplicar o prazo de guarda das conversas")
        return None
    log.info("prazo de guarda: apagadas %d mensagem(ns), %d pedido(s) de reserva e %d hóspede(s)",
             apagados["mensagens"], apagados["pedidos"], apagados["hospedes"])
    return apagados


def _campo(pousada: Pousada | dict, nome: str) -> str:
    """Campo da pousada, venha como Pousada ou como linha do banco (dict)."""
    valor = pousada.get(nome) if isinstance(pousada, dict) else getattr(pousada, nome, "")
    return str(valor or "").strip()


class Instancias:
    """Um cliente da uazapi por instância (= por pousada), criado na primeira vez e guardado.

    Token e URL vêm da pousada (pousadas.uazapi_token/uazapi_url); vazios, valem UAZAPI_TOKEN/UAZAPI_URL do
    ambiente. Pousadas sem token próprio dividem o cliente do ambiente (é a mesma instância)."""

    def __init__(self, config: Config, padrao=None, fabrica: Callable[[str, str], object] = ClienteUazapi):
        self._config = config
        self._fabrica = fabrica
        self._clientes: dict[tuple[str, str], object] = {}
        if padrao is not None:   # cliente do ambiente já pronto (testes)
            self._clientes[(config.uazapi_url, config.uazapi_token)] = padrao

    def chave(self, pousada: Pousada | dict) -> tuple[str, str]:
        return (_campo(pousada, "uazapi_url") or self._config.uazapi_url,
                _campo(pousada, "uazapi_token") or self._config.uazapi_token)

    def token(self, pousada: Pousada | dict) -> str:
        """Token esperado no corpo dos webhooks desta pousada ("" = nenhum: tudo é recusado)."""
        return self.chave(pousada)[1]

    def cliente(self, pousada: Pousada | dict):
        chave = self.chave(pousada)
        if chave not in self._clientes:
            if not chave[1]:
                raise ValueError(f"pousada {_campo(pousada, 'slug')} sem token da uazapi (nem UAZAPI_TOKEN no ambiente)")
            self._clientes[chave] = self._fabrica(*chave)
        return self._clientes[chave]


def criar_app_pousada(config: Config, *, cliente=None, repo: RepositorioPousada | None = None,
                      atender_fn: Callable[..., ResultadoPousada] = atender_hospede,
                      fabrica_cliente: Callable[[str, str], object] = ClienteUazapi,
                      atender_negocio_fn: Callable[..., negocio.ResultadoNegocio] = negocio.atender_cliente,
                      calendario=None) -> FastAPI:
    """cliente: o da instância do ambiente (UAZAPI_TOKEN); fabrica_cliente(url, token): o das pousadas com token
    próprio; calendario: Google Calendar dos negócios de serviços (padrão: GOOGLE_SERVICE_ACCOUNT_B64). Esses
    parâmetros existem para os testes; em produção ficam no padrão."""
    if len(config.webhook_segredo) < 16:
        raise ValueError("WEBHOOK_SEGREDO precisa ter 16+ caracteres (vai no caminho da URL)")
    if not config.uazapi_token:
        log.warning("UAZAPI_TOKEN vazio: só pousadas com uazapi_token cadastrado no banco são atendidas")
    instancias = Instancias(config, cliente, fabrica_cliente)
    pausa_horas = config.pausa_dono_horas
    if not PAUSA_MIN_HORAS <= pausa_horas <= PAUSA_MAX_HORAS:   # Config montada à mão: mesmo limite do ambiente
        log.warning("pausa_dono_horas=%r fora da faixa %d a %d; usando %d", pausa_horas, PAUSA_MIN_HORAS,
                    PAUSA_MAX_HORAS, PAUSA_DONO_HORAS_PADRAO)
        pausa_horas = PAUSA_DONO_HORAS_PADRAO
    if repo is None:
        if not config.database_url:
            raise ValueError("NEGOCIO=pousada precisa de DATABASE_URL (projeto Supabase das pousadas)")
        from whatsapp.pousada_repo import RepositorioPousadaPostgres

        repo = RepositorioPousadaPostgres(config.database_url)
    if calendario is None and config.google_service_account_b64:
        from agenda.google_calendar import GoogleCalendar

        try:
            calendario = GoogleCalendar(config.google_service_account_b64)
        except Exception:
            log.exception("GOOGLE_SERVICE_ACCOUNT_B64 inválida: negócios com agenda vão anotar o pedido e passar ao dono")

    def pousadas_sem_aviso() -> list[str]:
        """Pousadas ativas sem WhatsApp do dono: o bot fica calado nelas (ninguém receberia pedido nem aviso)."""
        try:
            return sorted(p.slug for p in repo.pousadas_ativas() if not str(p.telefone_aviso or "").strip())
        except Exception:
            log.exception("não consegui conferir o telefone_aviso das pousadas")
            return []

    # não derruba a subida (as outras pousadas seguem no ar), mas fica no log e no /saude
    if sem_aviso := pousadas_sem_aviso():
        log.error("pousada(s) ativa(s) sem telefone_aviso, bot calado nelas: %s. Preencha pousadas.telefone_aviso "
                  "ou marque ativo = false.", ", ".join(sem_aviso))

    def relatorio(pousada: dict, hoje) -> str:
        inicio, fim = relatorio_pousada.mes_anterior(hoje)
        return relatorio_pousada.montar(pousada["nome"], inicio, repo.metricas_mes(pousada["id"], inicio, fim))

    def enviar_relatorios() -> None:
        """Dia 1º: o resumo do mês anterior no WhatsApp de cada dono."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        hoje = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
        for p in repo.pousadas_para_relatorio():
            try:  # pelo número da própria pousada
                instancias.cliente(p).enviar_texto(p["telefone_aviso"], relatorio(p, hoje))
                log.info("relatório mensal enviado: %s", p["slug"])
            except Exception:
                log.exception("falha no relatório mensal de %s", p["slug"])

    # um monitor de conexão por instância (pousadas sem token próprio dividem o da instância do ambiente)
    monitores: dict[tuple[str, str], tuple[str, object]] = {}   # chave da instância -> (slug, MonitorWhatsApp)

    def monitor_de(pousada: Pousada):
        chave = instancias.chave(pousada)
        if chave not in monitores:
            cli = instancias.cliente(pousada)
            if not hasattr(cli, "status"):
                return None
            from whatsapp.monitor import MonitorWhatsApp, enviar_alerta

            nome = pousada.nome   # o alerta diz qual pousada caiu (no fim, para não mudar o começo ⚠️/✅ do texto)
            monitores[chave] = (pousada.slug, MonitorWhatsApp(
                cli.status, lambda texto: enviar_alerta(config.alerta_url, f"{texto} Pousada: {nome}.", titulo=PRODUTO),
                produto=PRODUTO))
        return monitores[chave][1]

    def conferir_conexao(pousada: Pousada) -> None:
        try:
            monitor = monitor_de(pousada)
            if monitor is not None:
                monitor.conferir()
        except Exception:
            log.exception("falha ao conferir a conexão do WhatsApp de %s", pousada.slug)

    def conferir_conexoes() -> None:
        """A cada 10 min: a conexão de cada instância (uma vez só para pousadas que dividem a mesma)."""
        try:
            pousadas = repo.pousadas_ativas()
        except Exception:
            log.exception("falha ao listar as pousadas para o monitor do WhatsApp")
            return
        conferidas = set()
        for p in pousadas:
            if instancias.chave(p) not in conferidas:
                conferidas.add(instancias.chave(p))
                conferir_conexao(p)

    agendador = None
    if not config.rotinas:
        log.warning("ROTINAS=off: prazo de guarda (LGPD), relatório mensal e monitor do WhatsApp NÃO vão rodar")
    if config.rotinas:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        agendador = AsyncIOScheduler(timezone="America/Sao_Paulo")
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from apscheduler.triggers.cron import CronTrigger

        # fuso explícito: sem ele o CronTrigger usa o fuso do servidor (UTC no Railway), não o do agendador
        agendador.add_job(enviar_relatorios, CronTrigger(day=1, hour=9, minute=0, timezone=FUSO), id="relatorio_mensal",
                          name="Relatório do mês para os donos (dia 1º, 9h)", misfire_grace_time=6 * 3600)
        agendador.add_job(aplicar_prazo_de_guarda, CronTrigger(hour=3, minute=0, timezone=FUSO), args=[repo], id="prazo_de_guarda",
                          name="Prazo de guarda: apaga conversas antigas (3h)", misfire_grace_time=6 * 3600)
        agendador.add_job(conferir_conexoes, IntervalTrigger(minutes=10), id="monitor_whatsapp",
                          next_run_time=datetime.now(ZoneInfo("America/Sao_Paulo")),
                          name="Monitor do WhatsApp (10 min)")

    @asynccontextmanager
    async def ciclo_de_vida(_app):
        if agendador is not None:
            agendador.start()
        yield
        if agendador is not None:
            agendador.shutdown(wait=False)

    app = FastAPI(title="recepcionista de pousada", docs_url=None, redoc_url=None, lifespan=ciclo_de_vida)
    app.state.agendador = agendador
    vistos = _Vistos()

    @app.get("/saude")
    def saude():
        return {"ok": True, "negocio": "pousada", "versao": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "local")[:7],
                # por pousada (slug); vazio até a primeira conferência
                "whatsapp": {slug: m.resumo() for slug, m in monitores.values()}, "alerta": bool(config.alerta_url),
                # para conferir depois do deploy que o prazo de guarda e o relatório estão agendados
                "rotinas": sorted(j.id for j in agendador.get_jobs()) if agendador is not None else [],
                "transcricao": bool(config.openai_api_key),
                "pausa_dono_horas": pausa_horas,
                "sem_telefone_aviso": pousadas_sem_aviso(),   # bot calado nelas: corrigir o cadastro
                "lugares": bool(config.google_places_key),
                "agenda": calendario is not None,
                "mapa_google": bool(config.google_maps_browser_key)}

    @app.get("/relatorio/{segredo}/{slug}", include_in_schema=False)
    def previa_relatorio(segredo: str, slug: str, mes: str = ""):
        """Prévia do relatório (texto, não envia nada). mes=AAAA-MM; vazio = mês passado. Protegido pelo segredo."""
        from datetime import date, datetime
        from zoneinfo import ZoneInfo

        from fastapi.responses import PlainTextResponse

        if not hmac.compare_digest(segredo.encode(), config.webhook_segredo.encode()):
            raise HTTPException(status_code=404)
        pousada = repo.pousada_por_slug(slug)
        if pousada is None:
            raise HTTPException(status_code=404)
        hoje = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
        if mes:
            try:
                a, m = (int(x) for x in mes.split("-"))
                hoje = date(a + (m == 12), m % 12 + 1, 1)  # o relatório de `mes` é o "mês passado" deste dia
            except ValueError:
                raise HTTPException(status_code=400, detail="mes no formato AAAA-MM")
        return PlainTextResponse(relatorio(pousada, hoje))

    @app.get("/mapa/{slug}", include_in_schema=False)
    def mapa(slug: str):
        arquivo = MAPAS / f"{slug}.html"
        if not re.fullmatch(r"[a-z0-9-]{1,60}", slug) or not arquivo.is_file():
            raise HTTPException(status_code=404)
        google = MAPAS / f"{slug}.google.html"
        if config.google_maps_browser_key and google.is_file():  # mapa do Google com busca ao vivo (chave restrita ao domínio)
            return HTMLResponse(google.read_text(encoding="utf-8").replace("{{CHAVE_MAPS}}", config.google_maps_browser_key))
        return FileResponse(arquivo, media_type="text/html; charset=utf-8")

    def link_mapa(slug: str) -> str:
        return f"{config.url_publica}/mapa/{slug}" if slug and config.url_publica and (MAPAS / f"{slug}.html").is_file() else ""

    @app.post("/webhook/uazapi/{segredo}")
    async def webhook(segredo: str, request: Request, tarefas: BackgroundTasks):
        if not hmac.compare_digest(segredo.encode(), config.webhook_segredo.encode()):
            raise HTTPException(status_code=404)
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="json inválido")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=401)
        # a instância diz de qual pousada é o evento; o token tem de ser o DESSA pousada
        instancia = str(payload.get("instanceName") or "")
        try:
            pousada = await run_in_threadpool(repo.pousada_por_instancia, instancia) if instancia else None
        except Exception:
            log.exception("falha ao buscar a pousada da instância %r", instancia[:60])
            raise HTTPException(status_code=503)
        if pousada is None:
            log.warning("webhook de instância sem pousada ativa recusado: %r", instancia[:60])
            raise HTTPException(status_code=401)
        if not token_valido(payload, instancias.token(pousada)):   # compare_digest; o token nunca vai para o log
            log.warning("webhook com token inválido recusado (instância %r)", instancia[:60])
            raise HTTPException(status_code=401)
        if payload.get("EventType") == "connection":
            tarefas.add_task(conferir_conexao, pousada)
            return {"ok": True, "conexao": True}
        envio = extrair_envio_do_celular(payload)
        if envio is not None:
            texto_dono = str((payload.get("message") or {}).get("text") or "")
            tarefas.add_task(dono_assumiu, pousada, envio[1], texto_dono)
            return {"ok": True, "dono": True}
        msg = extrair_mensagem(payload)
        if msg is None:
            return {"ok": True, "ignorado": True}
        if not vistos.novo(msg.id):
            return {"ok": True, "repetido": True}
        tarefas.add_task(processar, pousada, msg)
        return {"ok": True}

    # (instância, telefone) -> hora do último envio do bot, para não pausar com o próprio eco. Por instância: o eco
    # chega pelo webhook da instância que enviou; o mesmo hóspede falando com outra pousada não conta.
    ultimo_envio: dict[tuple[str, str], float] = {}

    def enviar(pousada: Pousada, numero: str, texto: str, digitando: bool = True) -> str | None:
        """Tudo o que o bot manda sai pela instância (número) da pousada."""
        import time

        ultimo_envio[(pousada.instancia, numero)] = time.monotonic()
        return instancias.cliente(pousada).enviar_texto(numero, texto,
                                                        digitando_ms=tempo_digitando(texto) if digitando else 0)

    def enviar_seguro(pousada: Pousada, numero: str, texto: str, digitando: bool = True):
        """messageid (ou None) se enviou; False se a uazapi falhou: loga e alerta (ALERTA_URL) sem derrubar o resto
        (o pedido e os avisos ao dono seguem)."""
        try:
            return enviar(pousada, numero, texto, digitando)
        except Exception as e:
            log.exception("pousada %s: falha ao enviar mensagem pelo WhatsApp", pousada.slug)
            if config.alerta_url:
                try:
                    from whatsapp.monitor import enviar_alerta

                    enviar_alerta(config.alerta_url, f"Falha ao enviar mensagem pelo WhatsApp ({type(e).__name__}). "
                                                     f"Pousada: {pousada.nome}.", titulo=PRODUTO)
                except Exception:
                    log.exception("falha ao enviar o alerta")
            return False

    def dono_assumiu(pousada: Pousada, telefone: str, texto: str = "") -> None:
        import time

        try:
            if pousada.telefone_aviso and set(variantes_telefone(telefone)) & set(variantes_telefone(pousada.telefone_aviso)):
                return
            if REATIVAR_BOT.fullmatch(texto.strip()):   # antes do eco: o bot nunca envia "#lumi"
                repo.retomar_hospede(pousada.id, telefone)
                log.info("pousada: dono mandou #lumi; bot de volta nesse chat")
                return
            # eco da resposta do bot que a uazapi não marcou como enviada pela API. Por instância, e com e sem o 9 (e
            # com "+"): o número do eco pode vir num formato diferente do que o bot usou para enviar
            if any(time.monotonic() - ultimo_envio.get((pousada.instancia, t), -1e9) < JANELA_ENVIO_BOT_S
                   for t in variantes_telefone(telefone)):
                return
            nova = repo.pausar_hospede(pousada.id, telefone, pausa_horas)
            log.info("pousada: dono respondeu pelo celular; bot pausado %sh nesse chat (pausa nova: %s)", pausa_horas, nova)
        except Exception:
            log.exception("falha ao pausar o chat assumido pelo dono")
            return
        if nova and pousada.telefone_aviso:
            # só na 1ª pausa (não a cada renovação): se foi a saudação/ausência do WhatsApp Business, o dono percebe
            try:
                nome = repo.hospede(pousada.id, telefone).nome
            except Exception:
                nome = None
            enviar_seguro(pousada, pousada.telefone_aviso, avisos.bot_pausado(nome, telefone, pausa_horas),
                          digitando=False)

    def transcrever(pousada: Pousada, msg: MensagemRecebida) -> str:
        """Áudio -> texto pela uazapi + OpenAI. Sem chave ou com falha: "" (o hóspede recebe o pedido de texto).
        Pela instância da pousada: o áudio só existe na instância que o recebeu."""
        if not config.openai_api_key:
            return ""
        try:
            cli = instancias.cliente(pousada)
            if not hasattr(cli, "transcrever_audio"):
                return ""
            return cli.transcrever_audio(msg.id, config.openai_api_key)
        except Exception:
            log.exception("falha ao transcrever o áudio %s", msg.id)
            return ""

    def busca_de_lugares(pousada):
        """Consulta ao Google Maps a partir da pousada; None (só o guia) sem chave ou sem localização."""
        if not config.google_places_key or pousada.lat is None or pousada.lon is None:
            return None
        opinioes = lugares.opinioes_do_guia(pousada.faq)
        def buscar(categoria: str, idioma: str, termo: str = "") -> dict:
            texto = categoria == "outro" or (categoria not in lugares.CATEGORIAS and bool(termo))
            if texto:
                r = lugares.buscar_texto(config.google_places_key, pousada.lat, pousada.lon, termo or categoria,
                                         idioma=idioma, opinioes=opinioes)
            else:
                r = lugares.buscar(config.google_places_key, pousada.lat, pousada.lon, categoria, idioma=idioma,
                                   opinioes=opinioes)
            try:  # o que o hóspede procurou vai para o relatório mensal; falhar aqui não pode travar a resposta
                repo.registrar_busca(pousada.id, "outro" if texto else categoria, termo if texto else "", idioma,
                                     len(r.get("lugares") or []))
            except Exception:
                log.exception("falha ao registrar a busca")
            return r

        return buscar

    def processar(pousada: Pousada, msg: MensagemRecebida) -> None:
        try:
            processar_hospede(pousada, msg)
        except Exception:
            log.exception("falha ao processar mensagem %s", msg.id)

    def processar_hospede(pousada: Pousada, msg: MensagemRecebida) -> None:
        if pousada.telefone_aviso and set(variantes_telefone(msg.telefone)) & set(variantes_telefone(pousada.telefone_aviso)):
            log.info("mensagem do número do dono (msg=%s); sem resposta", msg.id)
            return
        if msg.contato_salvo and not pousada.responder_contatos_salvos:
            log.info("contato salvo na agenda (msg=%s); sem resposta", msg.id)
            return
        if not str(pousada.telefone_aviso or "").strip():
            # ninguém receberia o pedido nem o encaminhamento: calado em vez de dizer "já passei para a Carla"
            log.error("pousada %s sem telefone_aviso: mensagem %s não atendida", pousada.slug, msg.id)
            return
        if msg.tipo == "outro" and not msg.texto:
            return  # reação, figurinha, enquete: nada a responder
        hospede = repo.hospede(pousada.id, msg.telefone)
        texto = msg.texto if msg.tipo == "texto" else ""
        if msg.tipo == "audio":
            texto = transcrever(pousada, msg)
        if msg.tipo == "audio":
            registro = f"[áudio] {texto}" if texto else None
        elif msg.tipo in ROTULO_MIDIA:
            registro = f"{ROTULO_MIDIA[msg.tipo]} {msg.texto}".strip()
        else:
            registro = msg.texto or None
        # idempotência ANTES do PARAR: evento antigo reenviado depois de um restart não reativa quem saiu
        if not repo.registrar_mensagem(pousada.id, hospede.id, "entrada", msg.id, registro):
            return  # já processada
        if msg.tipo == "texto" and eh_parar(msg.texto):
            if not hospede.saiu:
                repo.atualizar_hospede(hospede.id, {"saiu": True})
                enviar_seguro(pousada, msg.telefone, no_idioma(SAIDA, hospede.idioma))
            return
        if hospede.saiu:
            repo.atualizar_hospede(hospede.id, {"saiu": False})  # voltou a escrever por conta própria
        if hospede.pausado:
            log.info("pousada: chat assumido pelo dono (hospede=%s); bot quieto", hospede.id)
            return

        if msg.tipo in MIDIAS_PARA_O_DONO or not texto:
            processar_midia(pousada, hospede, msg)
            return

        texto = texto[:MAX_CARACTERES]
        if pousada.tipo == "servicos":
            atender_negocio(pousada, hospede, msg, texto)
            return
        info = PousadaInfo(pousada.nome, pousada.dono, pousada.cidade, pousada.faq, link_mapa(pousada.slug),
                           pousada.reserva_url)
        pergunta = f"(mensagem de áudio, transcrita automaticamente; pode ter erros) {texto}" if msg.tipo == "audio" else texto
        r = atender_fn(pergunta, info, hospede={"nome": hospede.nome, "idioma": hospede.idioma},
                       historico=_historico_para_api(repo.historico(hospede.id, msg.id)),
                       buscar_lugares=busca_de_lugares(pousada))
        # 1) o pedido é gravado antes de tudo: falha no envio ou no banco depois daqui não o perde
        if r.pedido:
            try:
                repo.registrar_pedido(pousada.id, hospede.id, r.pedido, r.idioma)
            except Exception:
                log.exception("pousada %s: falha ao gravar o pedido de reserva (o aviso ao dono segue)", pousada.slug)
        # 2) responde o hóspede; se a uazapi falhar, os avisos ao dono seguem do mesmo jeito
        wa_id = enviar_seguro(pousada, msg.telefone, r.mensagem)
        try:
            if wa_id is not False:
                repo.registrar_mensagem(pousada.id, hospede.id, "saida", wa_id, r.mensagem, r.acao, r.motivo or None)
            campos = {} if r.fallback else {"idioma": r.idioma}   # fallback não sabe o idioma: não apaga o conhecido
            if r.pedido:
                campos["nome"] = r.pedido["nome"]
            if campos:
                repo.atualizar_hospede(hospede.id, campos)
        except Exception:
            log.exception("pousada %s: falha ao gravar a resposta (os avisos ao dono seguem)", pousada.slug)
        log.info("pousada: hospede=%s acao=%s idioma=%s pedido=%s passos=%d", hospede.id, r.acao, r.idioma,
                 bool(r.pedido), r.passos)

        # 3) avisos ao dono (cada envio protegido: um não derruba o outro)
        if r.pedido:
            enviar_seguro(pousada, pousada.telefone_aviso, avisos.pedido_reserva(r.pedido, msg.telefone, r.idioma),
                          digitando=False)
        elif r.acao == "encaminhar":
            enviar_seguro(pousada, pousada.telefone_aviso, avisos.hospede_precisa(hospede.nome, msg.telefone,
                                                                                  r.resumo_para_dono or r.motivo, r.idioma),
                          digitando=False)
        if wa_id is False:
            enviar_seguro(pousada, pousada.telefone_aviso, avisos.hospede_precisa(
                hospede.nome, msg.telefone, f"A resposta automática não chegou ao hóspede (falha no WhatsApp). "
                                            f"Ele escreveu: \"{texto[:160]}\"", r.idioma), digitando=False)

    def agenda_de(pousada: Pousada, hospede, telefone: str):
        """AgendaNegocio desta conversa, ou None (sem agenda_config, sem conta do Google ou config inválida: o bot
        anota o pedido de horário e passa para o dono)."""
        if not pousada.agenda_config or calendario is None:
            return None
        from agenda.servicos import AgendaNegocio, ler_config

        try:
            cfg = ler_config(pousada.agenda_config)
        except ValueError as e:
            log.error("pousada %s: agenda_config inválida (%s); bot sem agenda", pousada.slug, e)
            return None
        return AgendaNegocio(calendario, cfg, cliente_telefone=telefone,
                             gravar=lambda ag: repo.gravar_agendamento(pousada.id, hospede.id, ag),
                             listar=lambda: repo.agendamentos_do_cliente(pousada.id, telefone),
                             cancelar_registro=lambda ag_id: repo.cancelar_agendamento(pousada.id, ag_id), fuso=FUSO)

    def atender_negocio(pousada: Pousada, hospede, msg: MensagemRecebida, texto: str) -> None:
        """Salão, barbearia, clínica, corretor (pousadas.tipo = 'servicos'): base de conhecimento + agenda."""
        agenda = agenda_de(pousada, hospede, msg.telefone)
        cfg = pousada.agenda_config or {}
        info = negocio.NegocioInfo(
            pousada.nome, pousada.dono, pousada.cidade, pousada.faq,
            servicos=[{"nome": s["nome"], "duracao_min": s["duracao_min"]} for s in cfg.get("servicos") or []],
            profissionais=[{"id": p["id"], "nome": p["nome"],
                            "servicos": [s["nome"] for s in cfg.get("servicos") or [] if s["id"] in (p.get("servicos") or [])]}
                           for p in cfg.get("profissionais") or []])
        pergunta = f"(mensagem de áudio, transcrita automaticamente; pode ter erros) {texto}" if msg.tipo == "audio" else texto
        r = atender_negocio_fn(pergunta, info, cliente_conhecido={"nome": hospede.nome, "idioma": hospede.idioma},
                               historico=_historico_para_api(repo.historico(hospede.id, msg.id)), agenda=agenda)
        wa_id = enviar_seguro(pousada, msg.telefone, r.mensagem)
        try:
            if wa_id is not False:
                repo.registrar_mensagem(pousada.id, hospede.id, "saida", wa_id, r.mensagem, r.acao, r.motivo or None)
            campos = {} if r.fallback else {"idioma": r.idioma}
            if r.nome:
                campos["nome"] = r.nome
            if campos:
                repo.atualizar_hospede(hospede.id, campos)
        except Exception:
            log.exception("negócio %s: falha ao gravar a resposta (os avisos ao dono seguem)", pousada.slug)
        log.info("negocio: cliente=%s acao=%s idioma=%s agenda=%s passos=%d", hospede.id, r.acao, r.idioma,
                 [e["tipo"] for e in r.agenda], r.passos)
        # avisos ao dono: cada mudança na agenda e o encaminhamento (cada envio protegido)
        for evento in r.agenda:
            enviar_seguro(pousada, pousada.telefone_aviso, avisos.agenda(evento, r.nome or hospede.nome, msg.telefone),
                          digitando=False)
        if r.acao == "encaminhar":
            enviar_seguro(pousada, pousada.telefone_aviso, avisos.hospede_precisa(hospede.nome, msg.telefone,
                                                                                  r.resumo_para_dono or r.motivo, r.idioma,
                                                                                  rotulo="cliente"),
                          digitando=False)
        if wa_id is False:
            enviar_seguro(pousada, pousada.telefone_aviso, avisos.hospede_precisa(
                hospede.nome, msg.telefone, f"A resposta automática não chegou ao cliente (falha no WhatsApp). "
                                            f"Ele escreveu: \"{texto[:160]}\"", r.idioma, rotulo="cliente"),
                          digitando=False)

    def processar_midia(pousada: Pousada, hospede, msg: MensagemRecebida) -> None:
        """Imagem/documento (comprovante do sinal): direto para o dono. Áudio sem transcrição ou vídeo: pede texto
        uma vez; se repetir, passa para o dono (sem repetir o pedido de texto)."""
        pedidos_de_texto = set(NAO_TEXTO.values()) | {MENSAGEM_NAO_TEXTO}
        saidas = [c for d, c in repo.historico(hospede.id, msg.id) if d == "saida"]
        if msg.tipo not in MIDIAS_PARA_O_DONO and not (saidas and saidas[-1] in pedidos_de_texto):
            log.info("pousada: mensagem sem texto (tipo=%s, transcricao=%s); pede texto", msg.tipo,
                     bool(config.openai_api_key))
            texto = no_idioma(NAO_TEXTO, hospede.idioma)
            wa_id = enviar_seguro(pousada, msg.telefone, texto)
            if wa_id is not False:
                repo.registrar_mensagem(pousada.id, hospede.id, "saida", wa_id, texto, "responder", "mídia")
            return
        texto = mensagem_encaminhamento(nome_do_dono(pousada.dono), hospede.idioma)
        wa_id = enviar_seguro(pousada, msg.telefone, texto)
        if wa_id is not False:
            repo.registrar_mensagem(pousada.id, hospede.id, "saida", wa_id, texto, "encaminhar", "mídia")
        if msg.tipo in MIDIAS_PARA_O_DONO:
            resumo = f"Mandou {'uma imagem' if msg.tipo == 'imagem' else 'um documento'} (comprovante do sinal?)"
            resumo += f": \"{msg.texto[:160]}\"" if msg.texto else ". Veja na conversa."
        else:
            resumo = "Mandou áudio ou vídeo de novo e o atendimento virtual não conseguiu entender. Veja e responda."
        enviar_seguro(pousada, pousada.telefone_aviso, avisos.hospede_precisa(hospede.nome, msg.telefone, resumo,
                                                                             hospede.idioma or "pt"), digitando=False)

    return app
