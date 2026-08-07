# POP — Importar novas amostras do GAL no sistema

Procedimento operacional padrão para levar amostras do GAL até o banco de
produção do sistema de reprocesso de dengue (LACEN-RS).

> **Princípio que rege todo o processo:** o sistema conta a história do que a
> equipe **atual** fez e precisa fazer. Amostras cuja PCR já foi concluída em
> ciclo anterior pertencem ao GAL, não ao fluxo de reprocesso — elas não entram
> no sistema, e as que já estiverem no banco são removidas.

---

## Visão geral

```
GAL (CSV)  ──►  merge_data  ──►  2 planilhas  ──►  backup ──► remoção ──► import
                                                        └── aplicar_producao.sh ──┘
```

| Etapa | Comando |
|---|---|
| 1. Exportar do GAL | (manual, no sistema do GAL) |
| 2. Gerar planilhas | `python -m scripts.merge_data` |
| 3. Conferir | `python -m pytest -q` + checklist abaixo |
| 4. Aplicar em produção | `bash scripts/aplicar_producao.sh` |

---

## 1. Exportar do GAL

Exporte os dados como CSV e salve em `data/` como `data1.csv`, `data2.csv`,
`data3.csv`. Para outra quantidade de arquivos, ajuste `ARQUIVOS` em
`scripts/merge_data.py`.

| Item | Valor |
|---|---|
| Separador | `;` (ponto e vírgula) |
| Codificação | ISO-8859-1 (Latin-1) |
| Cabeçalho | 1ª linha, 110 colunas |

Os arquivos **podem se sobrepor** — o script remove duplicatas. Não recorte
períodos manualmente.

## 2. Gerar as planilhas

```bash
python -m scripts.merge_data
```

Saída em `data/`:

| Arquivo | Conteúdo |
|---|---|
| `dengue_consolidado.xlsx` | Acervo total — referência e base da remoção |
| `dengue_consolidado_pendentes.xlsx` | **Vai para o sistema** — sem as que já fizeram PCR |

O script empilha os CSVs → mantém só prefixo `D` → remove linhas idênticas →
colapsa para uma linha por amostra (preferindo a linha de PCR) → ordena pela
regra canônica → recorta as 20 colunas → grava os dois arquivos.

**Guarde as duas.** A planilha total é o insumo da etapa de remoção; sem ela o
`remover_pcr_anterior` não sabe o que excluir.

## 3. Conferir antes de aplicar

```bash
# Suíte completa (requer Postgres; ver README para o container de teste)
docker run -d --name denv-test-pg -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=denv_test -p 55432:5432 postgres:16-alpine
export DATABASE_URL="postgresql://postgres:test@127.0.0.1:55432/denv_test"
python -m pytest -q          # esperado: 258 passed
docker rm -f denv-test-pg
```

Abra a planilha de pendentes e confirme:

- [ ] Aba chamada **`dengue_coleta_dentro_prazo_mun_`**
- [ ] Colunas de data alinhadas à **direita** no Excel (prova que são data, não texto)
- [ ] `Número Interno` sem repetição, ordenado por ano e depois por número
- [ ] `Data do 1º Sintomas` e `Data da Coleta` lado a lado

## 4. Aplicar em produção

```bash
export DATABASE_URL="postgresql://...@ep-xxx.neon.tech/neondb?sslmode=require"

bash scripts/aplicar_producao.sh --simular   # backup + dry-run, não altera nada
bash scripts/aplicar_producao.sh             # aplica de verdade
```

O script executa, nesta ordem:

1. **Backup** — `pg_dump` das tabelas `amostras` e `eventos` em `backups/`.
   Aborta se o dump sair vazio. Não usa cliente local: roda `pg_dump` pela
   imagem `postgres:16-alpine` via Docker.
2. **Dry-run** da remoção — lista o que sairia, sem alterar.
3. **Remoção** das amostras com PCR de ciclo anterior.
4. **Import** da planilha de pendentes.
5. **Conferência** — totais por ano, resíduo de PCR concluída (esperado 0) e
   duplicatas entre anos (esperado 0).

Rode sempre `--simular` primeiro e leia a lista antes de aplicar.

### Rollback

O backup restaura o estado anterior:

```bash
docker run --rm -i --network host postgres:16-alpine \
  psql "$DATABASE_URL" < backups/amostras_AAAAMMDD_HHMMSS.sql
```

