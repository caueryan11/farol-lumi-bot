-- Proteções do modo pousada (whatsapp/pousada_app.py):
-- o dono respondeu pelo celular: o bot fica quieto nesse chat até pausado_ate;
-- contato salvo na agenda do celular (fornecedor, família) só é atendido se a pousada quiser.
alter table public.hospedes add column if not exists pausado_ate timestamptz;
alter table public.pousadas add column if not exists responder_contatos_salvos boolean not null default false;
