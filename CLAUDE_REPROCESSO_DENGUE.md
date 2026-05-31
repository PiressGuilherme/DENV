# Sistema de Controle de Reprocesso — Amostras Dengue (LACEN-RS)

> Documento de instruções iniciais para Claude Code (Opus 4.8) no VS Code.
> Stack escolhida: **NiceGUI + SQLite + pandas**. Local, single-user, controle interno.
> Cole este arquivo na raiz do projeto como `CLAUDE.md` e use a Seção 9 como primeiro prompt.

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

O sistema NÃO é o HELIX e não deve tentar ser. É uma ferramenta de tracking enxuta.

---

## 2. A planilha de origem — fatos confirmados

Arquivo: `dengue_coleta_dentro_prazo_mun_ordenado.xlsx`, aba única `dengue_coleta_dentro_prazo_mun_`.

- **17.624 linhas brutas**, 20 colunas.
- Colunas: `Requisição`, `Número Interno`, `Número Interno (limpo)`, `Retirada`, `PCR Feito`,
  `Observação` (100% vazia), `Ano`, `Data do 1º Sintomas`, `Data da Coleta`, `Dif Dias`,
  `Municipio de Residência`, `Metodologia`, `Status Exame`, `1º–6º Campo Resultado`, `Caso`.
- As datas (`Data da Coleta`, `Data do 1º Sintomas`) já vêm como **datetime real** (não serial Excel).
- `Retirada` e `PCR Feito` existem mas estão **100% FALSE** — campos do fluxo antigo, ignore-os
  e crie campos próprios para o reprocesso.

### Deduplicação (números reais já validados)

- **1.276 linhas ignoradas**: sem Número Interno (1.271) ou NI não-parseável (5).
- **16.348 linhas com NI válido**, que correspondem a **5.506 amostras únicas**.
- ~10.842 linhas são duplicatas: a mesma amostra repetida porque passou por NS1
  (Enzimaimunoensaio) e/ou RT-PCR, às vezes com a metodologia cancelada e relançada.
  A maioria das amostras (4.803) aparece em exatamente 3 linhas; algumas em até 7.

### Distribuição das amostras únicas por ano (ano-de-verdade = ano da coleta)

- 2025: 3.488 amostras
- 2026: 2.018 amostras

### Prefixos de Número Interno encontrados

`D` (esmagadora maioria, 16.305 linhas), `SR` (38), `FA` (3), `H` (2). O parser deve aceitar
qualquer prefixo alfabético, não assumir só `D`.

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
controle. Metodologia antiga é descartada (decisão do usuário), mas **guardar a contagem de
linhas brutas que originaram a amostra** (campo `n_origem`) como proxy de "já foi mexida antes".

### 3.3 Ordenação cronológica padrão (a dor central)

A visualização padrão e sempre disponível ordena por:

```sql
ORDER BY ano_verdade ASC, prefixo ASC, numero_sequencial ASC
```

`numero_sequencial` é **INTEGER**, nunca texto — é isso que conserta o "A-Z" do Excel que
coloca D1264 depois de D11633. No AG-Grid, a coluna do número deve ter tipo numérico para a
ordenação nativa funcionar; exiba o NI formatado (`D1264/25`) numa coluna separada de display.

### 3.4 Inconsistências — sinalizar, nunca bloquear

Marcar cada amostra com flags (campo texto, separado por `;`). Contagens reais:

- `ANO_NI_DIVERGE` — ano do NI ≠ ano da coleta. **78 amostras.** Inclui viradas de ano legítimas
  (ex.: `D001/26` coletada em 30/12/2025). Sinalizar, não corrigir o NI.
- `ANO_NI_IMPOSSIVEL` — ano do NI ≥ 2027 (erros tipo `D26585/28`, `D828/29`). **2 amostras.**
  O ano-de-verdade já as reposiciona corretamente via Data da Coleta.
- `COLETA_ANTES_SINTOMA` — Data da Coleta anterior ao 1º Sintoma. **25 amostras.** Erro lógico
  secundário, não afeta o reprocesso.
