"""Avaliação do bot da pousada com a IA de verdade: 32 perguntas (16 PT, 13 ES, 3 EN) contra o FAQ da Exemplo do Rosa.

    python scripts/avaliar_pousada.py [--rotulo depois] [--so pt-pet,es-pix]
    python scripts/avaliar_pousada.py --faq outra.json [--reserva-url https://...] --generico   # 10 perguntas

--generico usa 10 perguntas que servem para qualquer pousada (sem preço nem regra da Rosa): reserva, vaga,
desconto, reclamação, transfer, farmácia, fora do FAQ, confirmar reserva e Pix. Sem --reserva-url, usa o
"reserva_url" do JSON, se houver.

Usa ANTHROPIC_API_KEY e MODELO_BOT do .env do repositório (padrão: bot.assistente.MODELO_PADRAO), reserva_url do
site oficial e SEM buscar_lugares (só o guia do JSON). Não toca em banco nem em WhatsApp. Custo: alguns reais por
rodada (Haiku, prompt em cache). Grava relatorios/avaliacao-exemplo-rosa-AAAA-MM-DD[-rotulo].md com a tabela e as falhas.

Cada pergunta tem critérios objetivos (sem acento e sem diferença de maiúsculas):
  acao      'responder' / 'encaminhar' / None (tanto faz)
  todos     regex que precisam aparecer na resposta (todas)
  algum     regex das quais pelo menos uma precisa aparecer
  nunca     regex que não podem aparecer
Além disso, sempre: idioma certo, sem encaminhamento automático por erro (fallback), a trava do código aceitando,
nenhuma palavra em português em resposta em espanhol/inglês, nome do dono sem rótulo entre parênteses e
"perto/pertinho/a poucos minutos" só junto de uma distância (número com m, km ou min).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from bot.pousada import (DISTANCIA, PERTO, PORTUGUES, PROIBIDO_SO_SITE, PousadaInfo, _sem_acento, atender_hospede, checar_mensagem,  # noqa: E402
                         descontos_do_faq, distancias_do_centro_do_faq, texto_faq)

RESERVA_URL = "https://www.pousadaexemplo.com.br"
SITE = r"pousadaexemplo.com.br"
NAO_CONFIRMA = [r"reserva (esta |ficou |foi |queda |quedo |is )?(confirmad|garantid|feita|hecha)",
                r"(booking|reservation) (is )?(confirmed|guaranteed)", r"\byou'?re (all )?(booked|set)\b"]
# afirmar vaga (o bot não vê a agenda). "No site você vê quais estão disponíveis" é permitido: por isso a
# 1ª rodada (04/10) teve um falso positivo em en-availability e o critério foi refinado para a 2ª.
SEM_VAGA = [r"\b(sim|si|yes)[,!.]? ?(temos|tenemos|hay|we have|tem) (vaga|lugar|disponib|availab|room)",
            r"\b(temos|tenemos|we have|we still have) (vaga|lugar|disponib|availab|room)",
            r"(esta|estao|queda|is|are) (disponivel|disponible|available) (para voce|para vos|para ti|for you)",
            r"\b(it|the date|that date|a data|la fecha) (is|esta) (disponivel|disponible|available)",
            r"\bainda (tem|temos) vaga",
            # fase 3: es-reveillon passou com "tenemos bungalós disponibles" (vaga afirmada)
            r"\b(temos|tenemos|we have)( \w+){0,2} (disponive|disponible|available)"]
# fase 3 (04/10): farmácia com distância "do centrinho" (é da pousada) e "supermercados vendem remédio" (deduzido)
FARMACIA_INVENTADA = [r"\d ?(m|km)\b,? (do|del|de la|from the) centr", r"supermercad[^.]*(medicament|remedio)"]
# fase 2 (04/10): o bot negou um serviço que o FAQ não cita ("a pousada não oferece transfer")
TRANSFER_NEGADO = [r"\b(nao|no) (oferece|ofrece|oferecemos|ofrecemos|tem|tiene|temos|tenemos|faz|hace|disponibiliza)"
                   r" (servico de |el servicio de |o |el )?(transfer|traslado)",
                   r"(doesn'?t|does not|don'?t|do not) (offer|have|provide) (a |any )?(transfer|shuttle)"]
# reserva só pelo site: ninguém "vai confirmar" pelo WhatsApp ("para que confirmen tu reserva")
ALGUEM_CONFIRMA = [PROIBIDO_SO_SITE[0][0], r"para que (te )?confirm"]   # "o site vai confirmar" passa
ROTULO_DONO = r"\((os |as |los |las |the )?(donos?|donas?|duenos?|duenas?|owners?)\b"
RESSALVA = {"pt": r"valores confirmados pela pousada", "es": r"valores confirmados por la posada",
            "en": r"rates are confirmed by the property"}


@dataclass
class Caso:
    id: str
    idioma: str
    pergunta: str
    acao: str | None = None
    todos: list[str] = field(default_factory=list)
    algum: list[str] = field(default_factory=list)
    nunca: list[str] = field(default_factory=list)
    tema: str = ""


CASOS = [
    # --- português (15) ---
    Caso("pt-preco", "pt", "Oi! Quanto custa a diária de vocês em outubro?", "responder",
         todos=[r"351|401|354|357|455", RESSALVA["pt"]], tema="preço"),
    Caso("pt-carnaval", "pt", "Quanto fica o Carnaval de 2027? Tem mínimo de noites?", "responder",
         todos=[r"991", r"\b3 (noites|diarias)", RESSALVA["pt"]], tema="Carnaval"),
    Caso("pt-reveillon", "pt", "Vocês têm vaga pro Réveillon? Somos um casal.", None,
         todos=[SITE], nunca=SEM_VAGA + NAO_CONFIRMA, tema="Réveillon + vaga"),
    Caso("pt-minimo", "pt", "Qual o mínimo de noites num fim de semana normal, fora de feriado?", None,
         algum=[SITE, r"\bsite\b"], nunca=[r"minimo (e |sao )?de \d noites? (no|em) fim de semana"], tema="mínimo de noites"),
    Caso("pt-pet", "pt", "Posso levar meu cachorro? Ele é pequeno e bem comportado.", "responder",
         todos=[r"\bnao\b", r"pet|animal|cachorr|\bcao\b"], nunca=[r"\btaxa\b", r"\bkg\b"], tema="pet (não aceitam)"),
    Caso("pt-cafe", "pt", "O café da manhã está incluso na diária?", "responder",
         todos=[r"opcional|antecedencia|a parte|pedido antes"], tema="café"),
    Caso("pt-reserva", "pt", "Quero reservar de 12 a 15 de novembro pra 2 adultos, como faço?", None,
         todos=[SITE], nunca=SEM_VAGA + NAO_CONFIRMA + [r"(qual|me (diz|passa)) (e )?(o )?seu nome"], tema="reserva"),
    Caso("pt-desconto", "pt", "Se eu ficar 7 noites vocês fazem um desconto?", "encaminhar",
         nunca=[r"\d+ ?% ?(de )?desconto"], tema="desconto"),
    Caso("pt-reclamacao", "pt", "Estou hospedado no bangalô e o chuveiro não esquenta. Já tentei de tudo, que absurdo!",
         "encaminhar", algum=[r"carla|donos|passei|encaminh|avisei"], tema="reclamação"),
    Caso("pt-criancas-piscina", "pt", "Minha filha tem 8 anos, ela pode usar a piscina sozinha?", "responder",
         todos=[r"\b12\b", r"acompanhad|pais|responsave"], tema="crianças na piscina"),
    Caso("pt-som", "pt", "Posso levar minha caixa de som JBL pra curtir na beira da piscina?", "responder",
         todos=[r"\bnao\b", r"som"], nunca=[r"pode sim|pode levar sim|volume baixo"], tema="caixa de som"),
    Caso("pt-aeroporto", "pt", "Como chego do aeroporto de Florianópolis até a pousada?", None,
         todos=[r"florianopolis|\bfln\b", r"\bkm\b|hora|\bh\b|min"], nunca=TRANSFER_NEGADO, tema="como chegar (aeroporto)"),
    Caso("pt-transfer", "pt", "Vocês oferecem transfer do aeroporto de Floripa?", "encaminhar",
         nunca=TRANSFER_NEGADO + [r"\b(sim|temos|oferecemos)\b[^.]{0,20}transfer"], tema="transfer (fora do FAQ)"),
    Caso("pt-farmacia", "pt", "Tem farmácia aqui perto?", "responder",
         todos=[r"navegantes"], nunca=FARMACIA_INVENTADA, tema="farmácia (guia)"),
    Caso("pt-pagamento", "pt", "Quais as formas de pagamento? Dá pra pagar no Pix?", "responder",
         todos=[r"cartao|6x", r"pix"], nunca=[r"chave pix ?:"], tema="pagamento (Pix com 5% no FAQ)"),
    Caso("pt-fora-faq", "pt", "A pousada tem gerador de energia caso falte luz?", "encaminhar",
         nunca=[r"\bsim\b,? (temos|tem) gerador"], tema="fora do FAQ"),
    # --- espanhol (12) ---
    Caso("es-precio", "es", "¡Hola! ¿Cuánto sale la noche en el bungalow Prisma?", "responder",
         todos=[r"455|555", RESSALVA["es"]], tema="preço"),
    Caso("es-carnaval", "es", "¿Cuánto cuesta en Carnaval y cuántas noches mínimo?", "responder",
         todos=[r"991", r"\b3 noches|tres noches"], tema="Carnaval"),
    Caso("es-reveillon", "es", "¿Tienen lugar para Año Nuevo? Somos 4 adultos.", None,
         todos=[SITE], nunca=SEM_VAGA + NAO_CONFIRMA, tema="Réveillon + vaga"),
    Caso("es-mascota", "es", "¿Puedo llevar a mi perro?", "responder",
         todos=[r"\bno\b", r"mascota|perro|animal"], nunca=[r"\btasa\b", r"\bkg\b"], tema="pet (não aceitam)"),
    Caso("es-desayuno", "es", "¿El desayuno está incluido?", "responder",
         todos=[r"opcional|anticipacion|antelacion|aparte"], tema="café"),
    Caso("es-reserva", "es", "Quiero reservar del 3 al 8 de enero para 2 adultos y un niño de 5 años.", None,
         todos=[SITE], nunca=SEM_VAGA + NAO_CONFIRMA + [r"(cual es|decime|dime) tu nombre"], tema="reserva"),
    Caso("es-descuento", "es", "¿Me hacen un descuento si me quedo 10 noches y pago en efectivo?", "encaminhar",
         nunca=[r"\d+ ?% ?(de )?descuento"], tema="desconto"),
    Caso("es-queja", "es", "Estamos en el bungalow y la casa de al lado tiene música altísima a las 2 de la mañana. ¿Pueden hacer algo?",
         "encaminhar", tema="reclamação"),
    Caso("es-aeropuerto", "es", "¿Cómo llego desde el aeropuerto de Florianópolis?", None,
         todos=[r"florianopolis|\bfln\b", r"\bkm\b|hora|\bh\b|min"], nunca=TRANSFER_NEGADO, tema="como chegar (aeroporto)"),
    Caso("es-confirmar", "es", "Ya hice el pago por Pix. ¿Me confirmás la reserva del 10 al 15 de enero, a nombre de Martín Gómez?",
         None, todos=[SITE], nunca=NAO_CONFIRMA + ALGUEM_CONFIRMA + [r"confirmo|confirmada"], tema="tentar confirmar reserva"),
    Caso("es-pix", "es", "Pasame la clave Pix así te pago la seña ahora.", None,
         nunca=[r"clave pix ?(:|es)", r"contato@pousadaexemplo", r"9000"], algum=[SITE, r"carla"], tema="pedido de chave Pix"),
    Caso("es-farmacia", "es", "¿Hay una farmacia cerca de la posada?", "responder",
         todos=[r"navegantes"], nunca=FARMACIA_INVENTADA, tema="farmácia (guia, sem distância)"),
    Caso("es-supermercado", "es", "¿Dónde hay un supermercado para hacer las compras de la semana?", "responder",
         algum=[r"mercado|supermercado"], tema="mercado (guia)"),
    # --- inglês (3) ---
    Caso("en-price", "en", "Hi! How much is a night in early December?", "responder",
         todos=[r"401|451|351", RESSALVA["en"]], tema="preço"),
    Caso("en-availability", "en", "Can you book us 2 nights this weekend? Is there availability?", None,
         todos=[SITE], nunca=SEM_VAGA + NAO_CONFIRMA, tema="reserva + vaga"),
    Caso("en-confirm", "en", "Please confirm my booking for Nov 20-23 under John Smith. I already sent the payment.",
         None, todos=[SITE], nunca=NAO_CONFIRMA + ALGUEM_CONFIRMA + [r"\bi('ve| have)? confirmed\b"],
         tema="tentar confirmar reserva"),
    # fase 2: era /\bconfirmed\b/, que barrava a resposta certa "reservations are confirmed only through our website"
]


def casos_genericos(reserva_url: str, faq: list[dict]) -> list[Caso]:
    """10 perguntas para qualquer pousada (ensaio de implantação). Critérios só do que vale para todas."""
    site = [re.escape(_sem_acento(urlparse(reserva_url).netloc.removeprefix("www.")))] if reserva_url else []
    transfer = [] if "transfer" in _sem_acento(texto_faq(faq)) else TRANSFER_NEGADO  # o FAQ pode dizer que não tem
    return [
        Caso("pt-reserva", "pt", "Quero reservar de 12 a 15 de novembro pra 2 adultos, como faço?", None,
             todos=site, nunca=SEM_VAGA + NAO_CONFIRMA, tema="reserva"),
        Caso("pt-desconto", "pt", "Se eu ficar 7 noites vocês fazem um desconto?", "encaminhar",
             nunca=[r"\d+ ?% ?(de )?desconto"], tema="desconto"),
        Caso("pt-reclamacao", "pt", "Estou hospedado aí e o chuveiro não esquenta. Já tentei de tudo, que absurdo!",
             "encaminhar", tema="reclamação"),
        Caso("pt-transfer", "pt", "Vocês oferecem transfer do aeroporto de Floripa?", None,
             nunca=transfer, tema="transfer"),
        Caso("pt-farmacia", "pt", "Tem farmácia aqui perto?", "responder", nunca=FARMACIA_INVENTADA,
             tema="farmácia (guia)"),
        Caso("pt-fora-faq", "pt", "A pousada tem gerador de energia caso falte luz?", "encaminhar",
             nunca=[r"\bsim\b,? (temos|tem) gerador"], tema="fora do FAQ"),
        Caso("es-reserva", "es", "Quiero reservar del 3 al 8 de enero para 2 adultos y un niño de 5 años.", None,
             todos=site, nunca=SEM_VAGA + NAO_CONFIRMA, tema="reserva"),
        Caso("es-confirmar", "es", "Ya hice el pago por Pix. ¿Me confirmás la reserva del 10 al 15 de enero, a nombre de Martín Gómez?",
             None, todos=site, nunca=NAO_CONFIRMA + (ALGUEM_CONFIRMA if site else []) + [r"confirmo|confirmada"],
             tema="tentar confirmar reserva"),
        Caso("es-pix", "es", "Pasame la clave Pix así te pago la seña ahora.", None,
             nunca=[r"clave pix ?(:|es)"], tema="pedido de chave Pix"),
        Caso("en-availability", "en", "Can you book us 2 nights this weekend? Is there availability?", None,
             todos=site, nunca=SEM_VAGA + NAO_CONFIRMA, tema="reserva + vaga"),
    ]


def avaliar(caso: Caso, acao: str, idioma: str, mensagem: str, fallback: bool,
            descontos: frozenset[str] = frozenset(), so_site: bool = True, hoje: date | None = None,
            do_centro: frozenset[str] = frozenset()) -> list[str]:
    """Lista de critérios que falharam (vazia = passou). descontos: percentuais anunciados no FAQ;
    so_site: pousada que só reserva pelo site (a Exemplo do Rosa); hoje: confere dia da semana junto de data;
    do_centro: distâncias "do centrinho" que o FAQ escreve."""
    t = _sem_acento(mensagem)
    falhas = []
    if fallback:
        falhas.append("encaminhamento automático (erro/sem resposta da IA)")
    if idioma != caso.idioma:
        falhas.append(f"idioma {idioma} (esperado {caso.idioma})")
    if caso.acao and acao != caso.acao:
        falhas.append(f"ação {acao} (esperada {caso.acao})")
    for padrao in caso.todos:
        if not re.search(padrao, t):
            falhas.append(f"faltou /{padrao}/")
    if caso.algum and not any(re.search(p, t) for p in caso.algum):
        falhas.append(f"faltou um de {caso.algum}")
    for padrao in caso.nunca:
        if re.search(padrao, t):
            falhas.append(f"proibido /{padrao}/")
    if caso.idioma in ("es", "en") and (palavras := PORTUGUES.findall(mensagem or "")):
        falhas.append(f"português na resposta em {caso.idioma}: {', '.join(palavras)}")
    if re.search(ROTULO_DONO, t):
        falhas.append("rótulo entre parênteses no nome do dono")
    if re.search(PERTO, t) and not re.search(DISTANCIA, t):
        falhas.append("disse que é perto sem dar a distância")
    if motivo := checar_mensagem(mensagem, descontos, idioma=caso.idioma, so_site=so_site, hoje=hoje,
                                 distancias_do_centro=do_centro):
        falhas.append(f"trava do código: {motivo}")
    return falhas


def _celula(texto: str) -> str:
    return texto.replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def relatorio(linhas: list[dict], modelo: str, quando: datetime, rotulo: str, *, nome: str = "Pousada Exemplo do Rosa",
              faq: str = "pousadas/exemplo-rosa.json", reserva_url: str = RESERVA_URL,
              casos: list[Caso] | None = None) -> str:
    ok = sum(1 for l in linhas if not l["falhas"])
    entrada, saida = sum(l["tokens_entrada"] for l in linhas), sum(l["tokens_saida"] for l in linhas)
    partes = [
        f"# Avaliação do bot · {nome} · {quando:%d/%m/%Y %H:%M}{' · ' + rotulo if rotulo else ''}",
        "",
        f"Gerado por `scripts/avaliar_pousada.py`. Modelo `{modelo}`, FAQ `{faq}`, "
        f"reserva_url `{reserva_url or '(nenhuma: reserva pelo WhatsApp)'}`, sem busca no Google (só o guia). "
        "Cada pergunta é a primeira da conversa. Passos > 1: a IA chamou mais de uma ferramenta ou a trava do código "
        "recusou a primeira resposta e ela reescreveu.",
        "",
        f"**Placar: {ok}/{len(linhas)}** · tokens: {entrada} de entrada (fora o cache) e {saida} de saída.",
        "",
        "| # | id | tema | idioma | ação | passos | ok | pergunta | resposta |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for n, l in enumerate(linhas, 1):
        partes.append(f"| {n} | {l['id']} | {l['tema']} | {l['idioma']} | {l['acao']} | {l.get('passos', '')} | "
                      f"{'✅' if not l['falhas'] else '❌'} | {_celula(l['pergunta'])} | {_celula(l['mensagem'])} |")
    falhas = [l for l in linhas if l["falhas"]]
    partes += ["", "## Falhas", ""]
    if not falhas:
        partes.append("Nenhuma.")
    for l in falhas:
        partes.append(f"- **{l['id']}** ({l['tema']}): " + "; ".join(l["falhas"]))
    partes += ["", "## Critérios", "",
               "Sem acento e sem diferença de maiúsculas. Sempre: idioma certo, sem fallback, trava do código aceita, "
               "sem português em resposta em espanhol/inglês, sem rótulo entre parênteses no nome do dono, "
               "\"perto/pertinho\" só com distância.", ""]
    for c in casos or CASOS:
        regras = [f"ação {c.acao}" if c.acao else "ação livre"]
        regras += [f"contém /{p}/" for p in c.todos]
        if c.algum:
            regras.append("contém um de " + ", ".join(f"/{p}/" for p in c.algum))
        regras += [f"não contém /{p}/" for p in c.nunca]
        partes.append(f"- `{c.id}`: " + "; ".join(regras).replace("|", "\\|"))
    return "\n".join(partes) + "\n"


def reavaliar(arquivo: Path, casos: list[Caso], descontos: frozenset[str], so_site: bool = True,
              do_centro: frozenset[str] = frozenset()) -> list[dict]:
    """Aplica os critérios de hoje às respostas gravadas num relatório antigo (sem chamar a IA)."""
    por_id, linhas = {c.id: c for c in casos}, []
    for linha in arquivo.read_text(encoding="utf-8").splitlines():
        celulas = [c.strip() for c in re.split(r"(?<!\\)\|", linha)[1:-1]]
        if len(celulas) < 8 or celulas[1] not in por_id:
            continue
        c = por_id[celulas[1]]
        idioma, acao, mensagem = celulas[3], celulas[4], celulas[-1].replace("<br>", "\n").replace("\\|", "|")
        linhas.append({"id": c.id, "tema": c.tema, "idioma": idioma, "acao": acao, "pergunta": c.pergunta,
                       "mensagem": mensagem,
                       "falhas": avaliar(c, acao, idioma, mensagem, False, descontos, so_site, do_centro=do_centro),
                       "tokens_entrada": 0, "tokens_saida": 0})
    return linhas


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rotulo", default="", help="sufixo do arquivo (ex.: depois)")
    ap.add_argument("--so", default="", help="ids separados por vírgula (para conferir uma pergunta)")
    ap.add_argument("--faq", default=str(RAIZ / "pousadas" / "exemplo-rosa.json"), help="JSON da pousada")
    ap.add_argument("--reserva-url", default=None, help="padrão: o do JSON (Exemplo do Rosa: o site oficial)")
    ap.add_argument("--generico", action="store_true", help="as 10 perguntas que servem para qualquer pousada")
    ap.add_argument("--reavaliar", help="relatório .md antigo: aplica os critérios atuais às respostas dele, sem IA")
    a = ap.parse_args()

    arquivo_faq = Path(a.faq)
    dados = json.loads(arquivo_faq.read_text(encoding="utf-8"))
    rosa = dados.get("slug") == "exemplo-rosa"
    reserva_url = a.reserva_url if a.reserva_url is not None else (RESERVA_URL if rosa else dados.get("reserva_url", ""))
    todos_casos = casos_genericos(reserva_url, dados["faq"]) if a.generico or not rosa else CASOS
    casos = [c for c in todos_casos if not a.so or c.id in a.so.split(",")]
    descontos = descontos_do_faq(dados["faq"])
    do_centro = distancias_do_centro_do_faq(dados["faq"])
    if a.reavaliar:
        # so_site: antes ficava sempre True e a trava "ninguém confirma" reprovava pousada que reserva pelo WhatsApp
        linhas = reavaliar(Path(a.reavaliar), casos, descontos, bool(reserva_url), do_centro)
        for l in linhas:
            print(f"{'OK ' if not l['falhas'] else 'ERR'} {l['id']:22} {'; '.join(l['falhas'])}")
        print(f"\n{sum(1 for l in linhas if not l['falhas'])}/{len(linhas)} com os critérios de hoje")
        return

    import anthropic
    from dotenv import dotenv_values

    # .env deste checkout ou, num worktree (.claude/worktrees/...), o do repositório principal
    principal = next((p for p in RAIZ.parents if (p / ".git").is_dir()), RAIZ)
    env = {**dotenv_values(principal / ".env"), **dotenv_values(RAIZ / ".env")}
    chave = os.environ.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_API_KEY")
    if not chave:
        sys.exit("ANTHROPIC_API_KEY não encontrada no .env")
    from bot.assistente import MODELO_PADRAO

    modelo = os.environ.get("MODELO_BOT") or env.get("MODELO_BOT") or MODELO_PADRAO
    info = PousadaInfo(dados["nome"], dados["dono"], dados["cidade"], dados["faq"], reserva_url=reserva_url)
    cliente = anthropic.Anthropic(api_key=chave, max_retries=4)
    agora = datetime.now(ZoneInfo("America/Sao_Paulo"))

    linhas = []
    for c in casos:
        r = atender_hospede(c.pergunta, info, cliente=cliente, modelo=modelo, agora=agora)
        falhas = avaliar(c, r.acao, r.idioma, r.mensagem, r.fallback, descontos, so_site=bool(reserva_url),
                         hoje=agora.date(), do_centro=do_centro)
        linhas.append({"id": c.id, "tema": c.tema, "idioma": r.idioma, "acao": r.acao, "pergunta": c.pergunta,
                       "mensagem": r.mensagem, "falhas": falhas, "tokens_entrada": r.tokens_entrada,
                       "tokens_saida": r.tokens_saida, "passos": r.passos})
        print(f"{'OK ' if not falhas else 'ERR'} {c.id:22} {r.acao:10} {r.idioma} p{r.passos}  {'; '.join(falhas)}")
        time.sleep(0.5)

    slug = dados.get("slug") or arquivo_faq.stem
    destino = RAIZ / "relatorios" / f"avaliacao-{slug}-{agora:%Y-%m-%d}{'-' + a.rotulo if a.rotulo else ''}.md"
    destino.parent.mkdir(exist_ok=True)
    try:
        nome_faq = arquivo_faq.resolve().relative_to(RAIZ).as_posix()
    except ValueError:
        nome_faq = arquivo_faq.name
    destino.write_text(relatorio(linhas, modelo, agora, a.rotulo, nome=dados["nome"], faq=nome_faq,
                                 reserva_url=reserva_url, casos=todos_casos), encoding="utf-8")
    print(f"\n{sum(1 for l in linhas if not l['falhas'])}/{len(linhas)} · relatório: {destino}")


if __name__ == "__main__":
    main()
