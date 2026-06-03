# Especificação — Controle de Reprocesso de Amostras de Dengue (LACEN-RS)

> Regras de negócio e decisões de projeto. O código referencia as seções deste
> documento (ex.: "Seção 3.3") nos comentários. Stack: **NiceGUI + PostgreSQL
> (Neon) + pandas**, single-user, controle interno.

---

## 1. Objetivo do sistema

Controlar, para cada amostra de dengue, o avanço de um **novo fluxo de reprocesso** com três etapas sequenciais:

1. **Coletada** (retirada do freezer / disponível)
2. **Extraída** (RNA extraído)
3. **PCR feito** (RT-PCR realizado no reprocesso)

Este fluxo é **independente** dos resultados antigos da planilha. As colunas originais
`Metodologia`, `Status Exame` e `1º–6º Campo Resultado` descrevem o processamento ANTIGO
(NS1/PCR de rotina) e **não devem dirigir nem aparecer como status** do reprocesso. Servem
apenas como contexto histórico de leitura.

O sistema é uma ferramenta de tracking enxuta — não é o HELIX e não tenta ser.

---

## 2. A planilha de origem — fatos confirmados

Arquivo: `dengue_coleta_dentro_prazo_mun_ordenado.xlsx`, aba única `dengue_coleta_dentro_prazo_mun_`.

- **17.624 linhas brutas**, 20 colunas.
- Colunas: `Requisição`, `Número Interno`, `Número Interno (limpo)`, `Retirada`, `PCR Feito`,
  `Observação` (100% vazia), `Ano`, `Data do 1º Sintomas`, `Data da Coleta`, `Dif Dias`,
  `Municipio de Residência`, `Metodologia`, `Status Exame`, `1º–6º Campo Resultado`, `Caso`.
- As datas (`Data da Coleta`, `Data do 1º Sintomas`) já vêm como **datetime real** (não serial Excel).
- `Retirada` e `PCR Feito` existem mas estão **100% FALSE** — campos do fluxo antigo, ignorados;
  o reprocesso usa campos próprios.

### Deduplicação (números reais já validados)

- **1.276 linhas ignoradas**: sem Número Interno (1.271) ou NI não-parseável (5).
- **16.348 linhas com NI válido**, que correspondem a **5.506 amostras únicas**.
- ~10.842 linhas são duplicatas: a mesma amostra repetida porque passou por NS1
  (Enzimaimunoensaio) e/ou RT-PCR, às vezes com a metodologia cancelada e relançada.
  A maioria das amostras (4.803) aparece em exatamente 3 linhas; algumas em até 7.

### Distribuição das amostras únicas por ano (ano-de-verdade = ano da coleta)

- 2025: 3.488 amostras
- 2026: 2.018 amostras

(Após a reclassificação 2026 da Seção 3, essas contagens passam a **3.415 / 2.091** — ver adiante.)

### Prefixos de Número Interno encontrados

`D` (esmagadora maioria, 16.305 linhas), `SR` (38), `FA` (3), `H` (2). O parser aceita
qualquer prefixo alfabético, não assume só `D`.

---

## 3. Regras de negócio (decisões do usuário — NÃO reabrir)

### 3.1 Ano-de-verdade = Data da Coleta

Quando o ano embutido no Número Interno diverge do ano da Data da Coleta, **a Data da Coleta
vence**. Isso neutraliza os erros de digitação do NI automaticamente.

`ano_verdade = ano(Data da Coleta)`; se a Data da Coleta estiver ausente, cai para o ano do NI.

### 3.2 Chave de amostra única

```
chave = f"{prefixo}{numero_sequencial}/{ano_verdade}"
```

O `numero_sequencial` (inteiro) vem do NI; o ano vem da regra 3.1. Uma amostra = uma linha de
controle. Metodologia antiga é descartada (decisão do usuário), mas a contagem de linhas brutas
que originaram a amostra é guardada (campo `n_origem`) como proxy de "já foi mexida antes".

### 3.3 Ordenação cronológica padrão (a dor central)

A visualização padrão e sempre disponível ordena por:

```sql
ORDER BY ano_verdade ASC, prefixo ASC, numero_sequencial ASC
```

`numero_sequencial` é **INTEGER**, nunca texto — é isso que conserta o "A-Z" do Excel que
coloca D1264 depois de D11633. No AG-Grid, a coluna do número tem tipo numérico para a
ordenação nativa funcionar; o NI formatado (`D1264/25`) fica numa coluna separada de display.

### 3.4 Inconsistências — sinalizar, nunca bloquear

Marcar cada amostra com flags (campo texto, separado por `;`). Contagens reais:

