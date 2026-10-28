"""Agenda de um negócio de serviços (salão, barbearia, clínica, corretor) para o bot: ver horários livres, marcar,
remarcar e cancelar, com duração por serviço e uma agenda do Google por profissional.

O Google Calendar de cada profissional é a fonte da verdade do que está ocupado (inclui o que ele marca por fora).
public.agendamentos guarda o que o bot marcou; o índice de exclusão do banco é a última barreira contra horário
duplicado (008_agenda.sql).

agenda_config (jsonb em public.pousadas):
    {"antecedencia_horas": 2, "janela_dias": 14, "passo_min": 30,
     "servicos": [{"id": "corte", "nome": "Corte", "duracao_min": 30}, ...],
     "profissionais": [{"id": "carla", "nome": "Carla", "calendar_id": "...@group.calendar.google.com",
                        "atendimento": {"2": ["09:00-12:00", "13:00-19:00"], ...},
                        "servicos": ["corte", "escova"]}]}   # sem "servicos": faz todos
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from agenda.horarios import descrever, sugerir, todos_os_horarios

FUSO_PADRAO = "America/Sao_Paulo"
JANELA_EVENTOS_MANUAIS_DIAS = 60
_FAIXA = re.compile(r"^\d{2}:\d{2}-\d{2}:\d{2}$")


class CalendarioProtocolo(Protocol):
    def ocupados(self, calendar_id: str, inicio: datetime, fim: datetime) -> list[tuple[datetime, datetime]]: ...
    def criar_evento(self, calendar_id: str, inicio: datetime, fim: datetime, titulo: str, descricao: str = "",
                     fuso: str = FUSO_PADRAO) -> str: ...
    def cancelar_evento(self, calendar_id: str, evento_id: str) -> None: ...
    def listar_eventos(self, calendar_id: str, inicio: datetime, fim: datetime) -> list[dict]: ...


class HorarioOcupado(Exception):
    """O banco recusou o agendamento por sobreposição (alguém marcou o mesmo horário no mesmo instante)."""


# ---------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------
@dataclass
class Servico:
    id: str
    nome: str
    duracao_min: int


@dataclass
class Profissional:
    id: str
    nome: str
    calendar_id: str
    atendimento: dict
    servicos: list[str] = field(default_factory=list)   # vazio = faz todos

    def faz(self, servico: Servico) -> bool:
        return not self.servicos or servico.id in self.servicos


@dataclass
class ConfigAgenda:
    servicos: list[Servico]
    profissionais: list[Profissional]
    antecedencia_horas: float = 2
    janela_dias: int = 14
    passo_min: int = 30


def ler_config(config: dict | None) -> ConfigAgenda:
    """Valida o agenda_config do banco. ValueError diz o que está errado (para quem cadastra o negócio)."""
    if not isinstance(config, dict):
        raise ValueError("agenda_config precisa ser um objeto")
    servicos = []
    for s in config.get("servicos") or []:
        duracao = s.get("duracao_min")
        if not s.get("id") or not s.get("nome") or not isinstance(duracao, int) or not 5 <= duracao <= 600:
            raise ValueError(f"serviço inválido (precisa de id, nome e duracao_min de 5 a 600): {s!r}")
        servicos.append(Servico(str(s["id"]), str(s["nome"]), duracao))
    if not servicos:
        raise ValueError("agenda_config sem serviços")
    ids = {s.id for s in servicos}
    profissionais = []
    for p in config.get("profissionais") or []:
        if not p.get("id") or not p.get("nome") or not p.get("calendar_id"):
            raise ValueError(f"profissional inválido (precisa de id, nome e calendar_id): {p.get('nome')!r}")
        atendimento = p.get("atendimento") or {}
        for dia, faixas in atendimento.items():
            if dia not in {str(d) for d in range(1, 8)} or not all(_FAIXA.match(f) for f in faixas):
                raise ValueError(f"atendimento inválido de {p['nome']}: {dia!r}: {faixas!r} (use \"1\" a \"7\" e \"09:00-12:00\")")
        desconhecidos = set(p.get("servicos") or []) - ids
        if desconhecidos:
            raise ValueError(f"{p['nome']} faz serviço que não está na lista: {sorted(desconhecidos)}")
        profissionais.append(Profissional(str(p["id"]), str(p["nome"]), str(p["calendar_id"]), atendimento,
                                          list(p.get("servicos") or [])))
    if not profissionais:
        raise ValueError("agenda_config sem profissionais")
    return ConfigAgenda(servicos, profissionais, float(config.get("antecedencia_horas", 2)),
                        int(config.get("janela_dias", 14)), int(config.get("passo_min", 30)))


def _chave(texto: str) -> str:
    return unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().lower().strip()


def telefone_no_texto(texto: str, telefone: str) -> bool:
    """O evento é deste cliente se o texto tem o DDD + número dele, em qualquer formato (com ou sem o 9)."""
    digitos = re.sub(r"\D", "", texto or "")
    tel = re.sub(r"\D", "", telefone or "")
    local = tel[2:] if tel.startswith("55") and len(tel) >= 12 else tel
    candidatos = {local}
    if len(local) == 11 and local[2] == "9":
        candidatos.add(local[:2] + local[3:])
    if len(local) == 10:
        candidatos.add(local[:2] + "9" + local[2:])
    return any(len(c) >= 10 and c in digitos for c in candidatos)


# ---------------------------------------------------------------------
# Agenda (uma por conversa: sabe quem é o cliente)
# ---------------------------------------------------------------------
class AgendaNegocio:
    def __init__(self, calendario: CalendarioProtocolo, config: ConfigAgenda, *, cliente_telefone: str,
                 gravar: Callable[[dict], str], listar: Callable[[], list[dict]],
                 cancelar_registro: Callable[[str], None], fuso: str = FUSO_PADRAO,
                 agora: Callable[[], datetime] | None = None):
        """gravar(agendamento) -> id: grava em public.agendamentos; levanta HorarioOcupado se o banco recusar.
        listar(): próximos agendamentos confirmados DESTE cliente
            [{id, servico_id, servico_nome, profissional_id, inicio, fim, google_event_id}].
        cancelar_registro(id): marca o agendamento como cancelado."""
        self._cal = calendario
        self.config = config
        self.fuso = ZoneInfo(fuso)
        self._tel = cliente_telefone
        self._gravar = gravar
        self._listar = listar
        self._cancelar_registro = cancelar_registro
        self._agora = agora or (lambda: datetime.now(self.fuso))
        self.eventos: list[dict] = []   # o que mudou nesta mensagem (o webhook avisa o dono)

    # --- busca de serviço e profissional -----------------------------------
    def _servico(self, valor: str) -> Servico | None:
        v = _chave(valor)
        return next((s for s in self.config.servicos if v in (_chave(s.id), _chave(s.nome))), None)

    def _profissional(self, valor: str) -> Profissional | None:
        v = _chave(valor)
        return next((p for p in self.config.profissionais
                     if v in (_chave(p.id), _chave(p.nome)) or _chave(p.nome).split(" ")[0] == v), None)

    def _nomes_servicos(self) -> str:
        return ", ".join(s.nome for s in self.config.servicos)

    # --- horários livres ---------------------------------------------------
    def _livres(self, pro: Profissional, duracao_min: int, a_partir_de: date | None, periodo: str) -> list[datetime]:
        agora = self._agora()
        fim = agora + timedelta(days=self.config.janela_dias + 1)
        ocupados = self._cal.ocupados(pro.calendar_id, agora, fim)
        return todos_os_horarios(pro.atendimento, ocupados, agora, self.fuso, duracao_min=duracao_min,
                                 passo_min=self.config.passo_min, antecedencia_horas=self.config.antecedencia_horas,
                                 janela_dias=self.config.janela_dias, a_partir_de=a_partir_de, periodo=periodo)

    def ver_horarios_livres(self, servico: str, profissional: str = "", a_partir_de: str = "",
                            periodo: str = "qualquer") -> dict:
        serv = self._servico(servico)
        if serv is None:
            return {"erro": f"serviço não encontrado; os serviços são: {self._nomes_servicos()}"}
        if profissional:
            pro = self._profissional(profissional)
            if pro is None:
                return {"erro": "profissional não encontrado; os profissionais são: "
                                + ", ".join(p.nome for p in self.config.profissionais)}
            if not pro.faz(serv):
                quem = [p.nome for p in self.config.profissionais if p.faz(serv)]
                return {"erro": f"{pro.nome} não faz {serv.nome}; quem faz: {', '.join(quem)}"}
            pros = [pro]
        else:
            pros = [p for p in self.config.profissionais if p.faz(serv)]
        try:
            dia = date.fromisoformat(a_partir_de) if a_partir_de else None
        except ValueError:
            return {"erro": "a_partir_de precisa ser uma data AAAA-MM-DD"}
        quem_em: dict[datetime, Profissional] = {}
        for pro in pros:
            for t in self._livres(pro, serv.duracao_min, dia, periodo or "qualquer"):
                quem_em.setdefault(t, pro)
        escolhidos = sugerir(sorted(quem_em))
        if not escolhidos:
            return {"horarios": [], "aviso": "Nenhum horário livre nesse período; ofereça outro dia ou período."}
        return {"servico": serv.nome, "duracao_min": serv.duracao_min,
                "horarios": [{"inicio": t.strftime("%Y-%m-%dT%H:%M"), "profissional": quem_em[t].id,
                              "profissional_nome": quem_em[t].nome, "descricao": descrever(t)} for t in escolhidos]}

    # --- marcar ------------------------------------------------------------
    def _ler_inicio(self, inicio: str) -> datetime | None:
        try:
            ini = datetime.fromisoformat(str(inicio))
        except (TypeError, ValueError):
            return None
        return ini.replace(tzinfo=self.fuso) if ini.tzinfo is None else ini.astimezone(self.fuso)

    def _criar(self, titulo: str, duracao_min: int, pro: Profissional, ini: datetime, nome: str,
               servico_id: str) -> dict:
        if ini not in self._livres(pro, duracao_min, ini.date(), "qualquer"):   # confere de novo na hora de marcar
            return {"marcado": False, "erro": "esse horário não está mais livre (ou não é de atendimento); ofereça outros"}
        fim = ini + timedelta(minutes=duracao_min)
        evento = self._cal.criar_evento(pro.calendar_id, ini, fim, f"{titulo} – {nome}",
                                        f"Marcado pelo atendimento no WhatsApp.\nCliente: {nome}\nTelefone: {self._tel}",
                                        fuso=str(self.fuso))
        try:
            ag_id = self._gravar({"servico_id": servico_id, "servico_nome": titulo, "profissional_id": pro.id,
                                  "profissional_nome": pro.nome, "calendar_id": pro.calendar_id, "nome": nome,
                                  "telefone": self._tel, "inicio": ini, "fim": fim, "google_event_id": evento})
        except HorarioOcupado:
            self._cal.cancelar_evento(pro.calendar_id, evento)
            return {"marcado": False, "erro": "esse horário acabou de ser ocupado; ofereça outros"}
        return {"marcado": True, "agendamento_id": ag_id, "servico": titulo, "profissional": pro.nome,
                "inicio": ini.strftime("%Y-%m-%dT%H:%M"), "descricao": descrever(ini), "duracao_min": duracao_min}

    def marcar_horario(self, servico: str, profissional: str, inicio: str, nome: str) -> dict:
        serv, pro = self._servico(servico), self._profissional(profissional)
        if serv is None:
            return {"marcado": False, "erro": f"serviço não encontrado; os serviços são: {self._nomes_servicos()}"}
        if pro is None or not pro.faz(serv):
            return {"marcado": False, "erro": "use o profissional que veio junto do horário em ver_horarios_livres"}
        if not str(nome or "").strip():
            return {"marcado": False, "erro": "faltou o nome do cliente; pergunte antes de marcar"}
        ini = self._ler_inicio(inicio)
        if ini is None:
            return {"marcado": False, "erro": "inicio precisa ser AAAA-MM-DDTHH:MM, de um horário da lista"}
        r = self._criar(serv.nome, serv.duracao_min, pro, ini, str(nome).strip()[:80], serv.id)
        if r["marcado"]:
            self.eventos.append({"tipo": "marcado", "nome": str(nome).strip()[:80], **r})
        return r

    # --- horários já marcados ----------------------------------------------
    def _manuais(self, conhecidos: set[str]) -> list[dict]:
        """Eventos que o profissional criou à mão com o telefone do cliente no título ou na descrição."""
        agora = self._agora()
        achados = []
        for pro in self.config.profissionais:
            for e in self._cal.listar_eventos(pro.calendar_id, agora, agora + timedelta(days=JANELA_EVENTOS_MANUAIS_DIAS)):
                if e["id"] not in conhecidos and telefone_no_texto(f"{e['titulo']} {e['descricao']}", self._tel):
                    achados.append({"id": f"g:{pro.id}:{e['id']}", "servico_id": "", "servico_nome": e["titulo"],
                                    "profissional_id": pro.id, "inicio": e["inicio"], "fim": e["fim"],
                                    "google_event_id": e["id"], "manual": True})
        return achados

    def _agendados(self) -> list[dict]:
        do_bot = self._listar()
        conhecidos = {a.get("google_event_id") for a in do_bot if a.get("google_event_id")}
        return sorted(do_bot + self._manuais(conhecidos), key=lambda a: a["inicio"])

    def _pro_de(self, agendamento: dict) -> Profissional | None:
        return next((p for p in self.config.profissionais if p.id == agendamento.get("profissional_id")), None)

    def meus_horarios(self) -> dict:
        agendados = self._agendados()
        if not agendados:
            return {"horarios": [], "aviso": ("Nenhum horário futuro encontrado para este cliente. Se ele diz que tem, "
                                              "foi marcado por fora sem o telefone dele: encaminhe para o dono.")}
        saida = []
        for a in agendados:
            pro = self._pro_de(a)
            ini = a["inicio"].astimezone(self.fuso)
            saida.append({"agendamento_id": str(a["id"]), "servico": a.get("servico_nome") or "",
                          "profissional": pro.nome if pro else "", "inicio": ini.strftime("%Y-%m-%dT%H:%M"),
                          "descricao": descrever(ini)})
        return {"horarios": saida}

    def _achar(self, agendamento_id: str) -> dict | None:
        return next((a for a in self._agendados() if str(a["id"]) == str(agendamento_id)), None)

    def _apagar(self, agendamento: dict) -> None:
        pro = self._pro_de(agendamento)
        if pro and agendamento.get("google_event_id"):
            try:
                self._cal.cancelar_evento(pro.calendar_id, agendamento["google_event_id"])
            except Exception as e:  # já apagado pelo profissional (404/410): segue e cancela no banco
                if getattr(getattr(e, "response", None), "status_code", None) not in (404, 410):
                    raise
        if not agendamento.get("manual"):
            self._cancelar_registro(str(agendamento["id"]))

    def cancelar_horario(self, agendamento_id: str) -> dict:
        a = self._achar(agendamento_id)
        if a is None:
            return {"cancelado": False, "erro": "horário não encontrado entre os deste cliente; chame meus_horarios"}
        self._apagar(a)
        pro = self._pro_de(a)
        r = {"cancelado": True, "servico": a.get("servico_nome") or "", "profissional": pro.nome if pro else "",
             "descricao": descrever(a["inicio"].astimezone(self.fuso))}
        self.eventos.append({"tipo": "cancelado", **r})
        return r

    def remarcar_horario(self, agendamento_id: str, novo_inicio: str, profissional: str = "") -> dict:
        """Marca o novo horário primeiro; só depois cancela o antigo (o cliente nunca fica sem horário)."""
        antigo = self._achar(agendamento_id)
        if antigo is None:
            return {"remarcado": False, "erro": "horário não encontrado entre os deste cliente; chame meus_horarios"}
        serv = self._servico(antigo.get("servico_id") or "")
        titulo = serv.nome if serv else (antigo.get("servico_nome") or "Horário").split(" – ")[0]
        duracao = serv.duracao_min if serv else int((antigo["fim"] - antigo["inicio"]).total_seconds() // 60)
        pro = self._profissional(profissional) if profissional else self._pro_de(antigo)
        if pro is None or (serv and not pro.faz(serv)):
            return {"remarcado": False, "erro": "use o profissional que veio junto do horário em ver_horarios_livres"}
        ini = self._ler_inicio(novo_inicio)
        if ini is None:
            return {"remarcado": False, "erro": "novo_inicio precisa ser AAAA-MM-DDTHH:MM, de um horário da lista"}
        nome = (antigo.get("nome") or (antigo.get("servico_nome") or "").split(" – ")[-1] or "Cliente")[:80]
        novo = self._criar(titulo, duracao, pro, ini, nome, serv.id if serv else "")
        if not novo["marcado"]:
            return {"remarcado": False, "erro": novo["erro"]}
        self._apagar(antigo)
        r = {"remarcado": True, "agendamento_id": novo["agendamento_id"], "servico": titulo, "profissional": pro.nome,
             "de": descrever(antigo["inicio"].astimezone(self.fuso)), "para": novo["descricao"], "inicio": novo["inicio"]}
        self.eventos.append({"tipo": "remarcado", "nome": nome, **r})
        return r
