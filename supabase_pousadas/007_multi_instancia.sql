-- Uma instância da uazapi por pousada (whatsapp/pousada_app.py): cada pousada tem o próprio número e o token dele.
--   * o webhook confere o `token` do corpo contra o token da pousada da instância (`instancia`);
--   * respostas, avisos ao dono e o relatório mensal saem pela instância da própria pousada.
-- Pousada com uazapi_token null usa o UAZAPI_TOKEN do ambiente (modo de uma pousada só, como antes).
-- uazapi_url null usa o UAZAPI_URL do ambiente (só muda se a instância estiver em outro servidor da uazapi).
-- O token é segredo: só o backend lê (RLS ligado sem políticas, 001_schema.sql); o app nunca o registra no log.
-- O código funciona antes desta migração (lê as colunas por to_jsonb). Idempotente.
-- (006 ficou para 006_retencao.sql, do branch feat/pousadas-noite.)
alter table public.pousadas add column if not exists uazapi_token text;
alter table public.pousadas add column if not exists uazapi_url text;
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'pousadas_uazapi_token_check') then
    alter table public.pousadas add constraint pousadas_uazapi_token_check
      check (uazapi_token is null or btrim(uazapi_token) <> '');
  end if;
  if not exists (select 1 from pg_constraint where conname = 'pousadas_uazapi_url_check') then
    alter table public.pousadas add constraint pousadas_uazapi_url_check
      check (uazapi_url is null or uazapi_url ~ '^https://');
  end if;
end;
$$;
-- O mesmo token em duas pousadas mandaria a resposta de uma pelo número da outra.
create unique index if not exists pousadas_uazapi_token_idx on public.pousadas (uazapi_token)
  where uazapi_token is not null;
