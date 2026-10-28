-- Localização da pousada: distâncias do guia local e busca de lugares no Google Maps (bot/lugares.py).
alter table public.pousadas add column if not exists lat double precision;
alter table public.pousadas add column if not exists lon double precision;