- `ANO_NI_DIVERGE` — ano do NI ≠ ano da coleta. **78 amostras.** Inclui viradas de ano legítimas
  (ex.: `D001/26` coletada em 30/12/2025). Sinalizar, não corrigir o NI.
- `ANO_NI_IMPOSSIVEL` — ano do NI ≥ 2027 (erros tipo `D26585/28`, `D828/29`). **2 amostras.**
  O ano-de-verdade já as reposiciona corretamente via Data da Coleta.
- `COLETA_ANTES_SINTOMA` — Data da Coleta anterior ao 1º Sintoma. **25 amostras.** Erro lógico
  secundário, não afeta o reprocesso.
- `SEM_DATA_COLETA` — sem data de coleta (cai no fallback do NI para o ano).

Essas flags são **filtráveis** na UI e exibidas como badge/coluna. O objetivo é o usuário
auditar visualmente, não o sistema esconder dados.

### 3.5 Reclassificação 2026 (exceção à regra 3.1)

Amostras de prefixo **D**, com NI já numerado em 2026 (`ni_ano=2026`) e número de **1 a 976**,
pertencem à série 2026 mesmo que a coleta tenha caído no fim de dez/2025. Para esse grupo o
`ano_verdade` é **forçado a 2026** (sobrepondo "a coleta vence"), elas **perdem a flag
`ANO_NI_DIVERGE`** e reordenam após as de 2025. Regra fixa em `parsing.reclassificar_2026`
(vale em reimports) + migração idempotente `db._reclassificar_2026` que reconcilia bancos já
populados (remapeia `D{n}/25→D{n}/26`, preserva progresso/rejeição e atualiza `eventos` via
`ON UPDATE CASCADE`). **73 amostras** movidas: contagens passam de 3.488/2.018 para
**3.415/2.091**.

---

## 4. Modelo de dados (PostgreSQL)

Tabela principal `amostras`. Tabela de auditoria leve `eventos`. Schema em `src/db.py`.

