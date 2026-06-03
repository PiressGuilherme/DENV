# Reprocesso Dengue — LACEN-RS

Tracker para controle do **reprocesso de amostras de dengue**. Acompanha cada
amostra por um fluxo de três etapas — **Coletada → Extraída → PCR feito** — além de um
estado terminal alternativo de **Rejeição** (volume insuficiente / não encontrada).

Stack: **NiceGUI + PostgreSQL (Neon) + pandas**. Acesso via navegador com login por e-mail e senha.

> As regras de negócio e decisões de projeto estão em
> [`CLAUDE_REPROCESSO_DENGUE.md`](CLAUDE_REPROCESSO_DENGUE.md).

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
├── Dockerfile
├── render.yaml          # deploy Render via GitHub
├── entrypoint.sh        # first-boot: popula o Neon a partir do xlsx
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

O `entrypoint.sh` detecta que o banco Neon está vazio e executa o importer
automaticamente (≈ 30 s para importar as 5.506 amostras do xlsx). Nos deploys
seguintes esse passo é pulado — o banco já tem dados.

Acompanhe em **Render → Logs**:
```
[entrypoint] Banco vazio — criando schema e importando xlsx...
[entrypoint] Import concluído.
[entrypoint] Iniciando app em 0.0.0.0:10000...
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

## Notas

- **Porta local**: padrão 8080. Para mudar: variável `PORT` ou ajuste em `src/app.py`.
- **Autenticação**: ativada automaticamente quando `APP_EMAIL` e `APP_PASS` estão definidos.
  Localmente (sem essas variáveis), o acesso é direto.
- **Backup do banco**: via painel do Neon → **Backups** (automático no plano free) ou
  exportando a visão atual em CSV/xlsx pela própria interface da aplicação.