- `SEM_DATA_COLETA` — sem data de coleta (cai no fallback do NI para o ano).

Essas flags devem ser **filtráveis** na UI e exibidas como badge/coluna. O objetivo é o usuário
auditar visualmente, não o sistema esconder dados.

---

## 4. Modelo de dados (SQLite)

Banco: `reprocesso.db`. Tabela principal `amostras`. Tabela de auditoria leve `eventos`.

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
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chave       TEXT NOT NULL REFERENCES amostras(chave),
    campo       TEXT NOT NULL,        -- "coletada" | "extraida" | "pcr_feito" | "obs"
    valor_novo  TEXT,
    em          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Regra de etapas: o reprocesso é sequencial. A UI pode alertar (não bloquear) se alguém marcar
`pcr_feito` sem `extraida`. Decisão suave — avisar, deixar passar.

> **ATUALIZAÇÃO (decisão posterior do usuário — sobrepõe o parágrafo acima):** o avanço de
> fase passou a ser **ESTRITO (bloquear)**. Não se pode marcar `extraida` sem `coletada`, nem
> `pcr_feito` sem `extraida`; o banco (`db.avancar_fase`) recusa a transição. Desmarcar uma
> etapa também limpa as etapas posteriores (`db.retroceder_fase`). A UI (`app.py`) é organizada
> em **abas por fase** (Geral + Coletadas + Extraídas + PCR feito): a Geral mostra todas as
> amostras com badge de fase e marca "Coletada" em lote; amostras que já têm status não
> reentram no fluxo. Cada amostra cai em exatamente uma fase (partição derivada dos 3 booleanos).

> **REJEIÇÃO (adição posterior do usuário):** além das 3 etapas, uma amostra pode ser
> **rejeitada** — estado terminal alternativo para quando não há volume suficiente / a amostra
> não foi encontrada, e portanto não será retirada do estoque para extração/PCR. Colunas novas:
> `rejeitada` (0/1), `motivo_rejeicao`, `data_rejeicao` (migração leve via `ALTER TABLE` para
> bancos existentes). Regras: **só se rejeita amostra PENDENTE**; **motivo obrigatório**
> (`Volume Insuficiente` | `Não Encontrada`); rejeitada **não reentra** no fluxo; é possível
> **reverter** (volta a Pendente). Há uma **aba "Rejeitadas"** após "PCR feito" e a partição de
> fases passou a excluir rejeitadas das demais (`... AND rejeitada = 0`). Funções:
> `db.rejeitar`, `db.reverter_rejeicao`.

> **FILTROS (Fase 4):** painel global com busca por NI (substring), Ano, Município e Flags;
> as métricas e todas as abas refletem o subconjunto filtrado. Lógica em `db.construir_filtro`
> / `db.valores_distintos`.

> **EXPORT (Fase 5):** botão "Exportar" por aba gera **xlsx/csv da visão atual** (fase + filtro +
> ordenação canônica) via `src/export.py` e `ui.download`. Só colunas do reprocesso (NI, número,
> ano, município, datas, fase, etapas Sim/Não, motivo de rejeição, flags, n_origem) — **nunca** as
> colunas antigas. Cards de métrica agora mostram também o % do total por etapa.

---

## 5. Arquitetura de arquivos

```
reprocesso-dengue/
├── CLAUDE.md                  # este documento
├── pyproject.toml             # deps: nicegui, pandas, openpyxl
├── reprocesso.db              # gerado (gitignore)
├── data/
│   └── dengue_coleta_dentro_prazo_mun_ordenado.xlsx
├── src/
│   ├── parsing.py             # parse_ni(), ano_verdade(), flags
│   ├── importer.py            # xlsx -> dedup -> SQLite (idempotente)
│   ├── db.py                  # conexão, queries, ordenação canônica
│   └── app.py                 # UI NiceGUI (AG-Grid + filtros + edição)
└── tests/
    └── test_parsing.py        # casos: D1264 vs D11633, D828/29, virada de ano
```

---

## 6. Componentes da UI (NiceGUI)

### 6.1 Grade principal — `ui.aggrid`

