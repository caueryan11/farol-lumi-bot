"""Banco da Lumi (NEGOCIO=pousada). Projeto Supabase PRÓPRIO das pousadas:
esquema em supabase_pousadas/*.sql (aplicar na ordem 001 -> 007)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from bot.relatorio_pousada import MetricasMes
from whatsapp.telefone import variantes_telefone

RETENCAO_PADRAO_DIAS = 365   # prazo de guarda das conversas quando a pousada não define outro (006_retencao.sql)
RETENCAO_MINIMA_DIAS = 30    # trava contra apagar tudo por engano (ex.: retencao_dias = 0)
GUARDA_PEDIDOS_DIAS = 365    # pedidos de reserva ficam pelo menos 1 ano (o dono consulta, relatório do ano)


@dataclass
class Pousada:
    id: str
    nome: str
    dono: str
    telefone_aviso: str | None
    cidade: str = ""
    faq: list[dict] = field(default_factory=list)
    slug: str = ""
    lat: float | None = None   # localização da pousada: distâncias do guia e busca de lugares
    lon: float | None = None
    reserva_url: str = ""      # só reserva pelo site: o bot manda o link em vez de registrar pedido
    responder_contatos_salvos: bool = False  # False: contato salvo na agenda (fornecedor, família) não é atendido
    instancia: str = ""        # nome da instância na uazapi (o número da pousada)
    # token e URL da instância (007_multi_instancia.sql); vazio = UAZAPI_TOKEN/UAZAPI_URL do ambiente.
    # repr=False: o token não aparece em log nem em mensagem de erro
    uazapi_token: str = field(default="", repr=False)
    uazapi_url: str = ""
    tipo: str = "pousada"      # 'servicos': salão, barbearia, clínica... (bot/negocio.py, 008_agenda.sql)
    agenda_config: dict | None = None   # serviços e profissionais com agenda do Google (agenda/servicos.py)


@dataclass
class Hospede:
    id: str
    telefone: str
    nome: str | None = None
    idioma: str | None = None
    saiu: bool = False   # mandou PARAR
    pausado: bool = False  # o dono respondeu pelo celular há pouco: o bot fica quieto nesse chat


class RepositorioPousada(Protocol):
    def pousada_por_instancia(self, instancia: str) -> Pousada | None: ...
    def pousadas_ativas(self) -> list[Pousada]: ...
    def hospede(self, pousada_id: str, telefone: str) -> Hospede: ...
    def pausar_hospede(self, pousada_id: str, telefone: str, horas: float) -> bool: ...
    def retomar_hospede(self, pousada_id: str, telefone: str) -> None: ...
    def atualizar_hospede(self, hospede_id: str, campos: dict) -> None: ...
    def registrar_mensagem(self, pousada_id: str, hospede_id: str, direcao: str, wa_id: str | None,
                           conteudo: str | None, acao: str | None = None, motivo: str | None = None) -> bool: ...
    def historico(self, hospede_id: str, antes_de_wa_id: str, limite: int = 10) -> list[tuple[str, str]]: ...
    def registrar_pedido(self, pousada_id: str, hospede_id: str, pedido: dict, idioma: str) -> str: ...
    def aplicar_prazo_de_guarda(self, padrao_dias: int = RETENCAO_PADRAO_DIAS) -> dict: ...
    def gravar_agendamento(self, pousada_id: str, hospede_id: str, ag: dict) -> str: ...
    def agendamentos_do_cliente(self, pousada_id: str, telefone: str) -> list[dict]: ...
    def cancelar_agendamento(self, pousada_id: str, agendamento_id: str) -> None: ...


class RepositorioPousadaPostgres:
    def __init__(self, dsn: str):
        from bot.banco import Conexao

        self._db = Conexao(dsn)

    def _um(self, sql: str, params: tuple) -> dict | None:
        return self._db.executar(sql, params, retorno="um", como_dict=True)

    # token/URL da instância lidos por to_jsonb: o código funciona antes da 007_multi_instancia.sql (sem as colunas,
    # vale o UAZAPI_TOKEN do ambiente, como no modo de uma pousada só)
    _CAMPOS = ("id::text, slug, nome, dono, telefone_aviso, cidade, faq, lat, lon, coalesce(reserva_url, '') as reserva_url, "
               "coalesce(responder_contatos_salvos, false) as responder_contatos_salvos, instancia, "
               "coalesce(to_jsonb(p) ->> 'uazapi_token', '') as uazapi_token, "
               "coalesce(to_jsonb(p) ->> 'uazapi_url', '') as uazapi_url, "
               "coalesce(to_jsonb(p) ->> 'tipo', 'pousada') as tipo, to_jsonb(p) -> 'agenda_config' as agenda_config")

    def pousada_por_instancia(self, instancia: str) -> Pousada | None:
        r = self._um(f"select {self._CAMPOS} from public.pousadas p where instancia = %s and ativo", (instancia,))
        return Pousada(**r) if r else None

    def pousadas_ativas(self) -> list[Pousada]:
        """Para o monitor de conexão: uma linha por pousada ativa, com a instância dela."""
        linhas = self._db.executar(f"select {self._CAMPOS} from public.pousadas p where ativo order by slug", (),
                                   retorno="todos", como_dict=True)
        return [Pousada(**r) for r in linhas]

    def hospede(self, pousada_id: str, telefone: str) -> Hospede:
        """Hóspede deste telefone (cria no primeiro contato; aceita celular com ou sem o 9)."""
        campos = ("id::text, telefone, nome, idioma, saiu_em is not null as saiu, "
                  "coalesce(pausado_ate > now(), false) as pausado")
        r = self._um(f"select {campos} from public.hospedes "
                     "where pousada_id = %s and telefone = any(%s)", (pousada_id, variantes_telefone(telefone)))
        if r is None:
            r = self._um("insert into public.hospedes (pousada_id, telefone) values (%s, %s) "
                         "on conflict (pousada_id, telefone) do update set telefone = excluded.telefone "
                         f"returning {campos}", (pousada_id, telefone))
        return Hospede(**r)

    def pausar_hospede(self, pousada_id: str, telefone: str, horas: float) -> bool:
        """O dono assumiu a conversa pelo celular: o bot não responde este hóspede por `horas`.
        True se é uma pausa nova (o chat não estava pausado): o dono recebe o aviso ⏸️ só nesse caso."""
        self.hospede(pousada_id, telefone)  # cria se for o primeiro contato (o dono escreveu antes do hóspede)
        # o subselect lê o pausado_ate de ANTES do update (mesmo snapshot)
        r = self._um("with mudados as (update public.hospedes h set pausado_ate = now() + make_interval(secs => %s) "
                     "from (select id, coalesce(pausado_ate > now(), false) as ja_pausado from public.hospedes "
                     "where pousada_id = %s and telefone = any(%s)) antes where h.id = antes.id returning antes.ja_pausado) "
                     "select coalesce(bool_or(ja_pausado), false) as ja_pausado from mudados",
                     (horas * 3600, pousada_id, variantes_telefone(telefone)))
        return not (r or {}).get("ja_pausado")

    def retomar_hospede(self, pousada_id: str, telefone: str) -> None:
        """O dono mandou "#lumi" na conversa: o bot volta a responder este hóspede na hora."""
        self._db.executar("update public.hospedes set pausado_ate = null where pousada_id = %s and telefone = any(%s)",
                          (pousada_id, variantes_telefone(telefone)))

    def atualizar_hospede(self, hospede_id: str, campos: dict) -> None:
        permitidos = {"nome", "idioma"}
        sets = [f"{k} = %s" for k in campos if k in permitidos]
        valores = [v for k, v in campos.items() if k in permitidos]
        if "saiu" in campos:
            sets.append("saiu_em = " + ("now()" if campos["saiu"] else "null"))
        if sets:
            self._db.executar(f"update public.hospedes set {', '.join(sets)} where id = %s", (*valores, hospede_id))

    def registrar_mensagem(self, pousada_id: str, hospede_id: str, direcao: str, wa_id: str | None,
                           conteudo: str | None, acao: str | None = None, motivo: str | None = None) -> bool:
        """False se a mensagem já foi registrada (evento repetido)."""
        r = self._um("insert into public.mensagens (pousada_id, hospede_id, direcao, wa_message_id, conteudo, acao, motivo) "
                     "values (%s, %s, %s, %s, %s, %s, %s) on conflict (wa_message_id) do nothing returning id",
                     (pousada_id, hospede_id, direcao, wa_id, conteudo, acao, motivo))
        return r is not None

    def historico(self, hospede_id: str, antes_de_wa_id: str, limite: int = 10) -> list[tuple[str, str]]:
        linhas = self._db.executar(
            "select direcao, conteudo from public.mensagens where hospede_id = %s and conteudo is not null "
            "and wa_message_id is distinct from %s and criado_em > now() - interval '7 days' "
            "order by criado_em desc limit %s", (hospede_id, antes_de_wa_id, limite), retorno="todos")
        return list(reversed(linhas))

    def registrar_pedido(self, pousada_id: str, hospede_id: str, pedido: dict, idioma: str) -> str:
        r = self._um(
            "insert into public.pedidos_reserva (pousada_id, hospede_id, entrada, saida, adultos, criancas_idades, "
            "nome, contato, acomodacao, observacoes, idioma) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "returning id::text",
            (pousada_id, hospede_id, pedido["entrada"], pedido["saida"], pedido["adultos"], pedido["criancas_idades"],
             pedido["nome"], pedido.get("contato") or None, pedido.get("acomodacao") or None,
             pedido.get("observacoes") or None, idioma))
        return r["id"]

    # --- agenda (negócios de serviços, 008_agenda.sql) -------------------------------------------------
    def gravar_agendamento(self, pousada_id: str, hospede_id: str, ag: dict) -> str:
        """HorarioOcupado se o banco recusar por sobreposição (mesmo profissional, mesmo horário)."""
        import psycopg2

        from agenda.servicos import HorarioOcupado

        try:
            r = self._um(
                "insert into public.agendamentos (pousada_id, hospede_id, telefone, nome, servico_id, servico_nome, "
                "profissional_id, profissional_nome, calendar_id, inicio, fim, google_event_id) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id::text",
                (pousada_id, hospede_id, ag["telefone"], ag["nome"], ag["servico_id"], ag["servico_nome"],
                 ag["profissional_id"], ag["profissional_nome"], ag["calendar_id"], ag["inicio"], ag["fim"],
                 ag["google_event_id"]))
        except psycopg2.errors.ExclusionViolation as e:
            raise HorarioOcupado(str(e)) from e
        return r["id"]

    def agendamentos_do_cliente(self, pousada_id: str, telefone: str) -> list[dict]:
        """Próximos horários confirmados deste cliente (telefone com ou sem o 9)."""
        return self._db.executar(
            "select id::text, nome, servico_id, servico_nome, profissional_id, inicio, fim, google_event_id "
            "from public.agendamentos where pousada_id = %s and telefone = any(%s) and status = 'confirmado' "
            "and fim > now() order by inicio", (pousada_id, variantes_telefone(telefone)),
            retorno="todos", como_dict=True)

    def cancelar_agendamento(self, pousada_id: str, agendamento_id: str) -> None:
        self._db.executar("update public.agendamentos set status = 'cancelado', cancelado_em = now() "
                          "where pousada_id = %s and id::text = %s", (pousada_id, agendamento_id))

    # --- prazo de guarda (LGPD) ---------------------------------------------------------------------
    def aplicar_prazo_de_guarda(self, padrao_dias: int = RETENCAO_PADRAO_DIAS) -> dict:
        """Apaga o que passou do prazo de cada pousada (retencao_dias; 006_retencao.sql): mensagens antigas,
        pedidos de reserva com mais de 1 ano (ou do prazo, se maior) e hóspedes sem mais nada guardado.
        Hóspede com pedido guardado fica (o pedido aponta para o telefone dele). Lê a coluna por to_jsonb para
        funcionar também antes da migração (sem a coluna: `padrao_dias`)."""
        prazo = ("with prazo as (select id, greatest(coalesce((to_jsonb(p) ->> 'retencao_dias')::int, %s), %s) as dias "
                 "from public.pousadas p), apagados as ({}) select count(*) as n from apagados")
        params = (int(padrao_dias), RETENCAO_MINIMA_DIAS)
        mensagens = ("delete from public.mensagens m using prazo where m.pousada_id = prazo.id "
                     "and m.criado_em < now() - make_interval(days => prazo.dias) returning 1")
        pedidos = ("delete from public.pedidos_reserva r using prazo where r.pousada_id = prazo.id "
                   f"and r.criado_em < now() - make_interval(days => greatest(prazo.dias, {GUARDA_PEDIDOS_DIAS})) returning 1")
        hospedes = ("delete from public.hospedes h using prazo where h.pousada_id = prazo.id "
                    "and h.criado_em < now() - make_interval(days => prazo.dias) "
                    "and coalesce((to_jsonb(h) ->> 'pausado_ate')::timestamptz, '-infinity') < now() "
                    "and not exists (select 1 from public.mensagens m where m.hospede_id = h.id) "
                    "and not exists (select 1 from public.pedidos_reserva r where r.hospede_id = h.id) returning 1")
        apagados = {}
        for nome, sql in (("mensagens", mensagens), ("pedidos", pedidos), ("hospedes", hospedes)):  # nesta ordem
            apagados[nome] = int(self._um(prazo.format(sql), params)["n"])
        return apagados


    # --- buscas e relatório mensal ------------------------------------------------------------------
    def registrar_busca(self, pousada_id: str, categoria: str, termo: str, idioma: str, encontrados: int) -> None:
        """O que o hóspede procurou na região (sem telefone e sem o resultado do Google, que não pode ser guardado)."""
        self._db.executar("insert into public.buscas (pousada_id, categoria, termo, idioma, encontrados) "
                          "values (%s, %s, %s, %s, %s)", (pousada_id, categoria, termo or None, idioma, encontrados))

    def pousadas_para_relatorio(self) -> list[dict]:
        """Inclui a instância e o token dela: o relatório sai pelo número da própria pousada."""
        return self._db.executar("select id::text, slug, nome, telefone_aviso, instancia, "
                                 "coalesce(to_jsonb(p) ->> 'uazapi_token', '') as uazapi_token, "
                                 "coalesce(to_jsonb(p) ->> 'uazapi_url', '') as uazapi_url from public.pousadas p "
                                 "where ativo and telefone_aviso is not null", (), retorno="todos", como_dict=True)

    def pousada_por_slug(self, slug: str) -> dict | None:
        return self._um("select id::text, slug, nome, telefone_aviso from public.pousadas where slug = %s", (slug,))

    def metricas_mes(self, pousada_id: str, inicio, fim) -> MetricasMes:
        """Números agregados de [inicio, fim) no horário de Brasília (bot/relatorio_pousada.py)."""
        periodo = ("criado_em >= (%s::date at time zone 'America/Sao_Paulo') "
                   "and criado_em < (%s::date at time zone 'America/Sao_Paulo')")
        r = self._um(
            f"""select count(*) filter (where direcao = 'entrada') as mensagens,
                       count(distinct hospede_id) filter (where direcao = 'entrada') as hospedes,
                       count(*) filter (where direcao = 'entrada' and extract(hour from criado_em at time zone
                                        'America/Sao_Paulo') not between 8 and 21) as fora_do_horario,
                       count(*) filter (where direcao = 'saida' and acao = 'responder') as respondidas,
                       count(*) filter (where direcao = 'saida' and acao = 'encaminhar') as encaminhadas,
                       count(*) filter (where direcao = 'entrada' and conteudo like '[áudio]%%') as audios
                from public.mensagens where pousada_id = %s and {periodo}""", (pousada_id, inicio, fim))
        idiomas = self._db.executar(
            f"""select coalesce(h.idioma, 'pt') as idioma, count(distinct h.id) as n from public.hospedes h
                join public.mensagens m on m.hospede_id = h.id and m.direcao = 'entrada'
                where m.pousada_id = %s and {periodo.replace('criado_em', 'm.criado_em')} group by 1""",
            (pousada_id, inicio, fim), retorno="todos", como_dict=True)
        pedidos = self._um(f"select count(*) as n from public.pedidos_reserva where pousada_id = %s and {periodo}",
                           (pousada_id, inicio, fim))
        buscas = self._db.executar(
            f"""select lower(coalesce(termo, categoria)) as o_que, count(*) as n from public.buscas
                where pousada_id = %s and {periodo} group by 1 order by 2 desc, 1 limit 5""",
            (pousada_id, inicio, fim), retorno="todos", como_dict=True)
        return MetricasMes(**{k: int(v or 0) for k, v in (r or {}).items()},
                           pedidos_reserva=int((pedidos or {}).get("n") or 0),
                           idiomas={i["idioma"]: int(i["n"]) for i in idiomas},
                           buscas=[(b["o_que"], int(b["n"])) for b in buscas])


def carregar_faq(caminho: str) -> dict:
    with open(caminho, encoding="utf-8") as f:
        return json.load(f)
