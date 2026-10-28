"""Configuração e ponto de entrada da Lumi (recepcionista de pousada no WhatsApp, via uazapi).

Rodar:
    uvicorn whatsapp.app:criar_app_do_ambiente --factory --port 8000 --no-access-log

URL para cadastrar na uazapi (a mesma para todas as pousadas):  https://<seu-endereço>/webhook/uazapi/<WEBHOOK_SEGREDO>

O app em si está em whatsapp/pousada_app.py. Responde 200 na hora e processa em segundo plano (recomendação da
uazapi). Logs sem conteúdo de mensagem.
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI

log = logging.getLogger("whatsapp")

PAUSA_DONO_HORAS_PADRAO, PAUSA_MIN_HORAS, PAUSA_MAX_HORAS = 3, 1, 72   # o dono respondeu pelo celular: bot quieto


def inteiro_do_ambiente(nome: str, padrao: int, minimo: int, maximo: int) -> int:
    """Variável inteira com faixa. Ausente, não numérica ("12h") ou fora da faixa: usa o padrão e loga um aviso.
    Um erro de digitação numa variável não pode derrubar a subida (nem calar o bot por dias)."""
    bruto = (os.environ.get(nome) or "").strip()
    if not bruto:
        return padrao
    try:
        valor = int(bruto)
    except ValueError:
        log.warning("%s=%r não é um número inteiro; usando o padrão %d", nome, bruto, padrao)
        return padrao
    if not minimo <= valor <= maximo:
        log.warning("%s=%d fora da faixa %d a %d; usando o padrão %d", nome, valor, minimo, maximo, padrao)
        return padrao
    return valor


@dataclass
class Config:
    uazapi_url: str
    uazapi_token: str
    webhook_segredo: str
    database_url: str = ""
    rotinas: bool = False     # liga o agendador (relatório mensal, prazo de guarda, monitor do WhatsApp)
    url_publica: str = ""     # https://<domínio> do app, para montar o link do mapa
    alerta_url: str = ""      # ntfy/Discord/Slack: aviso quando o WhatsApp cai (whatsapp/monitor.py)
    negocio: str = "pousada"  # só existe o modo pousada neste repositório (NEGOCIO=pousada no Railway)
    google_places_key: str = ""  # Places API (New): lugares perto da pousada na hora (bot/lugares.py)
    google_maps_browser_key: str = ""  # mapa do Google na página /mapa (chave de navegador restrita ao domínio)
    openai_api_key: str = ""  # transcrição de áudio pela uazapi (Whisper); vazio = pede texto
    # conta de serviço do Google (JSON da chave em base64): agenda dos negócios de serviços (agenda/servicos.py)
    google_service_account_b64: str = field(default="", repr=False)
    pausa_dono_horas: int = PAUSA_DONO_HORAS_PADRAO  # PAUSA_DONO_HORAS (1 a 72): bot quieto no chat que o dono assumiu

    @classmethod
    def do_ambiente(cls) -> "Config":
        from dotenv import load_dotenv

        # override: o .env vence variável antiga do sistema (ex.: ANTHROPIC_API_KEY de outro projeto)
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)
        return cls(
            uazapi_url=os.environ.get("UAZAPI_URL", ""),
            uazapi_token=os.environ.get("UAZAPI_TOKEN", ""),
            webhook_segredo=os.environ.get("WEBHOOK_SEGREDO", ""),
            database_url=os.environ.get("DATABASE_URL", ""),
            rotinas=os.environ.get("ROTINAS", "on").lower() not in ("off", "0", "false", "nao", "não"),
            alerta_url=os.environ.get("ALERTA_URL", ""),
            negocio=os.environ.get("NEGOCIO", "pousada").lower(),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            google_service_account_b64=os.environ.get("GOOGLE_SERVICE_ACCOUNT_B64", ""),
            google_places_key=os.environ.get("GOOGLE_PLACES_KEY", ""),
            google_maps_browser_key=os.environ.get("GOOGLE_MAPS_BROWSER_KEY", ""),
            pausa_dono_horas=inteiro_do_ambiente("PAUSA_DONO_HORAS", PAUSA_DONO_HORAS_PADRAO, PAUSA_MIN_HORAS,
                                                 PAUSA_MAX_HORAS),
            # o Railway preenche RAILWAY_PUBLIC_DOMAIN com o domínio público do serviço
            url_publica=os.environ.get("URL_PUBLICA")
            or (f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}" if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else ""),
        )


class _Vistos:
    """Últimos ids recebidos, para descartar eventos repetidos antes de ir ao banco."""

    def __init__(self, maximo: int = 5000):
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._maximo = maximo

    def novo(self, id_: str) -> bool:
        if id_ in self._ids:
            return False
        self._ids[id_] = None
        if len(self._ids) > self._maximo:
            self._ids.popitem(last=False)
        return True


def _historico_para_api(linhas: list[tuple[str, str]]) -> list[dict]:
    """(direcao, texto) -> mensagens da API: começa em 'user' e alterna papéis."""
    msgs: list[dict] = []
    for direcao, texto in linhas:
        papel = "user" if direcao == "entrada" else "assistant"
        if not msgs and papel == "assistant":
            continue
        if msgs and msgs[-1]["role"] == papel:
            msgs[-1]["content"] += "\n" + texto
        else:
            msgs.append({"role": papel, "content": texto})
    if msgs and msgs[-1]["role"] == "user":  # ficou sem resposta; a pergunta atual entra como 'user' logo depois
        msgs.pop()
    return msgs


def criar_app_do_ambiente() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.do_ambiente()
    if config.negocio != "pousada":
        raise ValueError("NEGOCIO precisa ser 'pousada' (o modo nutri ficou no repositório nutri-assistente)")
    from whatsapp.pousada_app import criar_app_pousada

    return criar_app_pousada(config)
