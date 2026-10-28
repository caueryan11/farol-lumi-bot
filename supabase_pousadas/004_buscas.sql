-- O que os hóspedes procuram na região (relatório mensal do dono). Sem telefone e sem o resultado do Google,
-- que os termos da Places API não deixam guardar.
create table if not exists public.buscas (
  id           uuid primary key default gen_random_uuid(),
  pousada_id   uuid not null references public.pousadas (id) on delete cascade,
  categoria    text not null,
  termo        text,
  idioma       text,
  encontrados  smallint not null default 0,
  criado_em    timestamptz not null default now()
);
create index if not exists buscas_pousada_idx on public.buscas (pousada_id, criado_em desc);
alter table public.buscas enable row level security;
