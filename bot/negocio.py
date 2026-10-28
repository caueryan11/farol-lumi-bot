"""Lumi para negócios de serviços (salão, barbearia, clínica de estética, corretor): pousadas.tipo = 'servicos'.

Mesmo laço da recepcionista das pousadas (Claude + ferramentas + enviar_resposta), com outro papel: responder
clientes 24 h em PT/ES/EN pela base de conhecimento do negócio (o FAQ) e, quando o negócio tem agenda conectada,
ver horários livres de verdade e marcar, remarcar e cancelar na agenda do Google do profissional
(agenda/servicos.py). Sem agenda conectada, anota o pedido de horário e passa para o dono.

Travas em código (além do prompt): Pix/dados bancários, desconto fora do FAQ, contato ou link fora do FAQ, prazo
de resposta, dia da semana errado e, principalmente, dizer que o horário está marcado sem a agenda ter marcado.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

import anthropic

from bot.assistente import FUSO_PADRAO, MAX_TOKENS, MODELO_PADRAO, formatar_whatsapp
from bot.pousada import (_DADO_PAGAMENTO, _FIM_DE_FRASE, IDIOMAS, NOMES_DIAS, PORTUGUES, PRAZO, _checar_contato,
                         _checar_desconto, _sem_acento, calendario, conferir_dias_da_semana, descontos_do_faq,
                         nome_do_dono, texto_faq)

log = logging.getLogger(__name__)

MAX_PASSOS = 8   # remarcar: meus_horarios + ver_horarios_livres + remarcar_horario + enviar_resposta (com folga)

ENCAMINHAMENTO = {
    "pt": "Obrigado pela mensagem! Já passei para {dono}; a resposta chega em breve.",
    "es": "¡Gracias por tu mensaje! Ya se lo pasé a {dono}; te responden en breve.",
    "en": "Thanks for your message! I've passed it to {dono}; you'll hear back soon.",
}


def mensagem_encaminhamento(dono: str, idioma: str | None = None) -> str:
    if idioma in ENCAMINHAMENTO:
        return ENCAMINHAMENTO[idioma].format(dono=dono)
    return "\n".join(t.format(dono=dono) for t in ENCAMINHAMENTO.values())


@dataclass
class NegocioInfo:
    nome: str
    dono: str
    cidade: str = ""
    faq: list[dict] = field(default_factory=list)   # base de conhecimento: [{id, pt: {p, r}, es: {p, r}}]
    servicos: list[dict] = field(default_factory=list)       # [{nome, duracao_min}] da agenda (vazio = sem agenda)
    profissionais: list[dict] = field(default_factory=list)  # [{nome, servicos: [nomes]}]


INSTRUCOES = """# Atendimento virtual de {negocio}

Você é o atendimento virtual de {negocio} ({cidade}) no WhatsApp. Conversa com clientes e interessados.
Você não é {dono} (quem responde pelo negócio) e nunca se passa por ninguém da equipe.

## Idioma
- Responda SEMPRE no idioma da última mensagem do cliente: português, espanhol ou inglês. Informe em `idioma`.
- Em espanhol ou inglês, nenhuma palavra em português. Nomes próprios (do negócio, de serviços, de pessoas) ficam
  como estão.

## Apresentação
- Não se apresente: responda direto, como o WhatsApp do negócio (um "Oi!" basta na primeira resposta).
- Nunca diga que é uma pessoa, que é {dono} ou que é da equipe. Se perguntarem diretamente se é robô ou pessoa,
  responda com sinceridade, em uma frase, que é o atendimento virtual (automático) de {negocio} e que {dono} entra
  na conversa quando precisa.

## O que você pode dizer
- Use SÓ a base de conhecimento abaixo e o que as ferramentas devolvem. Nunca invente preço, serviço, regra,
  endereço, prazo ou horário. O que a base não diz, você não afirma nem nega: diga que não tem essa informação e
  encaminhe para {dono}.
- Você NÃO cobra (nunca envie chave Pix, dados bancários ou link de pagamento) e NÃO dá desconto nem condição
  especial. Se a base fala de sinal ou pagamento, explique como funciona e diga que {dono} envia os dados.
- Datas: dia da semana só pelo calendário no fim destas instruções; nunca calcule de cabeça.

{secao_agenda}

