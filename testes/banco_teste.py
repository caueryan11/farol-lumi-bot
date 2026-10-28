"""Apoio aos testes de banco: conexão numa transação que é desfeita no fim (nada fica gravado)."""


class ConexaoTransacao:
    """Mesmo executar() de bot.banco.Conexao, mas sem autocommit (tudo é desfeito no fim)."""

    def __init__(self, conn):
        import psycopg2.extras

        self._conn, self._dict = conn, psycopg2.extras.RealDictCursor

    def executar(self, sql, params=(), *, retorno=None, como_dict=False):
        with self._conn.cursor(cursor_factory=self._dict if como_dict else None) as cur:
            cur.execute(sql, params)
            return cur.fetchone() if retorno == "um" else cur.fetchall() if retorno == "todos" else None
