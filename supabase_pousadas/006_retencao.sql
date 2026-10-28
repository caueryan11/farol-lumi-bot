-- Prazo de guarda (LGPD) das conversas dos hóspedes: todo dia às 3h (Brasília) o app apaga
-- (whatsapp/pousada_repo.py: aplicar_prazo_de_guarda)
--   * mensagens com mais de `retencao_dias`;
--   * pedidos de reserva com mais de 365 dias (ou do prazo, se for maior);
--   * hóspedes (telefone, nome, idioma) sem mensagem nem pedido guardado e criados há mais que o prazo.
-- O código funciona antes desta migração (usa 365 dias quando a coluna não existe). Idempotente.
alter table public.pousadas add column if not exists retencao_dias integer not null default 365;
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'pousadas_retencao_dias_check') then
    alter table public.pousadas add constraint pousadas_retencao_dias_check check (retencao_dias between 30 and 3650);
  end if;
end;
$$;
-- A limpeza apaga por data: índices para o delete não varrer as tabelas inteiras.
create index if not exists mensagens_criado_idx on public.mensagens (criado_em);
create index if not exists pedidos_hospede_idx on public.pedidos_reserva (hospede_id);
