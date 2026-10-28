-- Lumi para negócios de serviços (salão, barbearia, clínica, corretor) com agenda no Google Calendar.
--   pousadas.tipo          'pousada' (padrão, como sempre) ou 'servicos' (bot/negocio.py)
--   pousadas.agenda_config serviços (com duração) e profissionais (agenda do Google + horário de atendimento);
--                          null = sem agenda: o bot anota o pedido de horário e passa para o dono.
--                          Formato em agenda/servicos.py.
--   agendamentos           o que o bot marcou; o Google Calendar continua sendo a fonte do que está ocupado.
-- A tabela continua se chamando "pousadas" (é a tabela de negócios); "hospedes" são os clientes.
-- O código lê as colunas novas por to_jsonb: funciona antes desta migração (tudo vira 'pousada'). Idempotente.
create extension if not exists btree_gist;

alter table public.pousadas add column if not exists tipo text not null default 'pousada';
alter table public.pousadas add column if not exists agenda_config jsonb;
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'pousadas_tipo_check') then
    alter table public.pousadas add constraint pousadas_tipo_check check (tipo in ('pousada', 'servicos'));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'pousadas_agenda_config_check') then
    alter table public.pousadas add constraint pousadas_agenda_config_check
      check (agenda_config is null or jsonb_typeof(agenda_config) = 'object');
  end if;
end;
$$;

create table if not exists public.agendamentos (
  id                 uuid primary key default gen_random_uuid(),
  pousada_id         uuid not null references public.pousadas (id) on delete cascade,
  -- set null: o prazo de guarda pode apagar o cliente; o horário marcado fica (tem telefone e nome aqui)
  hospede_id         uuid references public.hospedes (id) on delete set null,
  telefone           text not null,
  nome               text not null,
  servico_id         text not null default '',
  servico_nome       text not null,
  profissional_id    text not null,
  profissional_nome  text not null,
  calendar_id        text not null,
  inicio             timestamptz not null,
  fim                timestamptz not null check (fim > inicio),
  google_event_id    text,
  status             text not null default 'confirmado' check (status in ('confirmado', 'cancelado')),
  criado_em          timestamptz not null default now(),
  cancelado_em       timestamptz
);
create index if not exists agendamentos_cliente_idx on public.agendamentos (pousada_id, telefone, inicio);
-- última barreira contra dois clientes no mesmo horário do mesmo profissional (o bot também confere no Google)
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'agendamentos_sem_sobreposicao') then
    alter table public.agendamentos add constraint agendamentos_sem_sobreposicao
      exclude using gist (calendar_id with =, tstzrange(inicio, fim) with &&) where (status = 'confirmado');
  end if;
end;
$$;

alter table public.agendamentos enable row level security;