## Encaminhe para {dono} (acao='encaminhar')
- Desconto, negociação, condição especial, orçamento fora da tabela.
- Reclamação ou problema com um atendimento.
- Pergunta que a base de conhecimento não responde.
- Pedido que só {dono} decide (ex.: encaixe fora do horário, serviço que não está na lista).
Ao encaminhar, diga ao cliente que passou para {dono} e que a resposta chega em breve, sem prometer prazo nem
resultado. Em `resumo_para_dono`, UMA frase em português com o que a pessoa quer.

## Estilo
- Curto (2 a 4 frases), acolhedor e direto, no tom da base de conhecimento. Use o primeiro nome quando souber.
- Formatação do WhatsApp: negrito com *um asterisco*; sem títulos nem listas longas; no máximo um emoji.
- Toda resposta termina chamando `enviar_resposta`.

## Base de conhecimento de {negocio}
{faq}
"""

SECAO_COM_AGENDA = """## Agenda (você marca de verdade)
Você vê a agenda real de cada profissional e pode marcar, remarcar e cancelar.
Serviços: {servicos}
Profissionais: {profissionais}
- Para marcar: descubra o serviço (e o profissional, se a pessoa tiver preferência), chame `ver_horarios_livres` e
  ofereça até 3 opções com dia da semana, data, hora e o profissional. Se a pessoa disser um dia ou período ("sexta",
  "de manhã", "semana que vem"), use os parâmetros da ferramenta.
- Só chame `marcar_horario` quando a pessoa escolher claramente um dos horários que vieram da ferramenta, com o
  profissional que veio junto. Precisa do nome da pessoa: pergunte se não souber. Nunca invente horário.
- Depois de marcar, confirme serviço, profissional, dia e hora. {dono} é avisado automaticamente.
- Se a ferramenta disser que o horário não está mais livre, peça desculpas e ofereça outros.
- Remarcar: chame `meus_horarios`, confirme qual é (se houver mais de um), chame `ver_horarios_livres` para o mesmo
  serviço, ofereça opções e, quando a pessoa escolher, chame `remarcar_horario`. Confirme o horário antigo e o novo.
- Cancelar: chame `meus_horarios`, confirme qual é e só depois da confirmação chame `cancelar_horario`. Ofereça
  remarcar em vez de cancelar, sem insistir. Se a base tiver regra de cancelamento, avise.
