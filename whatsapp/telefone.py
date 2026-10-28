"""Telefones do WhatsApp: o mesmo celular brasileiro chega em formatos diferentes."""

from __future__ import annotations


def variantes_telefone(telefone: str) -> list[str]:
    """Celular brasileiro pode chegar com ou sem o 9 (ex.: 5511999990001 / 551199990001)."""
    v = {telefone}
    if telefone.startswith("55") and len(telefone) == 13 and telefone[4] == "9":
        v.add(telefone[:4] + telefone[5:])
    if telefone.startswith("55") and len(telefone) == 12:
        v.add(telefone[:4] + "9" + telefone[4:])
    return sorted(v | {"+" + t for t in v})
