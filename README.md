# Farol · Lumi

A Lumi é a recepcionista de WhatsApp das pousadas: responde hóspedes 24 h em português, espanhol e inglês usando
só o FAQ da pousada (e, se configurado, lugares perto dela no Google Maps), recebe pedidos de reserva completos e
manda tudo pronto para o dono confirmar. Ela não confirma reserva, não cobra e não dá desconto: além do prompt, o
código barra frases desse tipo em PT/ES/EN (`bot/pousada.py`, `checar_mensagem`), inclusive chave Pix, dados
bancários, link de pagamento e e-mail/telefone/link que não estão no FAQ. Toda resposta com valor em dinheiro sai
com a ressalva "Valores confirmados pela pousada na reserva." (no idioma do hóspede), também em código.

Várias pousadas rodam no mesmo servidor, cada uma com a própria instância (número) na uazapi.

> Separado do `nutri-assistente` em 06/10/2026 a partir de `integracao/pousadas@a96e985` (o que estava no
> Railway), com o histórico dos arquivos das pousadas. O modo nutri não existe mais aqui.

## Estrutura

| Caminho | O que é |
|---|---|
| `whatsapp/app.py` | `Config` (variáveis de ambiente) e o ponto de entrada `criar_app_do_ambiente` |
| `whatsapp/pousada_app.py` | o app FastAPI: webhook da uazapi, `/saude`, `/mapa/<slug>`, `/relatorio/...`, rotinas |
| `whatsapp/pousada_repo.py` | acesso ao banco (Supabase das pousadas) |
| `whatsapp/uazapi.py`, `whatsapp/monitor.py` | cliente da uazapi; monitor de conexão e alerta (ntfy/Discord/Slack) |
| `whatsapp/telefone.py`, `whatsapp/consentimento.py` | telefone com/sem o 9; "PARAR" do hóspede |
| `bot/pousada.py` | o laço da IA (Claude) e as travas |
| `bot/lugares.py`, `bot/relatorio_pousada.py`, `bot/avisos.py` | Google Places, relatório mensal, avisos ao dono |
| `supabase_pousadas/` | migrações do banco |
| `pousadas/` | FAQs (JSON) e mapas gerados (`pousadas/mapas/<slug>.html`, servidos em `/mapa/<slug>`) |
| `scripts/` | `nova_pousada.py`, `gerar_mapa.py`, `montar_guia.py`, `avaliar_pousada.py` |
| `relatorios/` | avaliações do bot por pousada |

