-- Pousada que só reserva pelo site: o bot manda o link em vez de registrar pedido (bot/pousada.py).
alter table public.pousadas add column if not exists reserva_url text;