Os dumps ficam em `backups/`, fora do git (contêm dados de paciente).

---

## Regras que a planilha precisa respeitar

### Colunas obrigatórias (grafia exata)

O importador localiza as colunas **pelo nome**, não pela posição — ordem e
colunas extras são indiferentes. Estas seis precisam existir com a grafia exata,
incluindo acentos e o ordinal `º`:

```
Requisição
Número Interno
Municipio de Residência      (sem acento em "Municipio" — é assim no GAL)
Data do 1º Sintomas
Data da Coleta
Caso
```

### Datas: sempre datetime, nunca texto

**A regra que mais causa erro silencioso.** O GAL mistura dois formatos na mesma
coluna (`26-04-2026` e `26/04/26`). Com a data em **texto**, `03/04/26` pode ser
lido como **4 de março** em vez de 3 de abril — dia e mês trocados, sem aviso.

Não é cosmético: a `Data da Coleta` define o *ano-de-verdade*, que compõe a
**chave** da amostra. Data errada gera chave errada, e a amostra é duplicada em
vez de atualizada, deixando o progresso preso na linha antiga.

O `merge_data.py` grava as datas como datetime real. Montando planilha à mão,
formate as colunas de data como **Data** no Excel antes de salvar.

### Uma linha por amostra

O GAL traz uma linha por **exame** (NS1, IgM, ZDC), então a mesma amostra
aparece ~3 vezes. O sistema espera uma linha por amostra. O script escolhe a
linha de **PCR (ZDC)** e, no desempate, a de status mais avançado.

### Critério de "já fez PCR"

Uma amostra é excluída do import (e removida do banco) quando tem linha ZDC
**e**:

- `Status Exame` é `Resultado Liberado` ou `Resultado Cadastrado`, **ou**
- `Data do Processamento` preenchida

Os dois sinais concordam integralmente nos dados atuais (601 amostras, zero
divergência).

**O Kit não é prova.** Existem amostras com kit de PCR alocado cujo exame foi
**cancelado** sem data de processamento e sem resultado — o kit foi separado, a
PCR nunca saiu. Essas ainda precisam de PCR e permanecem no fluxo. Foram 5 casos
na carga de agosto/2026 (`D3046/26`, `D3369/26`, `D5341/26`, `D5351/26`,
`D5926/26`). Kits de sorologia (ELISA/NS1) em linhas não-ZDC são irrelevantes.

### O que a reimportação preserva

O UPSERT atualiza só campos descritivos (município, requisição, caso, datas,
flags) e **nunca** toca o trabalho de bancada:

- Etapas: `coletada`, `extraida`, `pcr_feito`, `sequenciado` e suas datas
- Rejeição: `rejeitada`, `motivo_rejeicao`, `data_rejeicao`, `obs_reprocesso`
- Resultados: `den1_ct`..`den4_ct`, `ci_ct`, `data_resultado`

Reimportar a mesma planilha é seguro e idempotente.

---

## Solução de problemas

| Sintoma | Causa provável |
|---|---|
| `Worksheet named '...' not found` | Aba com nome errado — precisa ser `dengue_coleta_dentro_prazo_mun_` |
| `ForeignKeyViolation` ao remover | `eventos.chave` tem FK sem `ON DELETE`; o script já apaga os eventos antes |
| Muitas linhas ignoradas no import | `Número Interno` ausente ou sem `/ano` (ex.: `D3809` sem ano) |
| Amostra duplicada em dois anos | Data da Coleta lida como texto, com dia/mês trocados |
| `AssertionError` nas contagens | Rodou com `verificar_sanidade=True`; os valores são fixos na planilha de 2025 |
| Acentos quebrados (`Requisi��o`) | CSV lido como UTF-8 — o GAL exporta em ISO-8859-1 |
| `ABORTADO: N chaves sem evidência` | A planilha total não corresponde à usada para gerar as pendentes; regere ambas |

---

## Histórico

**Agosto/2026 — primeira carga por este processo.** 3 CSVs do GAL (21.468
linhas) → 5.863 amostras de dengue → 601 já com PCR concluída → **5.262
importadas**. No banco: 161 amostras com PCR anterior removidas, depois 3.787
inseridas + 1.475 atualizadas.