## Rodar local

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Linux/macOS: .venv/bin/python
cp .env.example .env                                      # preencha (o .env nunca vai para o git)
.venv/Scripts/python -m pytest                            # sem internet: IA, uazapi e banco simulados
.venv/Scripts/python -m uvicorn whatsapp.app:criar_app_do_ambiente --factory --port 8000 --no-access-log
```

Testes de banco (opcionais) rodam num Postgres **local e descartável**, nunca no de produção: as migrações são
aplicadas numa transação desfeita no fim.

```bash
POUSADAS_TESTE_DSN=postgresql://postgres@localhost:54330/postgres .venv/Scripts/python -m pytest
```

## Variáveis de ambiente

| Nome | Obrigatória | Para quê |
|---|---|---|
| `NEGOCIO` | sim (`pousada`) | só existe o modo pousada; outro valor impede o app de subir. Vazio vale `pousada` |
| `ANTHROPIC_API_KEY` | sim | IA (Claude) |
| `DATABASE_URL` | sim | Postgres do projeto Supabase das pousadas |
| `WEBHOOK_SEGREDO` | sim | 16+ caracteres; vai no caminho da URL do webhook |
| `UAZAPI_URL`, `UAZAPI_TOKEN` | sim* | instância do ambiente, usada pelas pousadas sem `uazapi_token` próprio no banco |
| `ROTINAS` | não (padrão `on`) | relatório mensal (dia 1º, 9h), prazo de guarda (3h), monitor do WhatsApp (10 min) |
| `ALERTA_URL` | não | ntfy/Discord/Slack: aviso quando um WhatsApp cai |
| `OPENAI_API_KEY` | não | transcrição de áudio (Whisper pela uazapi); vazio = pede texto |
| `GOOGLE_PLACES_KEY` | não | lugares perto da pousada na hora; vazio = só o guia do FAQ |
| `GOOGLE_MAPS_BROWSER_KEY` | não | mapa do Google em `/mapa/<slug>` (chave de navegador restrita ao domínio) |
| `URL_PUBLICA` | não | domínio público (link do mapa); no Railway vem de `RAILWAY_PUBLIC_DOMAIN` |
| `MODELO_BOT` | não | modelo da IA (padrão `claude-haiku-4-5`) |
| `GOOGLE_SERVICE_ACCOUNT_B64` | não | JSON da chave da conta de serviço do Google, em base64: agenda dos negócios de serviços (cada profissional compartilha a agenda com o `client_email` da chave, permissão "Fazer alterações nos eventos"); vazio = o bot anota o pedido de horário e passa ao dono |
| `PAUSA_DONO_HORAS` | não (padrão `3`) | horas que o bot fica quieto num chat depois que o dono responde pelo celular (1 a 72; valor inválido usa 3 e loga aviso) |

\* `UAZAPI_TOKEN` pode ficar vazio se todas as pousadas tiverem token próprio no banco.

## Negócios de serviços (salão, barbearia, clínica, corretor)

Com `pousadas.tipo = 'servicos'` o webhook usa `bot/negocio.py` em vez do bot da pousada: responde pela base de
conhecimento (`faq`) e, com `agenda_config` preenchido e `GOOGLE_SERVICE_ACCOUNT_B64` no ambiente, vê horários livres
de verdade e marca, remarca e cancela na agenda do Google de cada profissional (`agenda/servicos.py`). Cada mudança
gera um aviso 📅/🔁/❌ para o dono. O formato do `agenda_config` está no topo de `agenda/servicos.py`.

## Banco: migrações

No SQL Editor do projeto Supabase **das pousadas**, na ordem:

1. `001_schema.sql` (tabelas `pousadas`, `hospedes`, `mensagens`, `pedidos_reserva`; RLS sem políticas)
2. `002_localizacao.sql`
3. `003_reserva_url.sql`
4. `004_buscas.sql`
5. `005_protecoes.sql`
6. `006_retencao.sql` (prazo de guarda por pousada; padrão 365 dias)
7. `007_multi_instancia.sql` (token da uazapi por pousada)
8. `008_agenda.sql` (negócios de serviços: `pousadas.tipo`, `pousadas.agenda_config` e a tabela `agendamentos`)

O código lê as colunas novas por `to_jsonb`, então funciona antes da 006 e da 007.

## Cadastrar uma pousada nova

1. Na uazapi, crie uma instância para a pousada (ex.: `pousada-mar`), conecte o WhatsApp pelo QR code, copie o
   token e aponte o webhook para `https://<domínio>/webhook/uazapi/<WEBHOOK_SEGREDO>` (a mesma URL para todas).
2. Escreva o FAQ em `pousadas/<slug>.json` (formato de `pousadas/exemplo-rosa.json`).
3. Gere guia, mapas e SQL:

   ```bash
   python scripts/nova_pousada.py pousadas/<slug>.json --lat -28.12 --lon -48.65 \
       --instancia pousada-mar --telefone-aviso 55489XXXXXXXX \
       [--reserva-url https://site-da-pousada] [--pesquisa pousadas/guia-<slug>-pesquisa.json] \
       --uazapi-token <token da instância>
   ```

4. Faça commit dos mapas `pousadas/mapas/<slug>.html` e `.google.html` (o app os serve do repositório).
5. Cole `pousadas/<slug>.implantacao.sql` (fora do git: tem telefone e token) no SQL Editor do Supabase das
   pousadas. Rodar de novo só atualiza (pelo slug).
6. Mande uma mensagem de teste para o número e confira o log (`pousada: hospede=... acao=...`) e o `/saude`
   (`sem_telefone_aviso` tem de estar vazio: pousada sem WhatsApp do dono fica com o bot calado).
7. No celular da pousada, **desligue a saudação e a mensagem de ausência do WhatsApp Business** (e qualquer outro
   robô no número): elas saem como se o dono tivesse respondido e pausam o bot naquele chat. Combine com o dono:
   quando ele responde um hóspede pelo celular, o bot fica quieto nesse chat por `PAUSA_DONO_HORAS` e ele recebe
   um aviso ⏸️; para o bot voltar antes, ele manda `#bot` na conversa.

Ensaio antes de ligar: `python scripts/avaliar_pousada.py --faq pousadas/<slug>.json --generico` (usa a IA de
verdade; custa alguns reais e grava o resultado em `relatorios/`).

## Deploy no Railway

O `railway.json` já define tudo:

- build: Railpack (Python da `.python-version`, dependências do `requirements.txt`)
- start: `uvicorn whatsapp.app:criar_app_do_ambiente --factory --host 0.0.0.0 --port ${PORT:-8000} --no-access-log`
- healthcheck: `/saude`

Variáveis no serviço: as da tabela acima (`NEGOCIO=pousada`). Depois do deploy, `/saude` mostra a versão (commit),
as rotinas agendadas e o estado do WhatsApp de cada pousada.
