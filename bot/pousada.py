"""Recepcionista de WhatsApp para pousadas (modo NEGOCIO=pousada).

Laço de IA (Claude + ferramentas + enviar_resposta) com o papel de recepcionista:
responder hóspedes 24 h em português, espanhol ou inglês usando SÓ o FAQ da pousada e receber pedidos de
reserva (datas, pessoas, nome, contato), que vão prontos para o dono confirmar.

O bot NÃO confirma reserva, NÃO cobra e NÃO dá desconto. Além do prompt, `checar_mensagem` barra no código
frases que confirmam reserva ou disponibilidade (PT/ES/EN, com negação e condição: "não está confirmada", "depois
do sinal"), chave Pix/dados bancários/link de pagamento, e-mail/telefone/link que não está no FAQ e desconto.
Também em código: a ressalva de valores em toda resposta com dinheiro (`com_ressalva`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic

from bot.assistente import FUSO_PADRAO, MAX_PASSOS, MAX_TOKENS, MODELO_PADRAO, formatar_whatsapp

log = logging.getLogger(__name__)

IDIOMAS = {"pt": "português", "es": "espanhol", "en": "inglês"}

# Mensagens fixas por idioma. Idioma do hóspede conhecido: só a linha dele; desconhecido: PT + ES + EN.
ENCAMINHAMENTO = {
    "pt": "Obrigado pela mensagem! Já passei para {dono}; a resposta chega em breve.",
    "es": "¡Gracias por tu mensaje! Ya se lo pasé {dono}; te responden en breve.",
    "en": "Thanks for your message! I've passed it to {dono}; you'll hear back soon.",
}
# pedido já registrado (o dono recebe o 🛎️), mas o modelo não conseguiu fechar a resposta: o hóspede não pode receber
# "não conseguiu responder" (reteste de 09/10, pt58: 6 recusas seguidas da trava de valores)
PEDIDO_PASSADO = {
    "pt": "Pronto! Seu pedido de reserva foi passado para {dono}, que confirma a disponibilidade e responde em breve.",
    "es": "¡Listo! Tu pedido de reserva ya se lo pasé {dono}, que confirma la disponibilidad y te responde en breve.",
    "en": "Done! Your booking request has been passed to {dono}, who confirms availability and gets back to you soon.",
}
NAO_TEXTO = {
    "pt": "Por aqui eu respondo melhor por texto: me conta por escrito como posso ajudar? 😊",
    "es": "Por acá respondo mejor por texto: ¿me contás por escrito en qué te ayudo?",
    "en": "I answer best by text here: could you write how I can help? 😊",
}
RESSALVA = {
    "pt": "Valores confirmados pela pousada na reserva.",
    "es": "Valores confirmados por la posada al reservar.",
    "en": "Rates are confirmed by the property when booking.",
}
# dono que não é nome de pessoa ("a equipe da pousada", "A equipe"): rótulo no idioma, sem "a a equipe"
EQUIPE = {"pt": "a equipe da pousada", "es": "al equipo de la posada", "en": "the property's team"}
_ROTULO_EQUIPE = re.compile(r"^(a |o |la |el |the )?(equipe|equipo|time|team|pousada|posada|recep\w*|staff)\b")
MENSAGEM_ENCAMINHAMENTO = "\n".join(ENCAMINHAMENTO.values())   # com {dono}; use mensagem_encaminhamento
MENSAGEM_NAO_TEXTO = "\n".join(NAO_TEXTO.values())


def no_idioma(textos: dict[str, str], idioma: str | None) -> str:
    """O texto no idioma do hóspede; sem idioma conhecido, PT + ES + EN (uma linha cada)."""
    return textos[idioma] if idioma in textos else "\n".join(textos.values())


def dono_na_frase(dono: str, idioma: str) -> str:
    """Como o dono entra numa frase fixa: o nome (em espanhol com "a": "a Carla") ou o rótulo da equipe no idioma."""
    nome = re.sub(r"\s*\([^()]*\)\s*", " ", dono or "").strip()
    if not nome or _ROTULO_EQUIPE.match(_sem_acento(nome)):
        return EQUIPE[idioma]
    return f"a {nome}" if idioma == "es" else nome


def mensagem_encaminhamento(dono: str, idioma: str | None = None) -> str:
    """Aviso de encaminhamento no idioma do hóspede (desconhecido: nos 3 idiomas). Dono que não é nome próprio
    ("a equipe da pousada") não vai em português nas linhas em espanhol e inglês."""
    if idioma in ENCAMINHAMENTO:
        return ENCAMINHAMENTO[idioma].format(dono=dono_na_frase(dono, idioma))
    return "\n".join(t.format(dono=dono_na_frase(dono, i)) for i, t in ENCAMINHAMENTO.items())


@dataclass
class PousadaInfo:
    nome: str
    dono: str
    cidade: str = ""
    faq: list[dict] = field(default_factory=list)   # [{id, pt: {p, r}, es: {p, r}}] + itens do guia (tipo="guia")
    mapa_url: str = ""  # página com o mapa do guia (whatsapp/pousada_app.py: /mapa/<slug>)
    reserva_url: str = ""  # pousada que só reserva pelo site: o bot manda o link em vez de registrar pedido


# ---------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------
INSTRUCOES = """# Atendimento virtual da {pousada}

Você é o atendimento virtual da {pousada} ({cidade}) no WhatsApp. Conversa com hóspedes e interessados.
Você não é {dono} (a pessoa responsável pela pousada) e nunca se passa por ela.

## Idioma
- Responda SEMPRE no idioma da última mensagem do hóspede: português, espanhol ou inglês. Em espanhol, use um
  espanhol neutro e próximo (pode usar "vos" se o hóspede usar; criança = "niño/a"). Informe o idioma em `idioma`.
- O nome da pousada é nome próprio: escreva sempre exatamente "{pousada}", letra por letra, também em espanhol e
  inglês (nunca traduza nenhuma palavra dele).
- O FAQ abaixo está em português e espanhol; para inglês, traduza fielmente.
- Escreva a mensagem inteira no idioma do hóspede: em espanhol ou inglês, nenhuma palavra em português (ex.: "allá"
  ou "there", nunca "lá"). Só nomes próprios (pousada, acomodações, lugares do guia) ficam como estão.
- Se "{dono}" não é nome de pessoa (ex.: "a equipe da pousada"), traduza: "el equipo de la posada" / "the
  property's team".