- Nunca diga que um horário está marcado, remarcado ou cancelado sem a ferramenta ter devolvido isso nesta conversa.
- Serviço que não está na lista ou pedido fora do horário de atendimento: encaminhe para {dono}."""

SECAO_SEM_AGENDA = """## Pedido de horário
Você não vê a agenda: nunca diga que há ou não há horário livre, nem que está marcado.
- Quando a pessoa quiser marcar, colete com naturalidade: serviço, dia e período de preferência, profissional (se
  tiver preferência) e o nome. Com tudo em mãos, encaminhe (acao='encaminhar') dizendo que passou o pedido para
  {dono} confirmar o horário. Em `resumo_para_dono`, ponha serviço, preferência de dia/hora, profissional e nome."""

FERRAMENTAS_AGENDA = [
    {
        "name": "ver_horarios_livres",
        "description": "Horários livres de verdade na agenda dos profissionais para um serviço.",
        "input_schema": {
            "type": "object",
            "properties": {
                "servico": {"type": "string", "description": "Nome do serviço, como está na lista."},
                "profissional": {"type": "string", "description": "Nome do profissional, ou vazio para qualquer um."},
                "a_partir_de": {"type": "string", "description": "Data AAAA-MM-DD a partir da qual procurar (vazio = hoje)."},
                "periodo": {"type": "string", "enum": ["qualquer", "manha", "tarde", "noite"]},
            },
            "required": ["servico", "profissional", "a_partir_de", "periodo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "marcar_horario",
        "description": ("Marca o horário na agenda do profissional. Só depois que o cliente escolheu um horário que veio "
                        "de ver_horarios_livres; use o inicio e o profissional que vieram juntos."),
        "input_schema": {
            "type": "object",
            "properties": {
                "servico": {"type": "string"},
                "profissional": {"type": "string", "description": "O `profissional` (id) que veio com o horário."},
                "inicio": {"type": "string", "description": "AAAA-MM-DDTHH:MM, exatamente como veio da lista."},
                "nome": {"type": "string", "description": "Nome do cliente."},
            },
            "required": ["servico", "profissional", "inicio", "nome"],
            "additionalProperties": False,
        },
    },
    {
        "name": "meus_horarios",
        "description": "Próximos horários marcados deste cliente (com agendamento_id). Use antes de remarcar ou cancelar.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "remarcar_horario",
        "description": ("Troca um horário marcado por outro. Só depois que o cliente escolheu, com clareza, um novo "
                        "horário que veio de ver_horarios_livres."),
        "input_schema": {
            "type": "object",
            "properties": {
                "agendamento_id": {"type": "string", "description": "De meus_horarios."},
                "novo_inicio": {"type": "string", "description": "AAAA-MM-DDTHH:MM, como veio da lista."},
                "profissional": {"type": "string", "description": "O profissional que veio com o novo horário."},
            },
            "required": ["agendamento_id", "novo_inicio", "profissional"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancelar_horario",
        "description": "Cancela um horário marcado, depois que o cliente confirmou qual.",
        "input_schema": {
            "type": "object",
            "properties": {"agendamento_id": {"type": "string", "description": "De meus_horarios."}},
            "required": ["agendamento_id"],
            "additionalProperties": False,
        },
    },
]

FERRAMENTA_RESPOSTA = {
    "name": "enviar_resposta",
    "description": ("Envia a resposta final ao cliente. Toda conversa termina com esta ferramenta. "
                    "acao='encaminhar' quando o assunto precisa do dono."),
    "input_schema": {
        "type": "object",
        "properties": {
            "acao": {"type": "string", "enum": ["responder", "encaminhar"]},
            "mensagem": {"type": "string", "description": "Texto que o cliente recebe, no idioma dele."},
            "idioma": {"type": "string", "enum": ["pt", "es", "en"]},
            "motivo": {"type": "string", "description": "Por que encaminhou (vazio se responder)."},
            "resumo_para_dono": {"type": "string", "description": "Uma frase em português (obrigatório se encaminhar)."},
        },
        "required": ["acao", "mensagem", "idioma", "motivo", "resumo_para_dono"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------
# Travas em código
# ---------------------------------------------------------------------
# afirmar que marcou/remarcou/cancelou (PT/ES/EN); só pode depois que a agenda devolveu isso nesta conversa
CONFIRMA_HORARIO = re.compile(
    r"\b(marquei|agendei|reservei|esta (marcad|agendad|confirmad|reservad)|ficou (marcad|agendad|confirmad)|"
    r"(horario|hora|atendimento|visita) (marcad|agendad|confirmad|cancelad|remarcad)|cancelei|remarquei|"
    r"te espero|te esperamos|quedo (agendad|reservad|confirmad)|"
    r"(you'?re|you are) (booked|all set)|i'?ve (booked|scheduled|cancel)|is (booked|confirmed|scheduled|cancel))")


def _negado(t: str, inicio: int) -> bool:
    """"ainda não está marcado", "no está confirmado", "isn't booked": negação até 3 palavras antes."""
    antes = re.findall(r"[a-z']+", re.split(_FIM_DE_FRASE, t[:inicio])[-1])[-3:]
    return bool(set(antes) & {"nao", "no", "not", "nem", "ni", "isn't", "nunca", "never", "ainda"})


def checar_mensagem(mensagem: str, *, descontos_permitidos: frozenset[str] = frozenset(), idioma: str = "pt",
                    hoje=None, permitidos: str | None = None, agenda_confirmou: bool = False) -> str | None:
    """Motivo da recusa, ou None se a mensagem pode sair."""
    t = _sem_acento(mensagem)
    for frase in re.split(_FIM_DE_FRASE, t):
        if re.search(r"\b(pix|chave|clave)\b", frase) and _DADO_PAGAMENTO.search(frase):
            return "não envie chave Pix"
    if re.search(r"https?://\S*(pag|pay|checkout|cobr)", t):
        return "não envie link de pagamento"
    if not agenda_confirmou and any(not _negado(t, m.start()) for m in CONFIRMA_HORARIO.finditer(t)):
        return ("você não marcou/remarcou/cancelou nada nesta conversa (nenhuma ferramenta da agenda devolveu isso): "
                "não diga que está marcado; use as ferramentas da agenda ou encaminhe para o dono")
    if re.search(PRAZO, t):
        return "não prometa prazo de resposta; diga só que a resposta chega em breve"
    if hoje and (motivo := conferir_dias_da_semana(mensagem, hoje)):
        return motivo
    if idioma in ("es", "en") and (achadas := PORTUGUES.findall(mensagem or "")):
        return (f"a resposta está em {IDIOMAS[idioma]}, mas tem palavra em português "
                f"({', '.join(sorted(set(p.lower() for p in achadas)))}): escreva tudo em {IDIOMAS[idioma]}")
    if motivo := _checar_desconto(t, descontos_permitidos):
        return motivo
    if permitidos is not None and (motivo := _checar_contato((mensagem or "").lower(), permitidos)):
        return motivo
    return None


# ---------------------------------------------------------------------
# Laço
# ---------------------------------------------------------------------
@dataclass
class ResultadoNegocio:
    acao: str
    mensagem: str
    idioma: str = "pt"
    motivo: str = ""
    resumo_para_dono: str = ""
    agenda: list[dict] = field(default_factory=list)   # marcado/remarcado/cancelado nesta mensagem (aviso ao dono)
    nome: str = ""                                      # nome que o cliente deu ao marcar
    passos: int = 0
    tokens_entrada: int = 0
    tokens_saida: int = 0
    fallback: bool = False
    pedido = None   # compatível com o webhook das pousadas (negócio de serviços não tem pedido de reserva)


def _secao_agenda(info: NegocioInfo, dono: str) -> str:
    if not info.servicos:
        return SECAO_SEM_AGENDA.format(dono=dono)
    servicos = "; ".join(f"{s['nome']} ({s['duracao_min']} min)" for s in info.servicos)
    profissionais = "; ".join(f"{p['nome']} ({', '.join(p['servicos'])})" if p.get("servicos") else f"{p['nome']} (todos)"
                              for p in info.profissionais)
    return SECAO_COM_AGENDA.format(servicos=servicos, profissionais=profissionais, dono=dono)


def atender_cliente(mensagem: str, info: NegocioInfo, *, cliente_conhecido: dict | None = None,
                    historico: list[dict] | None = None, agenda=None, cliente: anthropic.Anthropic | None = None,
                    modelo: str | None = None, agora: datetime | None = None) -> ResultadoNegocio:
    """agenda: agenda.servicos.AgendaNegocio desta conversa (None = sem agenda: o bot anota e encaminha)."""
    dono = nome_do_dono(info.dono)
    if agenda is None:
        info = NegocioInfo(info.nome, info.dono, info.cidade, info.faq)   # sem agenda, o prompt não fala de agenda
    agora = agora or datetime.now(ZoneInfo(FUSO_PADRAO))
    cliente = cliente or anthropic.Anthropic()
    modelo = modelo or os.environ.get("MODELO_BOT") or MODELO_PADRAO
    sabido = {k: v for k, v in (cliente_conhecido or {}).items() if v}
    ferramentas = [*(FERRAMENTAS_AGENDA if agenda else []), FERRAMENTA_RESPOSTA]
    system = [
        {"type": "text", "text": INSTRUCOES.format(negocio=info.nome, cidade=info.cidade or "Brasil", dono=dono,
                                                   secao_agenda=_secao_agenda(info, dono), faq=texto_faq(info.faq)),
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"Hoje é {NOMES_DIAS[agora.weekday()]}, {agora:%d/%m/%Y}, {agora:%H:%M} (horário de Brasília). "
                                 f"{calendario(agora.date())}\n"
                                 f"O que já sabemos deste cliente: {json.dumps(sabido, ensure_ascii=False) if sabido else 'nada ainda'}."},
    ]
    mensagens = [*(historico or []), {"role": "user", "content": mensagem}]
    idioma_conhecido = sabido.get("idioma") if sabido.get("idioma") in IDIOMAS else None
    r = ResultadoNegocio(acao="encaminhar", mensagem="", idioma=idioma_conhecido or "pt")
    descontos = descontos_do_faq(info.faq)
    permitidos = [texto_faq(info.faq)]
    agenda_confirmou = False

    def encaminhar_automatico(motivo: str) -> ResultadoNegocio:
        r.acao, r.motivo, r.fallback = "encaminhar", motivo, True
        r.mensagem = mensagem_encaminhamento(dono, idioma_conhecido)
        r.resumo_para_dono = f"O atendimento virtual não conseguiu responder: \"{mensagem[:160]}\""
        if agenda is not None:
            r.agenda = list(agenda.eventos)   # o que a agenda já fez continua valendo: o dono precisa saber
        log.warning("negocio: encaminhamento automático: %s", motivo)
        return r

    acoes_agenda = {"ver_horarios_livres", "marcar_horario", "meus_horarios", "remarcar_horario", "cancelar_horario"}
    for _ in range(MAX_PASSOS):
        r.passos += 1
        try:
            resposta = cliente.messages.create(model=modelo, max_tokens=MAX_TOKENS, system=system, tools=ferramentas,
                                               tool_choice={"type": "any"}, messages=mensagens)
        except anthropic.APIError as e:
            return encaminhar_automatico(f"erro na API: {type(e).__name__}")
        r.tokens_entrada += resposta.usage.input_tokens
        r.tokens_saida += resposta.usage.output_tokens
        chamadas = [b for b in resposta.content if b.type == "tool_use"]
        if not chamadas:
            return encaminhar_automatico("modelo não chamou ferramenta")
        mensagens.append({"role": "assistant", "content": resposta.content})
        resultados = {}
        # a agenda roda antes da resposta, mesmo fora de ordem no mesmo turno ("marquei" só sai se marcou)
        for c in sorted(chamadas, key=lambda c: c.name == "enviar_resposta"):
            e = c.input if isinstance(c.input, dict) else {}
            erro = False
            if c.name == "enviar_resposta":
                e = {**e, "mensagem": e.get("mensagem") or e.get("mensaje") or e.get("message") or ""}
                if e.get("acao") not in ("responder", "encaminhar"):
                    problema = "acao precisa ser 'responder' ou 'encaminhar'"
                elif not str(e["mensagem"]).strip():
                    problema = "o campo `mensagem` está vazio (o nome da chave é sempre `mensagem`)"
                elif e["acao"] == "encaminhar" and not str(e.get("resumo_para_dono") or "").strip():
                    problema = "ao encaminhar, preencha resumo_para_dono"
                else:
                    problema = checar_mensagem(e["mensagem"], descontos_permitidos=descontos,
                                               idioma=e.get("idioma") or "pt", hoje=agora.date(),
                                               permitidos="\n".join(permitidos), agenda_confirmou=agenda_confirmou)
                if problema is None:
                    r.acao = e["acao"]
                    r.idioma = e.get("idioma") if e.get("idioma") in IDIOMAS else r.idioma
                    r.mensagem = formatar_whatsapp(e["mensagem"].strip())
                    r.motivo = str(e.get("motivo") or "").strip()
                    r.resumo_para_dono = str(e.get("resumo_para_dono") or "").strip()
                    r.agenda = list(agenda.eventos) if agenda is not None else []
                    return r
                log.info("negocio: resposta recusada (%s)", problema)
                saida, erro = {"erro": f"Resposta inválida: {problema}. Reescreva e chame enviar_resposta de novo."}, True
            elif c.name in acoes_agenda and agenda is not None:
                try:
                    saida = getattr(agenda, c.name)(**{k: str(v) for k, v in e.items()})
                except TypeError:
                    saida = {"erro": "parâmetros inválidos para " + c.name}
                except Exception:
                    log.exception("negocio: agenda falhou em %s", c.name)
                    saida = {"erro": "agenda indisponível agora; não marque nada e encaminhe para o dono"}
                erro = "erro" in saida
                if saida.get("marcado") or saida.get("remarcado") or saida.get("cancelado") or saida.get("horarios"):
                    agenda_confirmou = agenda_confirmou or c.name != "ver_horarios_livres"
                if c.name == "marcar_horario" and saida.get("marcado"):
                    r.nome = str(e.get("nome") or "").strip()[:80]
                permitidos.append(json.dumps(saida, ensure_ascii=False))
            else:
                saida, erro = {"erro": f"ferramenta desconhecida: {c.name}"}, True
            resultados[c.id] = {"type": "tool_result", "tool_use_id": c.id,
                                "content": json.dumps(saida, ensure_ascii=False), "is_error": erro}
        mensagens.append({"role": "user", "content": [resultados[c.id] for c in chamadas]})
    return encaminhar_automatico(f"sem resposta final em {MAX_PASSOS} passos")
