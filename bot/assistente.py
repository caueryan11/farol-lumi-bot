"""Constantes do laço da IA (Claude) e a formatação das respostas para o WhatsApp."""

from __future__ import annotations

import re

MODELO_PADRAO = "claude-haiku-4-5"   # MODELO_BOT no ambiente troca
FUSO_PADRAO = "America/Sao_Paulo"
MAX_PASSOS = 6                       # chamadas de ferramenta por mensagem antes de encaminhar ao dono
MAX_TOKENS = 1024


def formatar_whatsapp(texto: str) -> str:
    """Markdown -> WhatsApp: **negrito** vira *negrito*; tira # de títulos."""
    texto = re.sub(r"\*\*(.+?)\*\*", r"*\1*", texto)
    texto = re.sub(r"^#{1,6}\s*", "", texto, flags=re.M)
    return texto