## Apresentação
- Não se apresente: responda direto, como o WhatsApp da pousada (um "Oi!" ou "¡Hola!" basta na primeira resposta).
- Nunca diga que é uma pessoa, que é {dono} ou que é da equipe. Se perguntarem diretamente se é robô, atendente
  automático ou pessoa, responda com sinceridade, em uma frase, que é o atendimento virtual (automático) da {pousada}
  e que {dono} entra na conversa quando precisa (em espanhol: "la atención virtual de la {pousada}"; em inglês: "the
  virtual assistant of {pousada}", sem possessivo).

## O que você pode dizer
- Use SÓ as informações do FAQ abaixo. Nunca invente preço, regra, horário, distância, serviço ou disponibilidade.
- Serviço ou detalhe que o FAQ e o guia não citam (ex.: transfer, estacionamento, gerador): não afirme nem negue.
  Nunca escreva "a pousada não oferece/não tem ..." se o FAQ não diz isso; diga que não tem essa informação e
  encaminhe para {dono}. Em "como chegar", use a seção Como chegar do guia e não fale de transfer se não perguntarem.
- Ao citar {dono}, escreva só o nome, sem rótulo entre parênteses (nada de "(donos)" ou "(dueños)").
- Sempre que a resposta trouxer um valor em dinheiro (diária, taxa, sinal), termine com a ressalva, no idioma do
  hóspede: "Valores confirmados pela pousada na reserva." (es: "Valores confirmados por la posada al reservar.";
  en: "Rates are confirmed by the property when booking.").
- Você não vê a agenda da pousada: nunca diga que há ou que não há vaga, nem que uma data está livre.
  {disponibilidade}
- Datas: antes de dizer o dia da semana de uma data ou o preço de uma estadia com datas (mesmo aproximadas, como
  "de 20 a 23 de novembro"), chame a ferramenta `noites_da_estadia`: ela devolve, pelo calendário de verdade, cada
  noite com o dia da semana; repita os dias da semana exatamente como ela devolver. Para o resto (mês inteiro, "fim
  de dezembro"), use o calendário no fim destas instruções (os próximos dias um a um e a tabela de sextas, sábados e
  domingos de cada mês). Nunca calcule dia da semana de cabeça nem diga que uma data futura "já passou". Noites =
  da entrada até a saída (sexta a domingo = 2 noites). Repita as datas com os números que o hóspede deu; se ficar
  ambíguo (ex.: "este fim de semana" num domingo), pergunte as datas.
- Preço de uma estadia: diga o número de noites, a diária de cada noite ou período exatamente como está no FAQ (por
  dia da semana e temporada) e as taxas fixas do FAQ, e que {dono} confirma o valor final no pedido. NUNCA some o
  total nem escreva um valor em dinheiro que não esteja no FAQ (nada de "2 x R$ 750 = R$ 1.500"); se o hóspede fizer
  a conta, não confirme o resultado: diga que {dono} confirma o total. Estadia que cruza dois períodos de tarifa, cai
  no dia em que um período começa ou termina, ou não cumpre o mínimo de noites de um período: informe as diárias dos
  períodos envolvidos e a regra do mínimo, e diga que {dono} confirma o valor e as condições. Nunca escreva "não dá"
  ou "não é possível" para uma estadia abaixo do mínimo: diga o mínimo, pergunte se dá para ajustar as datas e, se a
  pessoa mantiver, {dono} decide. Um período especial dentro de outro (ex.: Réveillon dentro da alta temporada):
  fora do especial vale a tarifa do período maior, não a tarifa normal. Quando `noites_da_estadia` disser em que
  período cada noite cai, use para cada noite a diária que o FAQ dá para aquele período; a tarifa de fim de semana
  só vale onde o FAQ a prevê (ex.: fora da alta temporada). Noite marcada como "borda": diga que {dono} confirma a
  tarifa dela.
- Você NÃO confirma reserva, NÃO cobra (nunca envie chave Pix, dados bancários ou link de pagamento) e NÃO dá
  desconto nem condição especial.

{secao_reserva}

## Encaminhe para {dono} (acao='encaminhar')
- Pedido de desconto, negociação de preço ou condição especial.
- Reclamação ou problema durante a estadia.
- Grupo grande (mais de 8 pessoas ou mais de 2 acomodações) e eventos.
- Alteração, cancelamento ou dúvida sobre uma reserva que já existe.
- Qualquer pergunta que o FAQ e o guia local não respondem (ex.: transfer, passeio ou lugar que não está no guia).
Ao encaminhar, diga ao hóspede, no idioma dele, que passou para {dono} e que a resposta chega em breve. Não
prometa o resultado (nem confirmação de reserva ou pagamento, nem desconto) nem prazo ("em poucos minutos", "hoje
ainda", "já já"): você não sabe quando {dono} vai ver; diga só que vai ver e responder. Em
`resumo_para_dono`, UMA frase em português com o que a pessoa quer (sem saudação).

## Guia local (mercado, farmácia, roupa, restaurante, praia, trilha...)
- Se a ferramenta `buscar_lugares` estiver disponível, use-a PRIMEIRO para "onde tem/onde fica": ela traz dados atuais
  do Google Maps (distância, aberto agora, horário, nota, faixa de preço). Indique até 3, os mais perto primeiro, e
  termine com "Fonte: Google Maps". Se ela der erro ou não existir, use o guia abaixo.
  `mais_conhecidos_da_regiao` são lugares maiores mais longe (ex.: supermercado em outra localidade): cite um deles
  quando o hóspede quiser mais opção, preço ou compra grande (rancho).
- Sem a ferramenta, para "onde tem/onde fica", indique SÓ lugares do guia abaixo: no máximo 3, os mais perto primeiro, com a distância
  aproximada da pousada e uma frase do que é. Nunca invente lugar, endereço, horário, preço ou opinião.
- "Perto", "pertinho", "a poucos minutos": só com a distância do guia ou da busca. Lugar com "distância não
  informada": diga só onde fica (ex.: "em Ibiraquera"), sem dizer se é perto ou longe.
- Toda distância é a partir da pousada, como está no guia ("a 630 m da pousada"); nunca a partir do centrinho, da
  praia ou de outro lugar.
- Diga de cada lugar só o que o guia ou a busca trazem. Não deduza produto ou serviço (ex.: que o supermercado vende
  remédio, que o restaurante entrega): se pediram algo que só uma categoria do guia tem, indique só essa categoria.
- Se é caro ou barato, bom ou ruim: use a opinião de {dono} quando houver; senão, só a faixa de preço e a nota do
  Google, ditas como "no Google" (ex.: "nota 4,6 no Google, faixa $$"). Nunca dê opinião própria.
- Horários: diga que podem mudar fora da temporada e que vale confirmar. Lugar marcado "só na temporada": avise.
- Se não achar o que pediram (nem na busca nem no guia), diga que não encontrou e encaminhe para {dono}. Nunca diga
  que não existe sem antes buscar.
- {mapa}

## Estilo
- Curto (2 a 4 frases), acolhedor e direto. Use o primeiro nome quando souber.
- Formatação do WhatsApp: negrito com *um asterisco*; sem títulos nem listas longas; no máximo um emoji.
- Toda resposta termina chamando `enviar_resposta`.

## FAQ da {pousada}
{faq}

## Guia local da {pousada} (distâncias a partir da pousada)
{guia}
"""


RESERVA_COM_PEDIDO = """## Pedido de reserva
- Pagamento e sinal: explique como funciona pelo FAQ e diga que {dono} envia os dados.
- Quando a pessoa quiser reservar ou perguntar por vaga em datas, colete com naturalidade (pode ser em mais de uma
  mensagem): data de entrada, data de saída (ou número de noites), quantos adultos, quantas crianças e as idades,
  e o nome. O contato é este WhatsApp; pergunte só se ela quiser deixar outro.
- O nome é o que a pessoa escreveu: nunca registre sem ele nem preencha com "<UNKNOWN>", "hóspede" ou "n/a". A
  mensagem pronta do formulário do site ("Gostaria de consultar a disponibilidade...") vem sem nome: trate como
  pedido, confirme os dados e pergunte o nome antes de registrar.
- Datas sem ano: use a próxima ocorrência a partir de hoje. Se faltar o dia exato (ex.: "em janeiro"), pergunte.
- Se o pedido não cumpre o mínimo de diárias do FAQ, avise a regra com gentileza e pergunte se dá para ajustar;
  se a pessoa mantiver, registre assim mesmo e anote em `observacoes`.
- Com tudo em mãos, chame `registrar_pedido_reserva` SOZINHO; quando ele responder, chame `enviar_resposta` dizendo que o pedido foi passado
  para {dono}, que confirma a disponibilidade e envia os dados do sinal em breve. Não diga que está reservado.
- Pet, acomodação preferida ou chegada tarde: registre em `observacoes`."""

RESERVA_PELO_SITE = """## Reserva: só pelo site da pousada
- A {pousada} NÃO faz reserva nem cobrança pelo WhatsApp. Quando a pessoa quiser reservar, saber se tem vaga numa
  data ou o valor exato de uma data, mande o link {reserva_url} e explique: lá ela escolhe as datas, vê a
  disponibilidade e o valor na hora, e o site tem a melhor tarifa garantida. Não colete datas, nomes nem dados.
- Você pode ajudar a escolher antes (qual bangalô serve para quantas pessoas, mínimo de noites, regras, pagamento
  pelo FAQ) e então mandar o link.
- Pediram para confirmar a reserva ou disseram que já pagaram: a reserva é feita e confirmada só pelo site; mande o
  link {reserva_url}. Nunca diga que {dono} ou alguém vai confirmar a reserva pelo WhatsApp. Se a pessoa já pagou
  e não recebeu a confirmação, encaminhe para {dono} dizendo só que passou a mensagem.
- Problema no site, pedido especial ou dúvida sobre uma reserva já feita: encaminhe para {dono}."""


def texto_faq(faq: list[dict]) -> str:
    linhas = []
    for item in faq:
        if item.get("tipo") in ("guia", "temporadas"):
            continue
        for idioma in ("pt", "es"):
            qa = item.get(idioma) or {}
            if qa.get("p") and qa.get("r"):
                linhas.append(f"[{idioma}] P: {qa['p']}\n[{idioma}] R: {qa['r']}")
        linhas.append("")
    return "\n".join(linhas).strip()


def _distancia(km: float | None) -> str:
    """Distância de rua aproximada: a pé até 2 km (~80 m/min), de carro acima (~30 km/h)."""
    if km is None:
        return "distância não informada"
    if km < 1:
        texto = f"cerca de {round(km * 1000, -1):.0f} m"
    else:
        texto = f"cerca de {km:.1f} km".replace(".", ",")
    return f"{texto} ({max(1, round(km * 1000 / 80))} min a pé)" if km <= 2 else f"{texto} (~{max(1, round(km * 2))} min de carro)"


def texto_guia(faq: list[dict]) -> str:
    """Itens tipo="guia" agrupados por categoria, do mais perto para o mais longe."""
    itens = sorted((i for i in faq if i.get("tipo") == "guia"),
                   key=lambda i: (i.get("categoria") or "", i.get("km") is None, i.get("km") or 0))
    if not itens:
        return "(sem guia local: encaminhe perguntas sobre lugares da região)"
    linhas, categoria = [], None
    for i in itens:
        if i.get("categoria") != categoria:
            categoria = i.get("categoria")
            linhas.append(f"\n### {categoria}")
        dist = (f"cerca de {i['km']:.0f} km ({i['tempo']})" if i.get("tempo") and i.get("km") is not None
                else _distancia(i.get("km")))
        partes = [f"- {i['nome']}: {i.get('descricao') or ''}".rstrip(": "), dist]
        for rotulo, chave in (("endereço", "endereco"), ("horário", "horario"), ("contato", "contato"), ("preço", "preco")):
            if i.get(chave):
                partes.append(f"{rotulo}: {i[chave]}")
        if i.get("temporada"):
            partes.append("só na temporada")
        if i.get("opiniao_dono"):
            partes.append(f"opinião de quem é da pousada: {i['opiniao_dono']}")
        linhas.append(" · ".join(partes))
    return "\n".join(linhas).strip()


FERRAMENTA_LUGARES = {
    "name": "buscar_lugares",
    "description": ("Busca no Google Maps, agora, os lugares mais perto da pousada. Use uma categoria; para qualquer "
                    "outra coisa (sorveteria já tem categoria; lavanderia, pet shop, aluguel de prancha, açaí, loja de "
                    "surf...) use categoria='outro' e escreva em `termo` o que procurar, em português. Devolve nome, "
                    "distância, aberto agora, horários, nota e faixa de preço do Google."),
    "input_schema": {
        "type": "object",
        "properties": {"categoria": {"type": "string", "enum": ["mercado", "padaria", "farmacia", "roupas", "restaurante",
                                                                  "cafe", "sorveteria", "bar", "caixa_eletronico",
                                                                  "posto_gasolina", "saude", "praia_passeio", "outro"]},
                       "termo": {"type": "string", "description": "O que procurar quando categoria='outro' (ex.: lavanderia)."}},
        "required": ["categoria", "termo"],
        "additionalProperties": False,
    },
}

FERRAMENTA_NOITES = {
    "name": "noites_da_estadia",
    "description": ("Calcula, pelo calendário de verdade, as noites entre a entrada e a saída, com o dia da semana de "
                    "cada uma e quais são de sexta ou sábado. Chame SEMPRE antes de informar o dia da semana de uma data "
                    "ou o preço de uma estadia com datas, e repita os dias da semana exatamente como vierem. Para uma "
                    "data só, use saída = dia seguinte. Datas sem ano: a próxima ocorrência a partir de hoje."),
    "input_schema": {
        "type": "object",
        "properties": {"entrada": {"type": "string", "description": "Data de entrada AAAA-MM-DD."},
                       "saida": {"type": "string", "description": "Data de saída AAAA-MM-DD."},
                       "idioma": {"type": "string", "enum": ["pt", "es", "en"],
                                  "description": "Idioma do hóspede: os dias da semana voltam nele."}},
        "required": ["entrada", "saida", "idioma"],
        "additionalProperties": False,
    },
}


NOMES_DIAS_IDIOMA = {"pt": ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"],   # = NOMES_DIAS (definido abaixo)
                     "es": ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"],
                     "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}
FORA_DE_TEMPORADA = {"pt": "fora das temporadas (tarifa normal)", "es": "fuera de temporada (tarifa normal)",
                     "en": "outside the seasons (regular rate)"}
BORDA = {"pt": "borda de {nome}: a pousada confirma a tarifa desta noite",
         "es": "borde de {nome}: la posada confirma la tarifa de esta noche",
         "en": "edge of {nome}: the property confirms this night's rate"}


def temporadas_do_faq(faq: list[dict]) -> list[dict]:
    """Períodos de tarifa cadastrados no FAQ (item tipo="temporadas", periodos=[{nome, de: "dd/mm", ate: "dd/mm"}]),
    sem preço: o preço continua no texto do FAQ; a ferramenta só diz em que período cada noite cai."""
    periodos = []
    for item in faq:
        if item.get("tipo") != "temporadas":
            continue
        for per in item.get("periodos") or []:
            try:
                d1, m1 = (int(x) for x in str(per["de"]).split("/"))
                d2, m2 = (int(x) for x in str(per["ate"]).split("/"))
                periodos.append({"nome": str(per["nome"]), "de": (m1, d1), "ate": (m2, d2)})
            except (KeyError, ValueError, TypeError):
                log.warning("pousada: período de temporada inválido: %r", per)
    return periodos


def periodos_da_noite(d: date, temporadas: list[dict]) -> list[tuple[str, bool]]:
    """[(nome do período, é borda)] para a noite d; período que vira o ano (15/12 a 15/03) também vale."""
    md = (d.month, d.day)
    achados = []
    for per in temporadas:
        de, ate = per["de"], per["ate"]
        dentro = de <= md <= ate if de <= ate else (md >= de or md <= ate)
        if dentro:
            achados.append((per["nome"], md in (de, ate)))
    return achados


def noites_da_estadia(e: dict, hoje: date, temporadas: list[dict] | None = None) -> dict:
    """Noite a noite, com o dia da semana pelo calendário de verdade (o Haiku errava: QA do Refúgio) e, se a
    pousada cadastrou temporadas, em que período de tarifa cada noite cai (noite na data-limite = borda: o dono
    confirma; o FAQ do Refúgio diz "15/12 a 15/03" sem dizer se 15/03 é noite ou saída)."""
    idioma = e.get("idioma") if e.get("idioma") in NOMES_DIAS_IDIOMA else "pt"
    dias = NOMES_DIAS_IDIOMA[idioma]
    try:
        entrada, saida = date.fromisoformat(str(e.get("entrada"))), date.fromisoformat(str(e.get("saida")))
    except ValueError:
        return {"erro": "datas no formato AAAA-MM-DD"}
    if saida <= entrada:
        return {"erro": "a saída precisa ser depois da entrada"}
    if saida - entrada > timedelta(days=MAX_NOITES):
        return {"erro": f"mais de {MAX_NOITES} noites: confirme as datas com o hóspede"}
    noites = [entrada + timedelta(n) for n in range((saida - entrada).days)]

    def nome(d: date) -> str:
        return f"{dias[d.weekday()]} {d:%d/%m/%Y}"

    def noite(d: date) -> str:
        if not temporadas:
            return nome(d)
        per = periodos_da_noite(d, temporadas)
        if not per:
            return f"{nome(d)} · {FORA_DE_TEMPORADA[idioma]}"
        return f"{nome(d)} · " + " · ".join(BORDA[idioma].format(nome=n) if borda else n for n, borda in per)

    resultado = {"entrada": nome(entrada), "saida": nome(saida), "noites": len(noites), "lista": [noite(d) for d in noites],
                 "noites_de_sexta_e_sabado": [nome(d) for d in noites if d.weekday() in (4, 5)],
                 "noites_de_domingo_a_quinta": [nome(d) for d in noites if d.weekday() not in (4, 5)]}
    if temporadas:
        resultado["aviso"] = ("cada noite tem a diária do seu período, como está no FAQ (fim de semana = sexta e sábado só "
                              "fora das temporadas, se o FAQ disser assim); noite marcada como borda: a pousada confirma")
    if entrada < hoje:
        resultado["aviso"] = f"a entrada {entrada:%d/%m/%Y} já passou (hoje é {hoje:%d/%m/%Y}): confirme o ano com o hóspede"
    return resultado


FERRAMENTAS = [
    FERRAMENTA_NOITES,
    {
        "name": "registrar_pedido_reserva",
        "description": ("Registra um pedido de reserva completo para o dono confirmar. Só chame com entrada, saída, "
                        "adultos e nome. NÃO confirma a reserva."),
        "input_schema": {
            "type": "object",
            "properties": {
                "entrada": {"type": "string", "description": "Data de entrada AAAA-MM-DD."},
                "saida": {"type": "string", "description": "Data de saída AAAA-MM-DD."},
                "adultos": {"type": "integer", "minimum": 1},
                "criancas_idades": {"type": "array", "items": {"type": "integer"}, "description": "Idade de cada criança."},
                "nome": {"type": "string", "description": ("Nome do hóspede, como ele escreveu. Nunca um placeholder "
                                                           "(<UNKNOWN>, 'hóspede', 'n/a'): sem o nome, pergunte antes.")},
                "contato": {"type": "string", "description": "Outro contato, se o hóspede deu (vazio = este WhatsApp)."},
                "acomodacao": {"type": "string", "description": "Acomodação preferida, se disse."},
                "observacoes": {"type": "string", "description": "Pet, chegada tarde, mínimo de diárias etc."},
            },
            "required": ["entrada", "saida", "adultos", "criancas_idades", "nome", "contato", "acomodacao", "observacoes"],
            "additionalProperties": False,
        },
    },
    {
        "name": "enviar_resposta",
        "description": ("Envia a resposta final ao hóspede. Toda conversa termina com esta ferramenta. "
                        "acao='encaminhar' quando o assunto precisa do dono."),
        "input_schema": {
            "type": "object",
            "properties": {
                "acao": {"type": "string", "enum": ["responder", "encaminhar"]},
                "mensagem": {"type": "string", "description": "Texto que o hóspede recebe, no idioma dele."},
                "idioma": {"type": "string", "enum": ["pt", "es", "en"]},
                "motivo": {"type": "string", "description": "Por que encaminhou (vazio se responder)."},
                "resumo_para_dono": {"type": "string", "description": "Uma frase em português (obrigatório se encaminhar)."},
            },
            "required": ["acao", "mensagem", "idioma", "motivo", "resumo_para_dono"],
            "additionalProperties": False,
        },
    },
]


# ---------------------------------------------------------------------
# Travas em código
# ---------------------------------------------------------------------
def _sem_acento(texto: str) -> str:
    texto = re.sub("[\u2010-\u2015\u2212]", "-", texto or "").replace("\u00a0", " ")   # travessão: "12–14" não vira "1214"
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()


# Palavra de negação logo antes do trecho ("não confirmamos sua reserva", "no está confirmada").
_NEGACAO = {"nao", "not", "nunca", "never", "nem", "ni", "jamais"}
# Hipótese na mesma oração, antes do trecho ("se tiver vaga", "a Carla confere se..."). Só verbo no presente ou
# imperativo: "conferi a agenda e temos vaga" é afirmação, não hipótese. Pergunta indireta também ("no site você vê
# qual bangalô tem vaga", avaliação de 04/10).
_HIPOTESE = re.compile(
    r"\b(caso|if|whether|ver se|ve se|veja se|fijar si|confere|conferir|confira|verifica|verificar|verifique|"
    r"consulta|consultar|consulte|averigua|averiguar|qual|quais|cual|cuales|which)\b"
    r"|\b(se|si|if) $|\bse (o|a|os|as|tem|ha|houver|tiver|estiver|esta|estao)\b|\bsi (el|la|los|las|hay|tenemos|esta|estan)\b")
# Condição de tempo ("quando", "depois que", "after"): só vale se fala da confirmação do dono ou do sinal.
# "Quando chegarem é só fazer o check-in, a reserva está confirmada" continua barrada.
_TEMPORAL = re.compile(r"\b(depois que|apos|assim que|quando|cuando|una vez que|despues de que|after|once|when|until|"
                       r"ate que|hasta que)\b")
_INICIO_HIPOTESE = re.compile(r"(se|si|if|caso)\b")
_INICIO_TEMPORAL = re.compile(r"(depois que|apos|assim que|quando|cuando|una vez que|despues de que|after|once|when)\b")
_FALA_DA_CONFIRMACAO = re.compile(r"confirm|sinal|sena\b|deposit|pag|pay|aprov|approv|disponib|availab|respond|"
                                  r"reply|dono|dona\b|duen|owner")
# Condição logo depois do trecho ("a reserva fica garantida depois do sinal", "is confirmed only after...").
_CONDICAO_DEPOIS = re.compile(
    r"\w*\s+(so |somente |apenas |solo |only |just )?(depois|apos|quando|assim que|mediante|com o sinal|com sinal|"
    r"com o pagamento|cuando|despues|una vez|recien|con la sena|con el pago|after|once|when|upon|with the deposit|"
    r"if |se |si )")

_VERBOS_VAGA = (r"(temos|tenemos|ha|hay|quedan|queda|restam|resta|sobram|sobra|we have|we'?ve got|we still have|"
                r"there is|there are|there'?s)")
# "vaga" de carro não é vaga de hospedagem
_ESTACIONAMENTO = (r"(?! ?((de|para|pra|for) (o |os |el |los |\d+ )?(estacion|carro|auto|veicul|garag|moto|coche|parking|car)"
                   r"|(na|en la|on the|in the) (rua|calle|street)|na garagem|en el garaje))")
_TERMO_VAGA = (r"(vagas?|lugar(es)?|disponibilidad|disponibilidade|availability|vacanc\w*|space|"
               r"(a |one |\d+ )?rooms? (available|free|open|left|for (you|those|these|your|the))|"
               r"quartos? (livres?|disponive\w*|para (voce|voces|essas|esses|as|o dia))|"
               r"habitacion(es)? (libres?|disponibles?|para (vos|ustedes|esas|tus|las)))")
_NUMERO_EXTENSO = {"zero", "um", "uma", "dois", "duas", "tres", "quatro", "cinco", "seis", "sete", "oito", "nove", "dez",
                   "uno", "dos", "cuatro", "siete", "ocho", "nueve", "diez", "one", "two", "three", "four", "five", "six",
                   "seven", "eight", "nine", "ten", "vinte", "trinta", "quarenta", "cinquenta", "sessenta", "setenta",
                   "oitenta", "noventa", "veinte", "treinta", "cuarenta", "cincuenta"}
_EXTENSO = r"(\d|dez|cinco|quinze|vinte|trinta|cinquenta|diez|quince|veinte|treinta|cincuenta|ten|five|fifteen|twenty|thirty|fifty)"

# (padrão, motivo, contexto) sobre o texto sem acento e em minúsculas. contexto: False = sempre barra; True = não
# barra se negado ou condicionado ("não está confirmada", "se tiver vaga", "depois do sinal").
PROIBIDO = [
    # confirmação de reserva
    (r"\breserva (esta |ficou |fica |foi |queda |quedo |quedara |ta )?(confirmad|garantid)",
     "não diga que a reserva está confirmada", True),
    (r"\breserva (esta |ficou |fica |foi |queda |quedo |quedara |ta )(feita|hecha)",
     "não diga que a reserva está confirmada", True),
    (r"\b(confirmo|confirmamos|confirmei|confirmada|confirmado) (a |sua |seu |o |tu |su |la )?(reserva|estadia|hospedagem)"
     r"|\bconfirme (tu|su) reserva", "não confirme reserva", True),
    (r"\b(booking|reservation|stay|room)( is| has been| was| is now| are| have been)? (all )?"
     r"(confirmed|guaranteed|secured|booked|reserved)\b", "não confirme reserva", True),
    (r"\b(esta|ta|fica|ficou|queda|quedo|tudo|todo)( tudo| todo| ya)? (confirmad|reservad|garantid)", "não confirme reserva", True),
    (r"(^|[^a-z])(reservad[oa]s?|confirmad[oa]s?|booked|confirmed)\s*!", "não confirme reserva", True),
    (r"(^|[,;:]\s*)(reservad[oa]s?|confirmad[oa]s?|booked|confirmed)\s*([.!]|$)", "não confirme reserva", True),
    (r"\b(ja )?(reservei|deixei reservad|dejo reservad|i'?ve booked|we'?ve booked|i booked|we booked|i reserved|"
     r"we reserved|you'?re (all )?booked|you are (all )?booked|it'?s (all )?(confirmed|booked))",
     "não confirme reserva", True),
    (r"\b(quarto|chale|chalet|suite|habitacion|cuarto|room|cabana|casa|bangalo|bungalo|bungalow)( e| es| is| esta| ta)? "
     r"(todo |all )?(seu|sua|tuy[oa]|yours|de voces|de ustedes)\b", "não diga que a acomodação é do hóspede", True),
    (r"\b(separei|guardei|bloqueei|segurei|te guarde|i'?ve (saved|held|blocked)|we'?ve (saved|held|blocked))\b",
     "não diga que separou a acomodação", True),
    (r"\b(ja )?(anotei|agendei|marquei|te anote|los anote|apunte) (voces|voce|vcs|vc|vos|ustedes|tu nombre|seu nome|"
     r"o nome|a reserva|su reserva|tu reserva)\b", "não diga que a reserva está feita", True),
    (r"\b(ya|ja|already) (tenes|tienes|tiene|tem|have|got) (a |la |el |o |the |your |tu |su |seu |sua )?"
     r"(habitacion|cuarto|quarto|chale|chalet|suite|room|lugar|vaga|reserva)", "não diga que a reserva está feita", True),
    (r"\b(you'?re|you are) (all )?set\b|\ball set for\b|\btodo listo para (el|tu|su|vos)\b|"
     r"\btudo certo para (o dia|sua|a sua|voces|voce)\b", "não diga que a reserva está feita", True),
    # vaga / disponibilidade ("Sí" já chega como "sim": "Sí, hay lugar" é afirmação, não condição)
    (r"\b(sim|yes)[,!.]? " + _VERBOS_VAGA + r" (\w+ ){0,2}?" + _TERMO_VAGA + r"\b" + _ESTACIONAMENTO
     + r"|\bsi[,!.]? tenemos (\w+ ){0,2}?" + _TERMO_VAGA + r"\b" + _ESTACIONAMENTO
     + r"|\bsi[,.!] hay (\w+ ){0,2}?" + _TERMO_VAGA + r"\b" + _ESTACIONAMENTO,
     "não afirme que há vaga", False),
    (r"\b" + _VERBOS_VAGA + r" (sim |si |still |ainda |todavia )?(\w+ ){0,2}?" + _TERMO_VAGA + r"\b" + _ESTACIONAMENTO,
     "não afirme que há vaga", True),
    (r"\b(tem|tiene) (sim |si )?(\w+ )?(vagas?|disponibilidad|disponibilidade)\b" + _ESTACIONAMENTO,
     "não afirme que há vaga", True),
    (r"\b(livres?|disponivel|disponiveis|libres?|disponibles?|available|free|open) (nessas|nesses|nestas|neste|nesse|"
     r"nas|nos|para (essas|esses|as|os|suas|tus|las)|en (esas|tus|las|estas)|(on|for) (those|these|your|the)) "
     r"(datas|data|dias|noites|fechas|noches|dates|nights|periodo|feriado)", "não afirme que há vaga", True),
    # "tenemos bungalós disponibles con mínimo de 4 noches" (es-reveillon, 04/10): afirmou vaga sem ver a agenda
    # (só acomodação: "temos estacionamento disponível" é serviço do FAQ e passa)
    (r"\b(temos|tenemos|we have|we still have)( (\d+|alguns|algunos|algunas|some|several|varios|varias))?"
     r"( (bangalos?|bungalos?|bungalows?|quartos?|suites?|chales?|cabanas?|rooms?|habitacion\w*|acomodac\w*"
     r"|alojamientos?|unidades?|lugares|vagas?))? (disponive|disponible|available)", "não afirme que há vaga", False),
    # cobrança: o bot nunca tem dado de pagamento
    # chave Pix = e-mail, telefone, CPF/CNPJ ou chave aleatória; "a chave Pix é enviada pela Carla" pode
    (r"((chave|clave) pix|pix key)\s*(:|\b(e|es|eh|is)\b)\s*(\S*@|\+?\(?\d[\d ().-]{6,}|[0-9a-f]{8}-)",
     "não envie chave Pix", False),
    (r"\b(agencia|ag|conta|cuenta|account|acct|iban|cbu|cvu)\b\W{0,3}(n[o.]?|numero|number|corrente|ending( in)?|"
     r"terminad[ao] e[mn]|final)?\W{0,3}\d{3,}", "não envie dados bancários", False),
    (r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b|\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b", "não envie CPF/CNPJ", False),
    (r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "não envie chave Pix", False),
    (r"(https?://|www\.)\S*(pag|pay|checkout|cobr|stripe|mercadopago|mpago|picpay|pagseguro|asaas|sumup|infinitepay|"
     r"nubank|paypal|link\.)", "não envie link de pagamento", False),
    (r"\((os |as |los |las |the )?(donos?|donas?|duenos?|duenas?|owners?|proprietari|propietari)",
     "escreva o nome do dono sem rótulo entre parênteses", False),
]
# pousada que só reserva pelo site: ninguém confirma reserva pelo WhatsApp
# (o site/o e-mail confirmar é o caminho certo: "the website will confirm your booking by email" passa)
PROIBIDO_SO_SITE = [(r"(?<!site )(?<!sitio )(?<!website )(?<!e-mail )(?<!email )(?<!sistema )(?<!system )"
                     r"\b(confirmem|confirmen|confirmarao|confirmaran|(vai|vao|va a|van a|will) confirm)",
                     "a reserva é feita e confirmada só pelo site: não diga que alguém vai confirmar; mande o link")]
# palavras que só existem em português, numa resposta em espanhol/inglês (avaliação de 04/10: "Lá", "3 noites",
# "tarifa garantida")
PORTUGUES = re.compile(r"(?<!\w)(lá|você|vocês|não|também|então|até|obrigad[oa]s?|noites?|diárias?|garantida|pertinho"
                       r"|[eé]quipe)"
                       r"(?!\w)", re.IGNORECASE)
# "perto"/"cerca" qualificado só com a distância do guia ou da busca (número com m, km ou min)
PERTO = r"pertinho|bem perto|muito perto|a poucos minutos|cerquita|muy cerca|bien cerca|a pocos minutos|very close|close by"
DISTANCIA = r"\d+([.,]\d+)? ?(m|km|min|metros|minutos|minutes)\b"
# distância contada do centrinho em vez da pousada (avaliação de 04/10: "supermercados a 630 m y 1,4 km del
# centrinho", distâncias do guia, que são da pousada). Só passa a que o próprio FAQ escreve ("a 600 m do centrinho").
DISTANCIA_DO_CENTRO = (r"(?P<n>\d+(?:[.,]\d+)?) ?(?:m|km|metros|quilometros|kilometros)\b,? (?:de distancia )?"
                       r"(?:do|da|del|de la|desde el|desde la|from the|from) centr")
# prazo que só o dono sabe (Natribu's, 04/10: "alguém entra em contato em poucos minutos"). Com número ("5 min a pé")
# é distância do guia e não cai aqui.
PRAZO = (r"\b(em|en|in|within|dentro de) (poucos|alguns|uns|pocos|unos|algunos|a few|few) "
         r"(minutos|minutes|instantes|momentos|moments|horas|hours)\b|\bja ja\b|\bhoje mesmo\b|\bhoy mismo\b|right away"
         # fase 3, es-queja: "para que puedan atenderte de inmediato... responder enseguida"
         r"|\bde inmediato\b|\ben ?seguida\b|\bimediatamente\b|\binmediatamente\b|\bimmediately\b|\bo quanto antes\b"
         r"|\blo antes posible\b|\bas soon as possible\b")


def nome_do_dono(dono: str) -> str:
    """'Carla (donos)' -> 'Carla': o rótulo entre parênteses não vai para o hóspede."""
    return re.sub(r"\s*\([^()]*\)\s*$", "", dono or "").strip() or dono


def distancias_do_centro_do_faq(faq: list[dict]) -> frozenset[str]:
    """Distâncias "do centrinho/del centro" escritas no FAQ (ex.: Watu Kererê, "a 600 m do centrinho")."""
    return frozenset(m.group("n").replace(".", ",") for m in re.finditer(DISTANCIA_DO_CENTRO, _sem_acento(texto_faq(faq))))


# dia da semana junto de uma data ("sábado 10/10", "Friday, October 9", "11 de outubro (domingo)"): o Haiku erra
# a conta (Natribu's, 04/10: "this weekend (October 4–6)" num domingo). Conferido contra o calendário de verdade.
DIAS_SEMANA = {"segunda": 0, "terca": 1, "quarta": 2, "quinta": 3, "sexta": 4, "sabado": 5, "domingo": 6,
               "lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4,
               "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
MESES = {"janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
         "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
         "enero": 1, "febrero": 2, "marzo": 3, "mayo": 5, "junio": 6, "julio": 7, "septiembre": 9, "setiembre": 9,
         "octubre": 10, "noviembre": 11, "diciembre": 12,
         "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
         "september": 9, "october": 10, "november": 11, "december": 12,
         # abreviações em inglês ("Dec 1-4", "Jan 5"); "mar" fica de fora de propósito (palavra comum em pt/es)
         "jan": 1, "feb": 2, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}
NOMES_DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
_DIA = r"(?P<dia_semana>" + "|".join(DIAS_SEMANA) + r")(?:-feira)?"
_MES = "|".join(sorted(MESES, key=len, reverse=True))
_DATA = (r"(?:(?P<d1>\d{1,2})/(?P<m1>\d{1,2})(?:/(?P<a1>\d{2,4}))?"
         r"|(?P<d2>\d{1,2})(?:st|nd|rd|th)?(?: de | of | )(?P<m2>" + _MES + r")(?:,? (?:de )?(?P<a2>\d{4}))?"
         r"|(?P<m3>" + _MES + r") (?P<d3>\d{1,2})(?:st|nd|rd|th)?(?:,? (?P<a3>\d{4}))?)")
DIA_E_DATA = [re.compile(_DIA + r",? (?:\(|dia |el |the )?" + _DATA + r"(?!\d)"),
              # "11 de outubro (sábado)", "25/12, que cai numa quinta", "March 12, which is a Friday", "el 5 es viernes";
              # "domingo 03/01, segunda 04/01" é lista (a 1ª forma já casou 03/01 com domingo) e não entra aqui
              re.compile(r"(?<![\d/])" + _DATA + r"(?:,? \(|,? (?:que |which )?(?:cai|caiu|cae|es|is|falls on|e uma?|es un)"
                         r"(?: num[ao]| n[ao]| uma?| an?| en| on)? )" + _DIA)]


# dia da semana com só o número do dia: "quinta (20)", "sexta, dia 21", "20 (quinta)", "Thursday (20th)"
DIA_E_NUMERO = [re.compile(_DIA + r",? (?:\((?P<dn>\d{1,2})(?:st|nd|rd|th)?\)|dia (?P<dn2>\d{1,2})(?![\d/:h]))"),
                re.compile(r"(?<![\d/.,])(?P<dn>\d{1,2}) \(" + _DIA + r"\)")]
_MES_SOLTO = re.compile(r"\b(" + _MES + r")\b|\b\d{1,2}/(?P<mn>\d{1,2})\b")


def _candidatas(dia: int, mes: int, ano: str | int | None, hoje: date) -> list[date]:
    anos = [int(ano) + (2000 if len(str(ano)) == 2 else 0)] if ano else [hoje.year - 1, hoje.year, hoje.year + 1]
    datas = []
    for a in anos:
        try:
            datas.append(date(a, mes, dia))
        except ValueError:
            pass
    return datas if ano else [d for d in datas if -62 <= (d - hoje).days <= 400]


def _motivo_dia_errado(datas: list[date], dia_semana: str, hoje: date) -> str | None:
    if not datas or any(d.weekday() == DIAS_SEMANA[dia_semana] for d in datas):
        return None
    d = min(datas, key=lambda d: abs((d - hoje).days))
    return (f"dia da semana errado: {d:%d/%m/%Y} é {NOMES_DIAS[d.weekday()]}, não {dia_semana}; "
            "use a ferramenta noites_da_estadia ou o calendário das instruções")


def conferir_dias_da_semana(mensagem: str, hoje: date, datas_conhecidas: frozenset[date] = frozenset()) -> str | None:
    """Motivo, se a mensagem junta um dia da semana com uma data em que ele não cai. Sem ano, vale qualquer
    ocorrência de ~2 meses atrás até ~1 ano à frente (a pessoa pode falar de uma data que já passou). Dia da semana
    com só o número ("quinta (20)") é conferido pelo mês citado na mesma frase (ou na mensagem, se for um só) e
    pelas datas que a ferramenta noites_da_estadia já devolveu nesta conversa (`datas_conhecidas`)."""
    t = _sem_acento(mensagem)
    for frase in re.split(_FIM_DE_FRASE, t):
        pares = [(m.groupdict()["dia_semana"], int(m.groupdict().get("dn") or m.groupdict().get("dn2")))
                 for p in DIA_E_NUMERO for m in p.finditer(frase)]
        if not pares:
            continue
        meses = {int(m.group("mn")) if m.group("mn") else MESES[m.group(1)] for m in _MES_SOLTO.finditer(frase)}
        if not meses:
            meses = {int(m.group("mn")) if m.group("mn") else MESES[m.group(1)] for m in _MES_SOLTO.finditer(t)}
        achado_ano = re.search(r"\b(20\d{2})\b", frase)
        ano = achado_ano.group(1) if achado_ano else None
        for dia_semana, dn in pares:
            conhecidas = [d for d in datas_conhecidas if d.day == dn]
            if conhecidas:
                datas = conhecidas
            elif len(meses) == 1:
                datas = _candidatas(dn, next(iter(meses)), ano, hoje)
            else:
                continue
            if motivo := _motivo_dia_errado(datas, dia_semana, hoje):
                return motivo
    ja_casadas: list[tuple[int, int]] = []   # datas que a 1ª forma já juntou com o dia da semana certo
    for padrao in DIA_E_DATA:
        for m in padrao.finditer(t):
            if padrao is DIA_E_DATA[1] and any(a <= m.start() < b for a, b in ja_casadas):
                continue
            ja_casadas.append(m.span())
            g = m.groupdict()
            dia = int(g["d1"] or g["d2"] or g["d3"])
            mes = int(g["m1"]) if g["m1"] else MESES[g["m2"] or g["m3"]]
            if motivo := _motivo_dia_errado(_candidatas(dia, mes, g["a1"] or g["a2"] or g["a3"], hoje), g["dia_semana"], hoje):
                return motivo
    return None


def _datas_citadas(t: str) -> list[tuple[int, int, int | None]]:
    """(dia, mês, ano ou None) de cada data escrita no texto sem acento ("20/11", "20 de novembro de 2026", "Nov 20")."""
    datas = []
    for m in re.finditer(_DATA, t):
        g = m.groupdict()
        dia = int(g["d1"] or g["d2"] or g["d3"])
        mes = int(g["m1"]) if g["m1"] else MESES[g["m2"] or g["m3"]]
        ano = g["a1"] or g["a2"] or g["a3"]
        datas.append((dia, mes, int(ano) + (2000 if len(ano) == 2 else 0) if ano else None))
    return datas


# "o Réveillon (26/12 a 05/01) já passou" em outubro (QA do Refúgio, es01): data futura dada como passada
_JA_PASSOU = re.compile(r"\b(ja (passou|passaram|aconteceu|terminou|acabou)|ya (paso|pasaron|termino|acabo)|"
                        r"(already|has|have) passed|already (happened|over)|is (already )?over|was over)\b")


def conferir_data_passada(mensagem: str, hoje: date) -> str | None:
    """Motivo, se uma frase diz que "já passou" uma data que ainda vai acontecer (com ano, ou sem ano ainda neste ano)."""
    for frase in re.split(_FIM_DE_FRASE, _sem_acento(mensagem)):
        if not _JA_PASSOU.search(frase):
            continue
        for dia, mes, ano in _datas_citadas(frase):
            try:
                d = date(ano or hoje.year, mes, dia)
            except ValueError:
                continue
            if d >= hoje:
                return (f"{d:%d/%m/%Y} ainda não passou (hoje é {hoje:%d/%m/%Y}); não diga que uma data futura já "
                        "passou; use o calendário das instruções")
    return None


MESES_ABREV = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


def calendario_meses(hoje: date, meses: int = 13) -> str:
    """Sextas, sábados e domingos de cada um dos próximos meses, uma linha por mês. Com só 21 dias o Haiku chutava
    o resto (QA do Refúgio: 20/11/2026 virou "quarta"; é sexta) e errava a tarifa de fim de semana."""
    linhas, ano, mes = [], hoje.year, hoje.month
    for _ in range(meses):
        por_dia = {4: [], 5: [], 6: []}
        d = date(ano, mes, 1)
        while d.month == mes:
            if d.weekday() in por_dia:
                por_dia[d.weekday()].append(str(d.day))
            d += timedelta(1)
        linhas.append(f"{MESES_ABREV[mes - 1]}/{ano}: sex {', '.join(por_dia[4])} · sáb {', '.join(por_dia[5])} · "
                      f"dom {', '.join(por_dia[6])}")
        ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    return "\n".join(linhas)


def calendario(hoje: date, dias: int = 21) -> str:
    """Os próximos dias com o dia da semana e o fim de semana mais próximo, para o modelo não fazer conta de data;
    depois, os fins de semana de cada um dos próximos 13 meses (qualquer data de reserva cai nessa tabela)."""
    dias_txt = "; ".join(f"{NOMES_DIAS[d.weekday()][:3]} {d:%d/%m}" for d in (hoje + timedelta(n) for n in range(dias)))
    sexta = hoje - timedelta(hoje.weekday() - 4) if hoje.weekday() in (4, 5) else hoje + timedelta((4 - hoje.weekday()) % 7)
    domingo = sexta + timedelta(2)
    return (f"Calendário (use para dia da semana e contas de datas): {dias_txt}. Fim de semana mais próximo: sexta "
            f"{sexta:%d/%m} a domingo {domingo:%d/%m} (noites de sexta e sábado = 2 noites)."
            + (" Hoje já é domingo: se o hóspede disser 'este fim de semana', pergunte as datas." if hoje.weekday() == 6 else "")
            + f"\nSextas, sábados e domingos dos próximos 13 meses (todo outro dia é de segunda a quinta; para o dia "
              f"exato, conte a partir do domingo mais perto). Tudo o que está nesta tabela ainda vai acontecer, "
              f"inclusive dezembro de {hoje.year} e o ano de {hoje.year + 1}; só datas anteriores a {hoje:%d/%m/%Y} "
              f"já passaram:\n{calendario_meses(hoje)}")


# Desconto: barrado, salvo o percentual que o próprio FAQ anuncia (ex.: "Pix com 5% de desconto"). O grupo `pct` só
# existe quando o número é um percentual; desconto negado ("não damos 10% de desconto") passa, com condição
# ("15% menos si pagás en efectivo") continua sendo desconto.
_PERCENT = r" ?(?:%|por ?cento|por ciento|percent)"
DESCONTO = [r"(?P<pct>\d+)" + _PERCENT + r"\s*(?:de |of )?(?:desconto|descuento|off|discount)",
            r"(?P<pct>\d+)" + _PERCENT + r" (?:a )?(?:menos|less|mais barato|mas barato|cheaper|abaixo)",
            r"(?:desconto|descuento|discount|rebaja|abatimento) (?:de|del|of|do) "
            r"(?:(?P<pct>\d+) ?%|(?:r\$|us\$|\$)? ?" + _EXTENSO + r")",
            r"\b[a-z]+ (?:por ?cento|por ciento|percent) (?:de |of )?(?:desconto|descuento|off|discount)",
            r"(?:r\$|us\$|\$) ?\d[\d.,]* (?:em vez de|en vez de|en lugar de|instead of|ao inves de|no lugar de)",
            r"\b(?:knock|take|shave) (?:r\$|\$)? ?\d+ off\b|\b\d+ off (?:the|your) (?:total|price|rate|bill)"]
PRECO_ESPECIAL = [
    r"\b(?:te |lhe |le )?(?:faco|fazemos|deixo|deixamos|hago|hacemos|dejo|dejamos)(?: por| a| pra| en| em)? (?:r\$|us\$|\$)"
    r"|\b(?:i|we) can do it for\b|\b(?:i'?ll|we'?ll) do it for\b",
    # "condição especial" fica de fora: aparece em recusa/encaminhamento ("a equipe vê qualquer condição especial")
    r"\b(?:preco|precio|tarifa|valor)(?: bem| muy| um| una)? (?:especial|promocional|camarada|diferenciad)"
    r"|\bspecial (?:price|rate|deal|discount)|\bprecinho"]

# Contato ou link: só pode sair se estiver no FAQ, no guia ou na busca (o bot não inventa e-mail, telefone ou site).
_EMAIL = re.compile(r"[a-z0-9._%+-]+@[a-z0-9-]+\.[a-z.]+")
# sobre o texto original em minúsculas (sem tirar acento: "site.com.br—just" não pode virar "site.com.brjust")
_URL = re.compile(r"(https?://|www\.)[a-z0-9./?=&%#:+~_-]+"
                  r"|\b[a-z0-9-]+\.(com|net|org|ae|me|ly|link|app|io|page|site)(\.br|\.ar|\.uy)?(/[a-z0-9./?=&%#:+~_-]*)?")
_TELEFONE = re.compile(r"\+?\d[\d ().-]{8,}\d")
_DADO_PAGAMENTO = re.compile(r"[a-z0-9._%+-]+@[a-z0-9-]+\.[a-z]|\d[\d ().-]{8,}\d")
_FIM_DE_FRASE = r"[!?;\n]|\.(?:\s|$)"   # o ponto de "R$ 1.200" não fecha a frase


def _ressalvado(t: str, inicio: int, fim: int, so_negacao: bool = False) -> bool:
    """O trecho proibido está negado ou condicionado ("não confirmamos", "se tiver vaga", "depois do sinal")?"""
    frase = re.split(r"[!?;:\n]|\.(?:\s|$)", t[:inicio])[-1]
    partes = frase.split(",")
    atual = partes[-1]
    palavras = re.findall(r"[a-z']+", atual)
    if palavras and (palavras[-1] == "no" or set(palavras[-3:]) & _NEGACAO):  # "no" só colado: em PT é "em + o"
        return True
    if so_negacao:  # desconto com condição ("15% menos si pagás en efectivo") continua sendo desconto
        return False
    if _HIPOTESE.search(atual) or (_TEMPORAL.search(atual) and _FALA_DA_CONFIRMACAO.search(atual)):
        return True
    if len(partes) > 1 and len(re.findall(r"[a-z']+", partes[-2])) >= 3:   # "Sí, hay lugar" não conta
        anterior = partes[-2].strip()
        if _INICIO_HIPOTESE.match(anterior) or (_INICIO_TEMPORAL.match(anterior) and _FALA_DA_CONFIRMACAO.search(anterior)):
            return True  # "Depois que a Carla confirmar, a reserva fica garantida"
    depois = re.split(_FIM_DE_FRASE, t[fim:])[0]
    return bool(_CONDICAO_DEPOIS.match(depois) and _FALA_DA_CONFIRMACAO.search(depois))


def descontos_do_faq(faq: list[dict]) -> frozenset[str]:
    """Percentuais de desconto escritos no FAQ da pousada: o bot pode repeti-los."""
    t = _sem_acento(texto_faq(faq))
    return frozenset(m.group("pct") for p in DESCONTO for m in re.finditer(p, t) if m.groupdict().get("pct"))


def _checar_desconto(t: str, descontos_permitidos: frozenset[str]) -> str | None:
    for padrao in DESCONTO:
        for m in re.finditer(padrao, t):
            if m.groupdict().get("pct") in descontos_permitidos or _ressalvado(t, m.start(), m.end(), so_negacao=True):
                continue
            return "não ofereça desconto"
    for padrao in PRECO_ESPECIAL:
        for m in re.finditer(padrao, t):
            if not _ressalvado(t, m.start(), m.end(), so_negacao=True):
                return "não ofereça preço diferente do FAQ nem condição especial"
    return None


def _checar_contato(t: str, permitidos: str) -> str | None:
    """E-mail, link ou telefone que não está no FAQ/guia/busca (o dono é quem manda os dados)."""
    faq = (permitidos or "").lower()
    digitos_faq = re.sub(r"\D", "", faq)
    for achado in _EMAIL.finditer(t):
        if achado.group(0).rstrip(".") not in faq:
            return "não envie e-mail que não está no FAQ (o dono envia os dados)"
    for achado in _URL.finditer(t):
        # "*https://www.site.com.br*," vale como o link do FAQ; com ou sem https:// e www.
        link = re.sub(r"^(https?://)?(www\.)?", "", re.sub(r"[^\w/]+$", "", achado.group(0)))
        if link not in faq:
            return "não envie link que não está no FAQ"
    for achado in _TELEFONE.finditer(t):
        digitos = re.sub(r"\D", "", achado.group(0))
        if len(digitos) >= 10 and digitos not in digitos_faq:
            return "não envie telefone ou número de documento que não está no FAQ"
    return None


# Valores em reais escritos no texto: "R$ 1.600", "R$ 1,600" (en), "R$ 750,00", "700 reais"; centavos ignorados.
_VALOR_REAIS = re.compile(r"r\$ ?(\d[\d.,]*\d|\d)|(?<![\d.,])(\d[\d.,]*\d|\d) (?:reais|reales|brl)\b")


def valores_em_reais(t: str) -> set[int]:
    valores = set()
    for m in _VALOR_REAIS.finditer(t):
        bruto = m.group(1) or m.group(2)
        if re.fullmatch(r"\d{1,3}([.,]\d{3})+", bruto):            # 1.600 / 1,600 = mil e seiscentos
            n = int(re.sub(r"[.,]", "", bruto))
        elif re.fullmatch(r"\d{1,3}([.,]\d{3})+[.,]\d{2}", bruto):   # 1.600,00
            n = int(re.sub(r"[.,]", "", bruto[:-3]))
        elif re.fullmatch(r"\d+[.,]\d{1,2}", bruto):                 # 750,00 / 750.5
            n = int(re.split(r"[.,]", bruto)[0])
        else:
            n = int(re.sub(r"\D", "", bruto) or 0)
        valores.add(n)
    return valores


def _checar_valores(t: str, permitidos: str) -> str | None:
    """Valor em reais que não está no FAQ, no guia nem na busca: o modelo somou um total ou inventou a diária (QA do
    Refúgio, 09/10: "R$ 2.100", "R$ 5.700", "R$ 950 em 6 noites"). O bot só repete valores do FAQ; o total é do dono."""
    novos = sorted(valores_em_reais(t) - valores_em_reais(_sem_acento(permitidos)))
    if novos:
        return (f"o valor R$ {novos[0]} não está no FAQ: não some o total da estadia nem invente valor; diga a diária "
                "de cada noite ou período e as taxas exatamente como estão no FAQ e que a pousada confirma o valor final")
    return None


def checar_mensagem(mensagem: str, descontos_permitidos: frozenset[str] = frozenset(), *, idioma: str = "pt",
                    so_site: bool = False, hoje: date | None = None,
                    distancias_do_centro: frozenset[str] = frozenset(), permitidos: str | None = None,
                    datas_conhecidas: frozenset[date] = frozenset()) -> str | None:
    """Motivo da recusa, ou None se a mensagem pode sair. so_site: pousada que só reserva pelo site; hoje: confere
    dia da semana junto de data; distancias_do_centro: as que o FAQ escreve ("600 m do centrinho"); permitidos:
    texto do FAQ/guia/busca (e-mail, telefone e link que estão nele podem sair; None = não confere contato)."""
    t = _sem_acento(mensagem)
    ts = _sem_acento(re.sub(r"(?i)\bsí\b", "sim", mensagem or ""))   # "Sí hay lugar" é sim (yes), não "si" (se)
    for padrao, motivo, contexto in PROIBIDO + [(p, m, False) for p, m in PROIBIDO_SO_SITE if so_site]:
        for achado in re.finditer(padrao, ts):
            if contexto and _ressalvado(ts, achado.start(), achado.end(), contexto == "negacao"):
                continue
            return motivo
    for frase in re.split(_FIM_DE_FRASE, ts):   # "Pix: 4899..." e "pix é carla@pousada.com" na mesma frase
        if re.search(r"\b(pix|chave|clave)\b", frase) and _DADO_PAGAMENTO.search(frase):
            return "não envie chave Pix"
        if re.search(r"\b(pix|chave|clave)\b", frase) and sum(w in _NUMERO_EXTENSO for w in re.findall(r"[a-z]+", frase)) >= 5:
            return "não envie chave Pix (nem por extenso)"
    if re.search(PERTO, t) and not re.search(DISTANCIA, t):
        return "só diga que é perto com a distância do guia ou da busca; sem distância, diga só onde fica"
    for m in re.finditer(DISTANCIA_DO_CENTRO, t):
        if m.group("n").replace(".", ",") not in distancias_do_centro:
            return ("as distâncias do guia são a partir da pousada, não do centrinho/centro: escreva \"da pousada\" "
                    "(ou só a distância)")
    if re.search(PRAZO, t):
        return "não prometa prazo de resposta (você não sabe quando a pousada vai ver); diga só que a resposta chega em breve"
    if hoje and (motivo := conferir_dias_da_semana(mensagem, hoje, datas_conhecidas) or conferir_data_passada(mensagem, hoje)):
        return motivo
    if idioma in ("es", "en") and (achadas := PORTUGUES.findall(mensagem or "")):
        return (f"a resposta está em {IDIOMAS[idioma]}, mas tem palavra em português "
                f"({', '.join(sorted(set(p.lower() for p in achadas)))}): escreva tudo em {IDIOMAS[idioma]}")
    if motivo := _checar_desconto(ts, descontos_permitidos):
        return motivo
    if permitidos is not None and (motivo := _checar_contato((mensagem or "").lower(), permitidos)):
        return motivo
    if permitidos is not None and (motivo := _checar_valores(t, permitidos)):
        return motivo
    return None


# "Vou passar para o dono" com acao='responder': o dono não recebe aviso nenhum (QA do Refúgio: pt07, pt45,
# pt59 com Pix pago, es08). Frase que pede um dado ("me conta as datas que eu passo o pedido") ou pergunta não conta.
_PROMETE_ENCAMINHAR = re.compile(
    r"\b(ja )?(passei|repassei|encaminhei|avisei|transmiti)\b|\b(vou|(me )?deixa( eu)?|deixe eu|deixa que eu|melhor eu) (passar|encaminhar|avisar|repassar)\b"
    r"|\b(melhor )?(eu )?passo (para|pra|isso|essa|esse|sua|seu|a sua|o seu|a mensagem|o recado)\b"
    r"|\b(ya )?(pase|le pase|se lo pase|le avise|le aviso)\b|\bvoy a (pasar|avisar|transmitir)\b|\bte paso con\b"
    r"|\bi('ve| have| just)? (passed|forwarded|relayed)\b|\bi('ll| will) (pass|forward|relay|let)\b|\bpassing (this|it|your)\b")
_PEDE_DADO = re.compile(r"\bme (conta|contar|passa|passar|diz|diga|dizer|manda|mandar|informa|informar|fala|falar|avisa|avisar)\b|\b(contame|pasame|decime|avisame|cuentame)\b"
                        r"|\bme (decis|dices|pasas|contas|avisas)\b|\b(tell|send|give) me\b|\blet me know\b|\?")


def promete_encaminhar(mensagem: str) -> bool:
    """A mensagem afirma que o assunto foi (ou vai ser) passado ao dono, sem condicionar a um dado que ainda falta."""
    for frase in re.split(_FIM_DE_FRASE, _sem_acento(mensagem)):
        if _PROMETE_ENCAMINHAR.search(frase) and not _PEDE_DADO.search(frase):
            return True
    return False


_VALOR = re.compile(r"(r\$|us\$|u\$s|\busd\b|\$ ?\d|\d+ ?%|\breais\b|\bpesos\b|\bdolares\b|\bdollars\b)")


def com_ressalva(mensagem: str, idioma: str) -> str:
    """Resposta com valor em dinheiro termina com a ressalva no idioma do hóspede (regra do produto, em código)."""
    t = _sem_acento(mensagem)
    if not _VALOR.search(t) or any(_sem_acento(r) in t for r in RESSALVA.values()):
        return mensagem
    return f"{mensagem.rstrip()}\n{RESSALVA.get(idioma, RESSALVA['pt'])}"


MAX_PESSOAS = 8          # acima disso é grupo grande: o dono trata
MAX_NOITES = 60
HORIZONTE_DIAS = 730     # pedido para mais de 2 anos à frente é erro de data

# nome que não é nome: o formulário do site chega sem o campo e o modelo preenchia "<UNKNOWN>" (QA de 09/10, site01)
_NOME_PLACEHOLDER = re.compile(
    r"^[\W_]*(unknown|desconhecid[oa]|desconocid[oa]|n/?a|none|null|nulo|nil|undefined|sem nome|sin nombre|no name|"
    r"nome|nombre|name|hospede|huesped|guest|cliente|customer|client|visitante|pessoa|persona|person|usuario|user|"
    r"anonimo|anonymous|-+|\?+|\.+)(\s*\((sem nome|sin nombre|no name|unknown)\))?[\W_]*$")


def nome_de_hospede_valido(nome: str) -> bool:
    """Tem ao menos duas letras e não é placeholder ("<UNKNOWN>", "hóspede", "n/a", "-", só símbolos)."""
    limpo = _sem_acento(nome).strip()
    return bool(limpo) and not _NOME_PLACEHOLDER.match(limpo) and bool(re.search(r"[a-z]{2,}", limpo))


def validar_pedido(e: dict, hoje: date) -> tuple[dict | None, str | None]:
    try:
        entrada, saida = date.fromisoformat(str(e.get("entrada"))), date.fromisoformat(str(e.get("saida")))
    except ValueError:
        return None, "datas no formato AAAA-MM-DD; pergunte as datas exatas ao hóspede"
    if saida <= entrada:
        return None, "a saída precisa ser depois da entrada"
    if entrada < hoje:
        return None, "a data de entrada já passou; confirme as datas com o hóspede"
    if saida - entrada > timedelta(days=MAX_NOITES):
        return None, f"mais de {MAX_NOITES} noites: confirme as datas com o hóspede"
    if entrada > hoje + timedelta(days=HORIZONTE_DIAS):
        return None, "data de entrada muito distante; confirme o ano com o hóspede"
    adultos = e.get("adultos")
    if isinstance(adultos, bool) or not isinstance(adultos, int) or adultos < 1:
        return None, "informe quantos adultos (número inteiro)"
    nome = str(e.get("nome") or e.get("nombre") or e.get("name") or "").strip()  # o modelo às vezes traduz a chave
    if not nome_de_hospede_valido(nome):
        return None, ("nome do hóspede ausente ou inválido (placeholder como <UNKNOWN>, 'hóspede' ou 'n/a' não vale): "
                      "pergunte o nome antes de registrar, sem dizer que passou o pedido")
    idades = []
    for i in e.get("criancas_idades") or []:   # "4" (texto) vale; antes sumia calada
        try:
            idade = None if isinstance(i, bool) else int(str(i).strip())
        except ValueError:
            idade = None
        if idade is None or not 0 <= idade < 18:
            return None, f"idade de criança inválida ({i!r}): informe a idade em anos, de 0 a 17"
        idades.append(idade)
    return {"entrada": entrada.isoformat(), "saida": saida.isoformat(), "noites": (saida - entrada).days,
            "adultos": adultos, "criancas_idades": idades, "nome": nome[:80],
            "grupo_grande": adultos + len(idades) > MAX_PESSOAS,
            "contato": str(e.get("contato") or "").strip()[:80], "acomodacao": str(e.get("acomodacao") or "").strip()[:80],
            "observacoes": str(e.get("observacoes") or "").strip()[:300]}, None


# ---------------------------------------------------------------------
# Laço
# ---------------------------------------------------------------------
@dataclass
class ResultadoPousada:
    acao: str
    mensagem: str
    idioma: str = "pt"
    motivo: str = ""
    resumo_para_dono: str = ""
    pedido: dict | None = None          # pedido de reserva registrado nesta mensagem
    passos: int = 0
    tokens_entrada: int = 0
    tokens_saida: int = 0
    fallback: bool = False


def atender_hospede(mensagem: str, pousada: PousadaInfo, *, hospede: dict | None = None,
                    historico: list[dict] | None = None, cliente: anthropic.Anthropic | None = None,
                    modelo: str | None = None, agora: datetime | None = None,
                    buscar_lugares: Callable[[str, str, str], dict] | None = None) -> ResultadoPousada:
    """hospede: o que já se sabe ({"nome", "idioma"}); historico: mensagens anteriores no formato da API.
    buscar_lugares(categoria, idioma, termo): consulta ao Google Maps (bot/lugares.py); None = só o guia local."""
    dono = nome_do_dono(pousada.dono)
    descontos = descontos_do_faq(pousada.faq)  # o FAQ pode anunciar um desconto (ex.: Pix); o bot pode repetir
    do_centro = distancias_do_centro_do_faq(pousada.faq)
    ferramentas = [f for f in FERRAMENTAS if not (pousada.reserva_url and f["name"] == "registrar_pedido_reserva")]
    if buscar_lugares:
        ferramentas = [FERRAMENTA_LUGARES, *ferramentas]
    secao_reserva = (RESERVA_PELO_SITE.format(pousada=pousada.nome, reserva_url=pousada.reserva_url, dono=dono)
                     if pousada.reserva_url else RESERVA_COM_PEDIDO.format(dono=dono))
    disponibilidade = (f"Disponibilidade e valor exato da data: no site {pousada.reserva_url}." if pousada.reserva_url
                       else f"Diga que {dono} confirma a disponibilidade.")
    agora = agora or datetime.now(ZoneInfo(FUSO_PADRAO))
    cliente = cliente or anthropic.Anthropic()
    modelo = modelo or os.environ.get("MODELO_BOT") or MODELO_PADRAO
    dias = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
    sabido = {k: v for k, v in (hospede or {}).items() if v}
    system = [
        {"type": "text", "text": INSTRUCOES.format(pousada=pousada.nome, cidade=pousada.cidade or "Santa Catarina",
                                                   dono=dono, faq=texto_faq(pousada.faq),
                                                   secao_reserva=secao_reserva, disponibilidade=disponibilidade,
                                                   guia=texto_guia(pousada.faq),
                                                   mapa=(f"Mapa com todos os lugares e o caminho a partir da pousada: {pousada.mapa_url} "
                                                         "(mande o link quando indicar lugares e também quando perguntarem onde fica a pousada, a localização ou como chegar)") if pousada.mapa_url
                                                   else "Não há mapa da região para enviar."),
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"Hoje é {dias[agora.isoweekday() - 1]}, {agora:%d/%m/%Y}, {agora:%H:%M} (horário de Brasília). "
                                 f"{calendario(agora.date())}\n"
                                 f"O que já sabemos deste hóspede: {json.dumps(sabido, ensure_ascii=False) if sabido else 'nada ainda'}."},
    ]
    mensagens = [*(historico or []), {"role": "user", "content": mensagem}]
    # idioma já conhecido do hóspede: o encaminhamento automático sai nele (e não apaga o idioma gravado)
    idioma_conhecido = sabido.get("idioma") if sabido.get("idioma") in IDIOMAS else None
    r = ResultadoPousada(acao="encaminhar", mensagem="", idioma=idioma_conhecido or "pt")
    # e-mail, telefone e link só saem se estiverem no FAQ, no guia, no link do mapa/site ou no que a busca trouxe
    permitidos = [texto_faq(pousada.faq), texto_guia(pousada.faq), pousada.mapa_url, pousada.reserva_url]
    datas_conhecidas: set[date] = set()   # noites que noites_da_estadia devolveu: a trava confere "quinta (20)" por elas
    temporadas = temporadas_do_faq(pousada.faq)

    idioma_tentado: list[str] = []   # idioma das respostas recusadas: o fallback sai nele

    def encaminhar_automatico(motivo: str) -> ResultadoPousada:
        r.motivo, r.fallback = motivo, True
        idioma = idioma_conhecido or (idioma_tentado[-1] if idioma_tentado else None)
        if r.pedido:   # o pedido já foi registrado e avisado: a resposta fixa diz só isso
            r.acao = "responder"
            r.mensagem = "\n".join(t.format(dono=dono_na_frase(dono, i)) for i, t in PEDIDO_PASSADO.items()
                                   if idioma is None or i == idioma)
            log.warning("pousada: pedido registrado, resposta fixa (%s)", motivo)
            return r
        r.acao = "encaminhar"
        r.mensagem = mensagem_encaminhamento(dono, idioma)
        r.resumo_para_dono = f"O atendimento virtual não conseguiu responder: \"{mensagem[:160]}\""
        log.warning("pousada: encaminhamento automático: %s", motivo)
        return r

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
        registro_falhou = False
        # o registro do pedido roda antes da resposta, mesmo que o modelo mande as duas fora de ordem no mesmo turno:
        # senão a resposta "passei seu pedido" sai e o pedido se perde
        for c in sorted(chamadas, key=lambda c: c.name != "registrar_pedido_reserva"):
            e = c.input if isinstance(c.input, dict) else {}
            erro = False
            if c.name == "enviar_resposta":
                problema = None
                # respondendo em espanhol/inglês, o modelo às vezes traduz a chave (mensaje/message)
                e = {**e, "mensagem": e.get("mensagem") or e.get("mensaje") or e.get("message") or ""}
                if registro_falhou:
                    problema = ("o pedido NÃO foi registrado (veja o erro de registrar_pedido_reserva); corrija o "
                                "registro ou pergunte o que falta, sem dizer que passou o pedido")
                elif e.get("acao") not in ("responder", "encaminhar"):
                    problema = "acao precisa ser 'responder' ou 'encaminhar'"
                elif not str(e["mensagem"]).strip():
                    problema = ("o campo `mensagem` está vazio; o nome da chave é sempre `mensagem` (em português), "
                                "em qualquer idioma da resposta")
                elif e["acao"] == "encaminhar" and not str(e.get("resumo_para_dono") or "").strip():
                    problema = "ao encaminhar, preencha resumo_para_dono"
                elif e["acao"] == "responder" and r.pedido is None and promete_encaminhar(e["mensagem"]):
                    problema = (f"a mensagem diz que passou o assunto para {dono}, mas acao='responder' (assim ninguém "
                                f"avisa {dono}): use acao='encaminhar' com resumo_para_dono, ou tire a promessa da mensagem")
                else:
                    problema = checar_mensagem(e["mensagem"], descontos, idioma=e.get("idioma") or "pt",
                                               so_site=bool(pousada.reserva_url), hoje=agora.date(),
                                               distancias_do_centro=do_centro, permitidos="\n".join(permitidos),
                                               datas_conhecidas=frozenset(datas_conhecidas))
                if problema is None:
                    r.acao = e["acao"]
                    r.idioma = e.get("idioma") if e.get("idioma") in IDIOMAS else r.idioma
                    # ressalva de valores em código: o prompt pede, mas o modelo às vezes esquece
                    r.mensagem = com_ressalva(formatar_whatsapp(e["mensagem"].strip()), r.idioma)
                    r.motivo = str(e.get("motivo") or "").strip()
                    r.resumo_para_dono = str(e.get("resumo_para_dono") or "").strip()
                    return r
                log.info("pousada: resposta recusada (%s)", problema)
                if e.get("idioma") in IDIOMAS:
                    idioma_tentado.append(e["idioma"])
                saida, erro = {"erro": f"Resposta inválida: {problema}. Reescreva e chame enviar_resposta de novo."}, True
            elif c.name == "registrar_pedido_reserva" and not pousada.reserva_url:
                pedido, problema = validar_pedido(e, agora.date())
                if problema:
                    registro_falhou = True
                    saida, erro = {"registrado": False, "erro": problema}, True
                else:
                    r.pedido = pedido
                    saida = {"registrado": True, "aviso": "Pedido enviado ao dono. Não está confirmado: diga isso ao hóspede."}
                    if pedido["grupo_grande"]:
                        saida["aviso"] += f" Grupo com mais de {MAX_PESSOAS} pessoas: diga que {dono} combina os detalhes."
            elif c.name == "noites_da_estadia":
                saida = noites_da_estadia(e, agora.date(), temporadas)
                erro = "erro" in saida
                if not erro:
                    datas_conhecidas.update(date.fromisoformat(str(e["entrada"])) + timedelta(n)
                                            for n in range(saida["noites"] + 1))
            elif c.name == "buscar_lugares" and buscar_lugares:
                try:
                    saida = buscar_lugares(str(e.get("categoria") or ""), sabido.get("idioma") or "pt",
                                           str(e.get("termo") or ""))
                except Exception:
                    log.exception("pousada: buscar_lugares falhou")
                    saida = {"erro": "busca de lugares indisponível agora; use o guia local"}
                erro = "erro" in saida
                permitidos.append(json.dumps(saida, ensure_ascii=False))
            else:
                saida, erro = {"erro": f"ferramenta desconhecida: {c.name}"}, True
            resultados[c.id] = {"type": "tool_result", "tool_use_id": c.id,
                                "content": json.dumps(saida, ensure_ascii=False), "is_error": erro}
        mensagens.append({"role": "user", "content": [resultados[c.id] for c in chamadas]})  # na ordem do modelo
    return encaminhar_automatico(f"sem resposta final em {MAX_PASSOS} passos")
