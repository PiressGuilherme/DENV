# Reprocesso Dengue — LACEN-RS

Tracker para controle do **reprocesso de amostras de dengue**. Acompanha cada
amostra por um fluxo de três etapas — **Coletada → Extraída → PCR feito** — além de um
estado terminal alternativo de **Rejeição** (volume insuficiente / não encontrada).

Stack: **NiceGUI + PostgreSQL (Neon) + pandas**. Acesso via navegador com login por e-mail e senha.

> As regras de negócio e decisões de projeto estão em
> [`ESPECIFICACAO.md`](ESPECIFICACAO.md).

---

## Estrutura

```
DENV/
├── data/                # planilha de origem (read-only)
├── src/
│   ├── auth.py          # login por e-mail e senha (NiceGUI sessions)
│   ├── parsing.py       # parse do Número Interno, ano-de-verdade, flags
│   ├── importer.py      # xlsx -> dedup -> PostgreSQL (idempotente)
│   ├── db.py            # schema, queries, fluxo de fases
│   ├── export.py        # export da visão atual em xlsx/csv
│   └── app.py           # UI NiceGUI (abas por fase, filtros, lote, export)
├── tests/               # pytest (requer DATABASE_URL para testes de banco)
├── ESPECIFICACAO.md     # regras de negócio (referenciadas pelo código)
├── Dockerfile
├── render.yaml          # deploy Render via GitHub
├── entrypoint.sh        # inicia a app; o schema + import rodam no startup
└── requirements.txt
```

---

## Deploy na nuvem — Neon + Render (gratuito, sem cartão)

### 1. Neon — banco PostgreSQL

1. Acesse **[neon.tech](https://neon.tech)** e crie uma conta gratuita.
2. Clique em **"New Project"** → dê um nome (ex: `denv-lacen`) → **Create project**.
3. Na tela do projeto, clique em **"Connect"** e copie a **connection string**:
   ```
   postgresql://user:senha@ep-xxx.sa-east-1.aws.neon.tech/neondb?sslmode=require
   ```
   Guarde essa string — ela será o `DATABASE_URL`.

> **Dica:** Crie uma segunda branch chamada `dev` (menu "Branches → New branch")
> para usar como banco de desenvolvimento local. Cada branch tem sua própria
> connection string independente.

---

### 2. GitHub — subir o repositório

Se ainda não tiver o repo no GitHub:

```bash
git init
git add .
git commit -m "initial commit"
gh repo create denv-lacen-rs --private --push --source=.
```

Ou faça pelo site: **github.com → New repository → push** do código.

---

### 3. Render — serviço web

1. Acesse **[render.com](https://render.com)** e crie uma conta gratuita (pode usar login GitHub).
2. No painel: **New → Blueprint**.
3. Conecte o repositório GitHub que contém o projeto.
4. O Render detecta o `render.yaml` automaticamente. Clique em **"Apply"**.
5. Na lista de serviços, clique no serviço **`denv-lacen-rs`** → **"Environment"**.
6. Adicione as variáveis de ambiente:

   | Variável       | Valor                                    |
   |----------------|------------------------------------------|
   | `DATABASE_URL` | connection string copiada do Neon        |
   | `APP_EMAIL`    | e-mail de acesso (ex: lacen@saude.rs.gov.br) |
   | `APP_PASS`     | senha de acesso (escolha uma forte)      |
   | `APP_SECRET`   | string aleatória longa para sessões      |

   > Para gerar um `APP_SECRET` seguro: `python -c "import secrets; print(secrets.token_hex(32))"`

7. Clique em **"Save Changes"** → **"Manual Deploy → Deploy latest commit"**.

#### O que acontece no primeiro deploy

Ao subir, a app cria o schema e — se o banco Neon estiver vazio — importa as
5.506 amostras do xlsx automaticamente (em lote, ~3 s). Isso roda numa thread de
inicialização, então a porta abre na hora e o import acontece em segundo plano.
Nos deploys seguintes o import é pulado (o banco já tem dados).

Acompanhe em **Render → Logs**:
```
[startup] Schema OK. Amostras no banco: 0
[startup] Banco vazio — importando xlsx...
[startup] Import concluído: 5506 amostras (5506 inseridas).
```

8. Após o deploy, acesse a URL fornecida pelo Render:
   ```
   https://denv-lacen-rs.onrender.com
   ```
   Faça login com o e-mail e senha definidos acima.

---

### 4. Limitações do plano gratuito

| Serviço | Limite | Impacto |
|---------|--------|---------|
| Render free | Dorme após 15 min sem uso | Cold start de ~30 s na primeira requisição do dia |
| Neon free | 512 MB storage, pausa após 5 dias sem acesso | Banco acorda automaticamente na próxima conexão (~2 s) |

Para evitar o sleep do Render, adicione um cron job externo que faça um GET na
URL a cada 10 minutos (ex: [cron-job.org](https://cron-job.org) — gratuito).

---

## Desenvolvimento local

Requer uma `DATABASE_URL` apontando para uma branch de desenvolvimento do Neon
(ou PostgreSQL local).

```bash
# 1. Clonar
git clone https://github.com/seu-usuario/denv-lacen-rs.git
cd denv-lacen-rs

# 2. Ambiente virtual
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Configurar banco (branch dev do Neon ou PostgreSQL local)
export DATABASE_URL="postgresql://..."

# 4. Subir a app (primeiro acesso cria o schema e popula o banco)
python -m src.app
```

Acesse: **http://localhost:8080** (sem senha — auth só ativa com `APP_EMAIL`/`APP_PASS`).

### Atualizar o banco a partir de uma nova planilha

```bash
export DATABASE_URL="postgresql://..."
python -m src.importer data/nova_planilha.xlsx
```

O importador é **idempotente**: reexecutá-lo atualiza os campos descritivos mas
**preserva todo o progresso** de reprocesso/rejeição já registrado.

### Rodar os testes

```bash
export DATABASE_URL="postgresql://..."   # branch dev do Neon
pytest -q
```

Sem `DATABASE_URL`, os 50 testes de parsing rodam normalmente; os 75 testes de
banco são pulados (`skipped`) com a mensagem `DATABASE_URL não configurado`.

---

## Exportar / fazer backup do banco

O banco vive **só no Neon** (na nuvem) — não há mais cópia local. Há três formas de
tirar o estado atual:

1. **Export da visão (pela aplicação).** Em qualquer aba há os botões **Exportar
   xlsx / csv**, que baixam exatamente a visão atual (fase + filtros + ordenação).
   Use a aba **Geral** sem filtros para exportar todas as 5.506 amostras com o
   progresso de cada uma. É a forma do dia a dia para relatório de bancada.

2. **Dump SQL completo (backup integral).** Com a connection string do Neon:
   ```bash
   pg_dump "postgresql://user:senha@ep-xxx.neon.tech/neondb?sslmode=require" \
     > backup_$(date +%Y%m%d).sql
   ```
   Gera um arquivo restaurável com schema + dados de `amostras` e `eventos`.

3. **Snapshot do Neon.** No painel do Neon, **Branches → New branch** cria uma
   cópia instantânea do banco no estado atual (point-in-time), útil como ponto de
   restauração antes de uma mudança grande.

---

## Notas

- **Porta local**: padrão 8080. Para mudar: variável `PORT` ou ajuste em `src/app.py`.
- **Autenticação**: ativada automaticamente quando `APP_EMAIL` e `APP_PASS` estão definidos.
  Localmente (sem essas variáveis), o acesso é direto.
