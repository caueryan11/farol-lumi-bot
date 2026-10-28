"""Prazo de guarda do modo pousada contra um Postgres de verdade, numa transação desfeita no final.
Use um banco LOCAL e descartável (nunca o de produção): o teste aplica as migrações supabase_pousadas/*.sql.

Rodar:  POUSADAS_TESTE_DSN=postgresql://postgres@localhost:54330/postgres python -m pytest testes/test_pousada_banco.py
"""

import os
import pathlib

import pytest

from testes.banco_teste import ConexaoTransacao

DSN = os.environ.get("POUSADAS_TESTE_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="defina POUSADAS_TESTE_DSN (Postgres local descartável)")
MIGRACOES = pathlib.Path(__file__).resolve().parents[1] / "supabase_pousadas"
PO = "eeeeeeee-0000-0000-0000-000000000001"


def _banco(ate: str):
    import psycopg2

    conn = psycopg2.connect(DSN)
    db = ConexaoTransacao(conn)
    for m in sorted(MIGRACOES.glob("*.sql")):
        if m.name <= ate:
            db.executar(m.read_text(encoding="utf-8"))
    db.executar("insert into public.pousadas (id, slug, nome, dono, instancia) "
                "values (%s, 'teste-guarda', 'P', 'D', 'i-guarda')", (PO,))
    return conn, db


def _repo(db):
    from whatsapp.pousada_repo import RepositorioPousadaPostgres

    r = RepositorioPousadaPostgres.__new__(RepositorioPousadaPostgres)
    r._db = db
    return r


def _hospede(db, tel, dias_criado):
    return db.executar("insert into public.hospedes (pousada_id, telefone, criado_em) "
                       "values (%s, %s, now() - make_interval(days => %s)) returning id",
                       (PO, tel, dias_criado), retorno="um")[0]


def _msg(db, h, dias):
    db.executar("insert into public.mensagens (pousada_id, hospede_id, direcao, conteudo, criado_em) "
                "values (%s, %s, 'entrada', 'oi', now() - make_interval(days => %s))", (PO, h, dias))


def _pedido(db, h, dias):
    db.executar("insert into public.pedidos_reserva (pousada_id, hospede_id, entrada, saida, adultos, nome, criado_em) "
                "values (%s, %s, '2027-01-10', '2027-01-12', 2, 'A', now() - make_interval(days => %s))", (PO, h, dias))


def _cenario(db):
    # prazo padrão: 365 dias (RETENCAO_PADRAO_DIAS)
    antigo = _hospede(db, "5511900000001", 500)       # só mensagens antigas: some com tudo
    _msg(db, antigo, 400)
    ativo = _hospede(db, "5511900000002", 500)        # uma antiga e uma recente: fica, perde só a antiga
    _msg(db, ativo, 400)
    _msg(db, ativo, 5)
    reservou = _hospede(db, "5511900000003", 400)     # pedido de 250 dias: fica (pedido guardado por 1 ano)
    _msg(db, reservou, 370)
    _pedido(db, reservou, 250)
    velho = _hospede(db, "5511900000004", 500)        # pedido de 400 dias: pedido e hóspede somem
    _pedido(db, velho, 400)
    novo = _hospede(db, "5511900000005", 1)           # acabou de chegar, ainda sem mensagem: fica
    return antigo, ativo, reservou, velho, novo


def _conta(db, tabela, h):
    coluna = "id" if tabela == "hospedes" else "hospede_id"
    return db.executar(f"select count(*) from public.{tabela} where {coluna} = %s", (h,), retorno="um")[0]


def test_pausa_do_dono_diz_se_e_nova_e_hashtag_bot_retoma():
    """pausar_hospede: True só na pausa nova (aviso ⏸️ ao dono), False na renovação; com e sem o 9."""
    conn, db = _banco("999")
    try:
        repo = _repo(db)
        assert repo.pausar_hospede(PO, "5548988887777", 3) is True          # cria o hóspede e pausa
        assert repo.hospede(PO, "554888887777").pausado is True             # sem o 9: o mesmo hóspede
        assert repo.pausar_hospede(PO, "554888887777", 3) is False          # renovação: sem novo aviso
        ate = db.executar("select pausado_ate - now() from public.hospedes where pousada_id = %s", (PO,), retorno="um")[0]
        assert 2.9 * 3600 < ate.total_seconds() <= 3 * 3600
        db.executar("update public.hospedes set pausado_ate = now() - interval '1 minute' where pousada_id = %s", (PO,))
        assert repo.hospede(PO, "5548988887777").pausado is False           # a pausa acabou
        assert repo.pausar_hospede(PO, "5548988887777", 3) is True          # nova pausa: novo aviso
        repo.retomar_hospede(PO, "554888887777")                            # "#bot"
        assert repo.hospede(PO, "5548988887777").pausado is False
        assert db.executar("select count(*) from public.hospedes where pousada_id = %s", (PO,), retorno="um")[0] == 1
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize("ate", ["005_protecoes.sql", "006_retencao.sql"])   # antes e depois da migração 006
def test_prazo_de_guarda(ate):
    conn, db = _banco(ate)
    try:
        antigo, ativo, reservou, velho, novo = _cenario(db)
        assert _repo(db).aplicar_prazo_de_guarda() == {"mensagens": 3, "pedidos": 1, "hospedes": 2}
        assert [_conta(db, "hospedes", h) for h in (antigo, ativo, reservou, velho, novo)] == [0, 1, 1, 0, 1]
        assert _conta(db, "mensagens", ativo) == 1 and _conta(db, "pedidos_reserva", reservou) == 1
        if ate >= "006":   # prazo próprio da pousada
            db.executar("update public.pousadas set retencao_dias = 30 where id = %s", (PO,))
            _msg(db, ativo, 60)
            assert _repo(db).aplicar_prazo_de_guarda() == {"mensagens": 1, "pedidos": 0, "hospedes": 0}
    finally:
        conn.rollback()
        conn.close()
