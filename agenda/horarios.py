"""Horários livres: horário de atendimento do profissional menos o que está ocupado na agenda dele.

Funções puras (sem rede), testadas sem Google. Origem: agenda da nutri (nutri-assistente/agenda/horarios.py),
com duração por serviço e passo próprio (salão: escova de 60 min pode começar a cada 30 min).

atendimento: {"1": ["09:00-12:00", "13:00-19:00"], ..., "6": ["09:00-14:00"]}
Chaves: dia da semana ISO (1 = segunda ... 7 = domingo).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def descrever(dt: datetime) -> str:
    """ex.: 'qua 30/09 às 10:00'"""
    return f"{DIAS[dt.isoweekday() - 1]} {dt:%d/%m} às {dt:%H:%M}"


def _hora(texto: str) -> time:
    h, m = texto.split(":")
    return time(int(h), int(m))


def blocos_do_dia(atendimento: dict, dia: date, fuso: ZoneInfo) -> list[tuple[datetime, datetime]]:
    blocos = []
    for faixa in atendimento.get(str(dia.isoweekday()), []):
        ini, fim = faixa.split("-")
        blocos.append((datetime.combine(dia, _hora(ini), fuso), datetime.combine(dia, _hora(fim), fuso)))
    return blocos


def periodo_ok(dt: datetime, periodo: str) -> bool:
    if periodo == "manha":
        return dt.hour < 12
    if periodo == "tarde":
        return 12 <= dt.hour < 18
    if periodo == "noite":
        return dt.hour >= 18
    return True


def sobrepoe(ini: datetime, fim: datetime, ocupados: list[tuple[datetime, datetime]]) -> bool:
    return any(ini < o_fim and o_ini < fim for o_ini, o_fim in ocupados)


def todos_os_horarios(atendimento: dict, ocupados, agora: datetime, fuso: ZoneInfo, *, duracao_min: int,
                      passo_min: int, antecedencia_horas: float, janela_dias: int,
                      a_partir_de: date | None = None, periodo: str = "qualquer") -> list[datetime]:
    """Inícios possíveis: dentro do atendimento, sem sobrepor nada ocupado, com a antecedência mínima."""
    duracao, passo = timedelta(minutes=duracao_min), timedelta(minutes=passo_min)
    minimo = agora + timedelta(hours=antecedencia_horas)
    hoje = agora.astimezone(fuso).date()
    dia, ultimo_dia = max(a_partir_de or hoje, hoje), hoje + timedelta(days=janela_dias)
    livres = []
    while dia <= ultimo_dia:
        for ini_bloco, fim_bloco in blocos_do_dia(atendimento, dia, fuso):
            t = ini_bloco
            while t + duracao <= fim_bloco:
                if t >= minimo and periodo_ok(t, periodo) and not sobrepoe(t, t + duracao, ocupados):
                    livres.append(t)
                t += passo
        dia += timedelta(days=1)
    return livres


def sugerir(livres: list[datetime], maximo: int = 4, por_dia: int = 2) -> list[datetime]:
    """Espalha as sugestões: no máximo `por_dia` horários por dia, longe um do outro no mesmo dia."""
    escolhidos: list[datetime] = []
    por_data: dict[date, list[datetime]] = {}
    for t in livres:
        do_dia = por_data.setdefault(t.date(), [])
        if len(do_dia) < por_dia and all(abs(t - o) >= timedelta(hours=2) for o in do_dia):
            do_dia.append(t)
            escolhidos.append(t)
        if len(escolhidos) >= maximo:
            break
    return escolhidos
