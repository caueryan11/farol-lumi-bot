"""Avisos que o assistente manda para o WhatsApp do dono da pousada. Todos no mesmo formato, para ler em 5 segundos:

    🛎️ *Pedido de reserva · Maria*       <- o que é + quem (1ª linha já diz se precisa parar o que está fazendo)
    📅 10/01 a 12/01/2027 · 2 noite(s)   <- o que aconteceu
    👉 Confirme ...: wa.me/55...          <- o que fazer, com o link que abre a conversa

Cores: 🛎️ pedido de reserva pronto para confirmar · 🟡 hóspede precisa do dono (responder quando puder) ·
⏸️ bot pausado porque o dono respondeu pelo celular (só na 1ª pausa de cada conversa).
"""

from __future__ import annotations

import re

LIMITE_RESUMO = 220


def link_whatsapp(telefone: str) -> str:
    """wa.me/<só dígitos>: tocando, abre a conversa com o hóspede."""
    return "wa.me/" + re.sub(r"\D", "", telefone or "")


def curto(texto: str, limite: int = LIMITE_RESUMO) -> str:
    """Uma linha só, sem espaços sobrando, cortada com reticências."""
    t = re.sub(r"\s+", " ", (texto or "").strip())
    if len(t) > limite:
        t = t[: limite - 1].rstrip(" ,.;:") + "…"
    if t and t[-1] not in ".!?…\"”":
        t += "."
    return t[:1].upper() + t[1:]


def _cabecalho(icone: str, titulo: str, nome: str) -> str:
    return f"{icone} *{titulo} · {nome}*"


# --- avisos para o dono --------------------------------------------------------------
NOMES_IDIOMA = {"pt": "português", "es": "espanhol", "en": "inglês"}


def _data_br(iso: str, com_ano: bool = False) -> str:
    a, m, d = iso.split("-")
    return f"{d}/{m}/{a}" if com_ano else f"{d}/{m}"


def pedido_reserva(pedido: dict, telefone: str, idioma: str = "pt") -> str:
    """Pedido de reserva pronto para o dono confirmar (o bot nunca confirma)."""
    idades = pedido.get("criancas_idades") or []
    pessoas = f"{pedido['adultos']} adulto(s)"
    if idades:
        pessoas += f" + {len(idades)} criança(s) ({', '.join(f'{i} anos' for i in idades)})"
    linhas = [f"🛎️ *Pedido de reserva · {pedido['nome']}*",
              f"📅 {_data_br(pedido['entrada'])} a {_data_br(pedido['saida'], True)} · {pedido['noites']} noite(s)",
              f"👥 {pessoas}"]
    extras = [f"escreve em {NOMES_IDIOMA.get(idioma, idioma)}"] if idioma and idioma != "pt" else []
    if pedido.get("acomodacao"):
        extras.append(f"prefere {pedido['acomodacao']}")
    if extras:
        linhas.append("🌐 " + " · ".join(extras))
    if pedido.get("observacoes"):
        linhas.append(f"📝 {curto(pedido['observacoes'], 160)}")
    if pedido.get("contato"):
        linhas.append(f"☎️ Outro contato: {pedido['contato']}")
    if pedido.get("grupo_grande"):
        linhas.append("⚠️ Grupo grande: combine os detalhes direto com o hóspede.")
    linhas.append(f"👉 Confirme a disponibilidade e mande o sinal: {link_whatsapp(telefone)}")
    return "\n".join(linhas)


def bot_pausado(nome: str | None, telefone: str, horas: int) -> str:
    """O dono respondeu um hóspede pelo celular da pousada e o bot saiu da conversa (só na 1ª pausa, não a cada
    renovação). Se não foi o dono, foi a saudação/ausência automática do WhatsApp Business: ela também pausa o bot."""
    quem = nome or "este hóspede"
    return "\n".join([
        f"⏸️ *Atendimento virtual pausado · {quem}*",
        f"Você respondeu pelo WhatsApp da pousada, então o bot fica quieto com {quem} por {horas} h.",
        "Para a Lumi voltar antes, mande *#lumi* nessa conversa.",
        "Se não foi você (saudação ou mensagem de ausência do WhatsApp Business), desligue-as: elas pausam o bot.",
        f"👉 Conversa: {link_whatsapp(telefone)}"])


def hospede_precisa(nome: str | None, telefone: str, resumo: str, idioma: str = "pt", rotulo: str = "hóspede") -> str:
    quem = f"{nome} ({rotulo})" if nome else rotulo
    linhas = [_cabecalho("🟡", "Responder", quem), curto(resumo)]
    if idioma and idioma != "pt":
        linhas.append(f"🌐 Escreve em {NOMES_IDIOMA.get(idioma, idioma)}")
    linhas.append(f"👉 Responda: {link_whatsapp(telefone)}")
    return "\n".join(linhas)


def agenda(evento: dict, nome: str | None, telefone: str) -> str:
    """Negócio de serviços (bot/negocio.py): o bot marcou, remarcou ou cancelou na agenda do Google. Informativo: o
    horário já está (ou já saiu) da agenda do profissional; o dono só age se quiser."""
    quem = evento.get("nome") or nome or "cliente"
    servico = " com ".join(p for p in (evento.get("servico"), evento.get("profissional")) if p)
    if evento["tipo"] == "marcado":
        linhas = [f"📅 *Horário marcado · {quem}*", f"✂️ {servico}", f"🕐 {evento['descricao']}"]
    elif evento["tipo"] == "remarcado":
        linhas = [f"🔁 *Horário remarcado · {quem}*", f"✂️ {servico}", f"🕐 De {evento['de']} para {evento['para']}"]
    else:
        linhas = [f"❌ *Horário cancelado · {quem}*", f"✂️ {servico}", f"🕐 Era {evento['descricao']} (já saiu da agenda)"]
    linhas.append(f"👉 Conversa: {link_whatsapp(telefone)}")
    return "\n".join(linhas)