```sql
CREATE TABLE amostras (
    chave               TEXT PRIMARY KEY,         -- "D1264/25"
    prefixo             TEXT NOT NULL,            -- "D", "SR", "FA", "H"
    numero_sequencial   INTEGER NOT NULL,         -- 1264  (chave de ordenação)
    ano_verdade         INTEGER NOT NULL,         -- 2025  (da Data da Coleta)
    ni_original         TEXT,                     -- "D1264/25" como veio
    ni_ano              INTEGER,                  -- ano embutido no NI (para auditoria)
    requisicao          TEXT,
    municipio           TEXT,
    data_coleta         DATE,
    data_sintomas       DATE,
    caso                TEXT,                     -- contexto histórico

    -- FLUXO DE REPROCESSO (o que o sistema controla)
    coletada            INTEGER NOT NULL DEFAULT 0,   -- 0/1
    extraida            INTEGER NOT NULL DEFAULT 0,
    pcr_feito           INTEGER NOT NULL DEFAULT 0,
    data_coletada       TIMESTAMP,
    data_extraida       TIMESTAMP,
    data_pcr            TIMESTAMP,
    obs_reprocesso      TEXT,

    -- REJEIÇÃO (estado terminal alternativo)
    rejeitada           INTEGER NOT NULL DEFAULT 0,
    motivo_rejeicao     TEXT,
    data_rejeicao       TIMESTAMP,

    -- METADADOS / AUDITORIA
    n_origem            INTEGER NOT NULL DEFAULT 1,   -- linhas brutas que originaram a amostra
    flags               TEXT DEFAULT '',              -- "ANO_NI_DIVERGE;COLETA_ANTES_SINTOMA"
    importado_em        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    atualizado_em       TIMESTAMP
);

CREATE INDEX idx_ordem ON amostras (ano_verdade, prefixo, numero_sequencial);
CREATE INDEX idx_municipio ON amostras (municipio);
CREATE INDEX idx_flags ON amostras (flags);

CREATE TABLE eventos (
    id          BIGSERIAL PRIMARY KEY,
    chave       TEXT NOT NULL REFERENCES amostras(chave) ON UPDATE CASCADE,
    campo       TEXT NOT NULL,        -- "coletada" | "extraida" | "pcr_feito" | "rejeitada"
    valor_novo  TEXT,
    em          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Fluxo de fases — avanço ESTRITO

O reprocesso é sequencial e o avanço é **estrito (bloqueia)**: não se pode marcar `extraida`
sem `coletada`, nem `pcr_feito` sem `extraida`; o banco (`db.avancar_fase`) recusa a transição.
Desmarcar uma etapa também limpa as posteriores (`db.retroceder_fase`). A UI (`app.py`) é
organizada em **abas por fase** (Geral + Coletadas + Extraídas + PCR feito): a Geral mostra
todas as amostras com badge de fase e marca "Coletada" em lote; amostras que já têm status não
reentram. Cada amostra cai em exatamente uma fase (partição derivada dos 3 booleanos).

### Rejeição

Além das 3 etapas, uma amostra pode ser **rejeitada** — estado terminal alternativo para quando
não há volume suficiente / a amostra não foi encontrada. Regras: **só se rejeita amostra
PENDENTE**; **motivo obrigatório** (`Volume Insuficiente` | `Não Encontrada`); rejeitada **não
reentra** no fluxo; é possível **reverter** (volta a Pendente). Há uma aba "Rejeitadas" e a
partição de fases exclui rejeitadas das demais (`... AND rejeitada = 0`). Funções:
`db.rejeitar`, `db.reverter_rejeicao`.

---

## 5. Arquitetura de arquivos

```
DENV/
├── ESPECIFICACAO.md           # este documento
├── pyproject.toml             # deps: nicegui, pandas, openpyxl, psycopg2-binary
├── data/
│   └── dengue_coleta_dentro_prazo_mun_ordenado.xlsx
├── src/
│   ├── parsing.py             # parse_ni(), ano_verdade(), flags, reclassificar_2026
│   ├── importer.py            # xlsx -> dedup -> PostgreSQL (idempotente)
│   ├── db.py                  # conexão, schema, queries, fluxo de fases
│   ├── export.py              # export da visão atual em xlsx/csv
│   ├── auth.py                # login por e-mail e senha
│   └── app.py                 # UI NiceGUI (AG-Grid + filtros + abas por fase)
└── tests/                     # pytest (parsing + banco com DATABASE_URL)
```

---

## 6. Componentes da UI (NiceGUI)

### 6.1 Grade principal — `ui.aggrid`

- Colunas: NI (display), Número (numérico/ordenável), Ano, Município, Data Coleta,
  Data 1º Sintoma, Caso, **Fase** (badge), Flags, n_origem.
- **Ordenação default** pela chave numérica canônica (Seção 3.3).
- `:getRowId` pela `chave` para seleção estável sem re-render total.

### 6.2 Cabeçalho com métricas

Cards de contagem: total, coletadas, extraídas, PCR feito, rejeitadas — com **% do total** por
etapa. Atualizam ao filtrar.

### 6.3 Filtros (painel global) e ações em lote

Busca por NI (substring, case-insensitive), Ano, Município e Flags; métricas e abas refletem o
subconjunto filtrado (`db.construir_filtro` / `db.valores_distintos`). Seleção múltipla →
avançar/rejeitar em massa (operação comum de bancada).

### 6.4 Export

Botão "Exportar" por aba → **xlsx/csv da visão atual** (fase + filtro + ordenação canônica) via
`src/export.py` e `ui.download`. Só colunas do reprocesso (NI, número, ano, município, datas,
fase, etapas Sim/Não, motivo de rejeição, flags, n_origem) — **nunca** as colunas antigas.

---

## 7. Importador (`importer.py`) — comportamento exigido

1. Ler o xlsx; parsear datas explicitamente.
2. Descartar linhas sem NI / NI inválido (log: deve bater ~1.276).
3. Parsear NI: regex `^([A-Za-z]*)(\d+)\s*/\s*(\d+)$`, prefixo default `D` se vazio, ano `20YY`.
4. Calcular `ano_verdade` (Data da Coleta > NI), aplicando a reclassificação 2026 (Seção 3.5).
5. Agrupar por `chave`; para campos descritivos (município, requisição, caso, datas) usar a
   linha mais recente por Data da Coleta dentro do grupo.
6. Calcular `n_origem` (tamanho do grupo) e `flags`.
7. **Idempotente**: re-rodar o import NÃO zera o progresso de reprocesso já marcado.
   UPSERT (`ON CONFLICT(chave)`) atualiza só os campos descritivos/flags e preserva
   `coletada/extraida/pcr_feito`/rejeição e suas datas. A inserção é feita em **lote**
   (`execute_batch`) para minimizar viagens de rede ao banco remoto.
8. Asserts de sanidade: total = 5.506; 2025 = 3.415; 2026 = 2.091 (pós-reclassificação).

---

## 8. Convenções

- Mensagens de UI em **português (BR)**; código/docstrings em português ou inglês técnico, consistente.
- Sem dependências pesadas (Django/DRF) — a ferramenta é leve.
- Não recriar as colunas de resultado antigo como status do reprocesso.
- Sempre que tocar dados, gravar evento em `eventos` (auditoria mínima).
- O xlsx de origem é read-only; nunca sobrescrevê-lo.
- Antes de qualquer cálculo "hardcoded", preferir derivar dos dados.
