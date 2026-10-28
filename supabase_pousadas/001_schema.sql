-- =====================================================================
-- Recepcionista de WhatsApp para pousadas (NEGOCIO=pousada).
-- Roda num projeto Supabase PRÓPRIO, separado do banco das nutris (dados de pacientes ficam lá, intocados).
-- O backend acessa com a DATABASE_URL (role postgres); RLS ligado sem políticas = nada exposto pela API pública.
-- =====================================================================

create extension if not exists pgcrypto;

create table if not exists public.pousadas (
  id              uuid primary key default gen_random_uuid(),
  slug            text not null unique,              -- ex.: exemplo-rosa (nome do arquivo de configuração)
  nome            text not null,
  cidade          text not null default '',
  dono            text not null,                     -- como o bot chama o responsável (ex.: "Carla")
  telefone_aviso  text,                              -- WhatsApp do dono: recebe pedidos e encaminhamentos
  instancia       text not null unique,              -- nome da instância na uazapi (identifica o número da pousada)
  faq             jsonb not null default '[]'::jsonb,
  ativo           boolean not null default true,
  criado_em       timestamptz not null default now()
);

create table if not exists public.hospedes (
  id          uuid primary key default gen_random_uuid(),
  pousada_id  uuid not null references public.pousadas (id) on delete cascade,
  telefone    text not null,
  nome        text,
  idioma      text check (idioma in ('pt', 'es', 'en')),
  saiu_em     timestamptz,                           -- mandou PARAR
  criado_em   timestamptz not null default now(),
  unique (pousada_id, telefone)
);

create table if not exists public.mensagens (
  id             uuid primary key default gen_random_uuid(),
  pousada_id     uuid not null references public.pousadas (id) on delete cascade,
  hospede_id     uuid not null references public.hospedes (id) on delete cascade,
  direcao        text not null check (direcao in ('entrada', 'saida')),
  wa_message_id  text unique,
  conteudo       text,
  acao           text check (acao in ('responder', 'encaminhar')),
  motivo         text,
  criado_em      timestamptz not null default now()
);
create index if not exists mensagens_hospede_idx on public.mensagens (hospede_id, criado_em desc);

create table if not exists public.pedidos_reserva (
  id               uuid primary key default gen_random_uuid(),
  pousada_id       uuid not null references public.pousadas (id) on delete cascade,
  hospede_id       uuid not null references public.hospedes (id) on delete cascade,
  entrada          date not null,
  saida            date not null check (saida > entrada),
  adultos          smallint not null check (adultos >= 1),
  criancas_idades  smallint[] not null default '{}',
  nome             text not null,
  contato          text,
  acomodacao       text,
  observacoes      text,
  idioma           text,
  status           text not null default 'novo' check (status in ('novo', 'confirmado', 'recusado', 'sem_resposta')),
  criado_em        timestamptz not null default now()
);
create index if not exists pedidos_pousada_idx on public.pedidos_reserva (pousada_id, criado_em desc);

alter table public.pousadas enable row level security;
alter table public.hospedes enable row level security;
alter table public.mensagens enable row level security;
alter table public.pedidos_reserva enable row level security;