- Colunas: NI (display), Número (numérico/oculto-ordenável), Ano, Município, Data Coleta,
  Caso, **Coletada / Extraída / PCR** (checkbox editável), Flags (badge), n_origem.
- **Ordenação default** pela chave numérica canônica (Seção 3.3).
- `cellEditing` nas três checkboxes; ao editar, persistir no SQLite e gravar em `eventos`.
- Use `:getRowId` pela `chave` para edição estável sem re-render total.
- Filtros laterais: ano, município (dropdown), status de cada etapa, presença de flags.
- Busca rápida por NI.

### 6.2 Cabeçalho com métricas

Cards de contagem: total de amostras, % coletadas, % extraídas, % PCR feito, nº com flags.
Atualizam ao filtrar.

### 6.3 Ações em lote

Selecionar várias linhas → marcar "coletada" em massa (operação comum de bancada).

### 6.4 Export

Botão "Exportar visão atual" → xlsx/csv respeitando filtros e ordenação corrente. Útil para
relatório de bancada. (Sem reintroduzir as colunas antigas, só o que importa ao reprocesso.)

---

## 7. Importador (`importer.py`) — comportamento exigido

1. Ler o xlsx com `dtype=str` onde fizer sentido; parsear datas explicitamente.
2. Descartar linhas sem NI / NI inválido (log: quantas, deve bater ~1.276).
3. Parsear NI: regex `^([A-Za-z]*)(\d+)\s*/\s*(\d+)$`, prefixo default `D` se vazio, ano `20YY`.
4. Calcular `ano_verdade` (Data da Coleta > NI).
5. Agrupar por `chave`; para campos descritivos (município, requisição, caso, datas) usar a
   linha mais recente por Data da Coleta dentro do grupo.
6. Calcular `n_origem` (tamanho do grupo) e `flags`.
7. **Idempotente**: re-rodar o import NÃO pode zerar o progresso de reprocesso já marcado.
   Fazer UPSERT que atualiza só os campos descritivos/flags e preserva `coletada/extraida/pcr_feito`
   e suas datas. Validar isso com teste.
8. Ao final, asserts de sanidade: total ≈ 5.506 amostras; 2025 ≈ 3.488; 2026 ≈ 2.018.

---

## 8. Plano de execução em fases (para o Claude Code seguir)

**Fase 0 — Scaffold.** Criar estrutura, `pyproject.toml`, venv, instalar deps. Colocar o xlsx em `data/`.

**Fase 1 — Parsing + testes.** Implementar `parsing.py` e `tests/test_parsing.py` PRIMEIRO.
Casos obrigatórios: ordenação D1264 < D11633; `D828/29` cai em 2026 pela coleta; `D001/26`
coletada em 2025 recebe `ANO_NI_DIVERGE`. Rodar `pytest` e passar antes de seguir.

**Fase 2 — Importador + DB.** `db.py` cria schema; `importer.py` popula. Validar contagens
(5.506 / 3.488 / 2.018). Testar idempotência: marcar uma amostra, reimportar, confirmar que
o flag de reprocesso sobreviveu.

**Fase 3 — UI mínima.** `app.py` com a grade AG-Grid, ordenação canônica e as três checkboxes
persistindo. Nada de filtros ainda — só provar que edita e ordena certo.

**Fase 4 — Filtros + métricas + busca.** Adicionar painel de filtros, cards de métricas, busca por NI.

**Fase 5 — Lote + export + polish.** Ações em massa, export filtrado, ajuste visual.

A cada fase: e me mostrar o que mudou antes de avançar.

---

## 9. Convenções para o Claude Code

- Idioma do código e docstrings: português ou inglês técnico, consistente. Mensagens de UI em
  **português (BR)**.
- Não introduzir Django, DRF ou dependências pesadas — o usuário quer leve e local.
- Não recriar as colunas de resultado antigo como status do reprocesso.
- Sempre que tocar dados, gravar evento em `eventos` (auditoria mínima).
- `reprocesso.db` no `.gitignore`. O xlsx de origem é read-only; nunca sobrescrevê-lo.
- Antes de qualquer cálculo "hardcoded", preferir derivar dos dados.
