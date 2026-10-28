"""Relatório mensal para o dono da pousada, enviado no WhatsApp dele no dia 1º (whatsapp/pousada_app.py).

Só números agregados (LGPD): nada de nome ou telefone de hóspede. Serve para o dono ver o trabalho do assistente
e decidir coisas (ex.: muita gente pergunta de café da manhã) e para vocês: é o argumento de renovação.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
         "novembro", "dezembro"]
NOMES_IDIOMA = {"pt": "português", "es": "espanhol", "en": "inglês"}


@dataclass
class MetricasMes:
    mensagens: int = 0            # mensagens recebidas de hóspedes
    hospedes: int = 0             # pessoas diferentes que escreveram
    fora_do_horario: int = 0      # recebidas entre 22h e 8h (Brasília)
    respondidas: int = 0          # o assistente resolveu sozinho
    encaminhadas: int = 0         # foram para o dono
    audios: int = 0               # áudios transcritos
    pedidos_reserva: int = 0
    idiomas: dict[str, int] = field(default_factory=dict)       # idioma -> pessoas
    buscas: list[tuple[str, int]] = field(default_factory=list)  # (o que buscaram, vezes), mais buscados primeiro


def mes_anterior(hoje: date) -> tuple[date, date]:
    """(primeiro dia do mês passado, primeiro dia deste mês)."""
    inicio_deste = hoje.replace(day=1)
    inicio = (inicio_deste.replace(year=inicio_deste.year - 1, month=12) if inicio_deste.month == 1
              else inicio_deste.replace(month=inicio_deste.month - 1))
    return inicio, inicio_deste


def _pct(parte: int, total: int) -> str:
    return f"{round(100 * parte / total)}%" if total else "0%"


ROTULOS_BUSCA = {"mercado": "mercado", "padaria": "padaria", "farmacia": "farmácia", "roupas": "roupas",
                 "restaurante": "restaurante", "cafe": "café", "sorveteria": "sorveteria", "bar": "bar",
                 "caixa_eletronico": "caixa eletrônico", "posto_gasolina": "posto de gasolina", "saude": "saúde",
                 "praia_passeio": "praias e passeios"}


def montar(pousada: str, inicio: date, m: MetricasMes) -> str:
    titulo = f"📊 *{pousada} · {MESES[inicio.month - 1]} de {inicio.year}*"
    if not m.mensagens:
        return f"{titulo}\nNenhum hóspede escreveu para o assistente neste mês."
    linhas = [titulo,
              f"💬 {m.mensagens} mensagens de {m.hospedes} pessoa(s)",
              f"🤖 O assistente resolveu {m.respondidas} sozinho; {m.encaminhadas} vieram para você",
              f"🌙 {m.fora_do_horario} chegaram entre 22h e 8h ({_pct(m.fora_do_horario, m.mensagens)})"]
    outros = {k: v for k, v in m.idiomas.items() if k != "pt" and v}
    if outros:
        linhas.append("🌐 " + " · ".join(f"{v} em {NOMES_IDIOMA.get(k, k)}" for k, v in
                                          sorted(outros.items(), key=lambda kv: -kv[1])))
    if m.audios:
        linhas.append(f"🎙️ {m.audios} áudio(s) entendido(s)")
    if m.pedidos_reserva:
        linhas.append(f"🛎️ {m.pedidos_reserva} pedido(s) de reserva passados para você")
    if m.buscas:
        linhas.append("📍 Mais procurados na região: " + ", ".join(
            f"{ROTULOS_BUSCA.get(t, t)} ({n})" for t, n in m.buscas[:5]))
    return "\n".join(linhas)
