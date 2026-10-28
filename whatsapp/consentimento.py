"""Saída do hóspede (LGPD): "PARAR" e equivalentes fazem o bot parar de responder aquele número."""

from __future__ import annotations

import re
import unicodedata

# Mensagem inteira que vale como pedido de saída (normalizada). Só a mensagem sozinha: "sair às 10h" não conta.
_REVOGACOES = {
    "parar", "pare", "sair", "stop", "revogar", "revogo", "descadastrar", "me descadastra", "me descadastre",
    "revogar consentimento", "revogo o consentimento", "revogo meu consentimento", "cancelar consentimento",
    "nao quero mais receber mensagens", "parar mensagens", "parar de receber mensagens",
}


def normalizar(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)  # tira pontuação e emoji
    return re.sub(r"\s+", " ", t).strip()


def eh_revogacao(texto: str) -> bool:
    return normalizar(texto) in _REVOGACOES
