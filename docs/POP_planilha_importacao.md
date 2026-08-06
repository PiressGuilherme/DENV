# POP — Preparação da planilha de importação (LACEN-RS / DENV)

Procedimento operacional padrão para gerar as planilhas que alimentam o sistema
de reprocesso de dengue a partir dos exports do GAL.

---

## 1. Exportar do GAL

Exporte os dados como CSV e salve em `data/` com os nomes `data1.csv`,
`data2.csv`, `data3.csv` (a quantidade pode variar; ajuste `ARQUIVOS` em
`scripts/merge_data.py`).

Formato esperado, que é o padrão do GAL:

| Item | Valor |
|---|---|
| Separador | `;` (ponto e vírgula) |
| Codificação | ISO-8859-1 (Latin-1) |
| Cabeçalho | 1ª linha, 110 colunas |

Os arquivos **podem se sobrepor** — o script remove duplicatas. Não é preciso
recortar períodos manualmente.

## 2. Gerar as planilhas

```bash
python -m scripts.merge_data
```

Saída em `data/`:

| Arquivo | Conteúdo |
|---|---|
| `dengue_consolidado.xlsx` | Acervo total — todas as amostras de dengue |
| `dengue_consolidado_pendentes.xlsx` | **Vai para o sistema** — sem as que já fizeram PCR |

O script faz, nesta ordem: empilha os CSVs → mantém só prefixo `D` → remove
linhas idênticas → colapsa para uma linha por amostra (preferindo a linha de
PCR) → ordena pela regra canônica → recorta as colunas → grava os dois arquivos.

## 3. Conferir antes de importar

```bash
python -m pytest -q          # requer DATABASE_URL; ver README
```

Abra a planilha e confirme:

- [ ] Aba chamada **`dengue_coleta_dentro_prazo_mun_`**
- [ ] Colunas de data alinhadas à **direita** no Excel (prova que são data, não texto)
- [ ] `Número Interno` sem repetição, ordenado por ano e depois por número
- [ ] `Data do 1º Sintomas` e `Data da Coleta` lado a lado

## 4. Importar

```bash
python -m src.importer data/dengue_consolidado_pendentes.xlsx
```

Reimportar é seguro e **idempotente**: o UPSERT atualiza só os campos
descritivos e preserva todo o progresso de bancada — etapas (coletada, extraída,
PCR feito, sequenciada), rejeição e os Ct do termociclador.

---

## Regras que a planilha precisa respeitar

### Colunas obrigatórias (grafia exata)

O importador localiza as colunas **pelo nome**, não pela posição. Ordem e
colunas extras são indiferentes, mas estas seis precisam existir com a grafia
exata, incluindo acentos e o ordinal `º`:

```
Requisição
Número Interno
Municipio de Residência      (sem acento em "Municipio" — é assim no GAL)
Data do 1º Sintomas
Data da Coleta
Caso
```

### Datas: sempre datetime, nunca texto

**Esta é a regra que mais causa erro silencioso.** O GAL mistura dois formatos
na mesma coluna (`26-04-2026` e `26/04/26`). Se as datas forem gravadas como
**texto**, uma data como `03/04/26` pode ser lida como **4 de março** em vez de
3 de abril — dia e mês trocados, sem nenhum aviso.

Isso não é cosmético: a `Data da Coleta` define o *ano-de-verdade*, que compõe a
**chave** da amostra. Data errada gera chave errada, e a amostra é duplicada em
vez de atualizada, deixando o progresso preso na linha antiga.

O `merge_data.py` já grava as datas como datetime real. Se montar uma planilha à
mão, formate as colunas de data como **Data** no Excel antes de salvar.

### Uma linha por amostra

O GAL traz uma linha por **exame** (NS1, IgM, ZDC), então a mesma amostra
aparece ~3 vezes. O sistema espera uma linha por amostra. O script resolve
escolhendo a linha de **PCR (ZDC)** e, no desempate, a de status mais avançado.

### Critério de "já fez PCR"

Uma amostra é excluída da planilha de importação quando tem linha ZDC **e**:

- `Status Exame` é `Resultado Liberado` ou `Resultado Cadastrado`, **ou**
- `Kit` está preenchido (kit de PCR consumido)

Os dois sinais são unidos por OR porque cada um sozinho perde casos reais: há
amostras liberadas sem Kit registrado, e amostras com Kit consumido cujo exame
foi cancelado depois. Kits de sorologia (ELISA, NS1) em linhas não-ZDC são
ignorados — não dizem nada sobre a PCR.

---

## Solução de problemas

| Sintoma | Causa provável |
|---|---|
| `Worksheet named '...' not found` | Aba com nome errado — precisa ser `dengue_coleta_dentro_prazo_mun_` |
| Muitas linhas ignoradas no import | `Número Interno` ausente ou sem `/ano` (ex.: `D3809` sem ano) |
| Amostra duplicada em dois anos | Data da Coleta lida como texto e com dia/mês trocados |
| `AssertionError` nas contagens | Rodou com `verificar_sanidade=True`; os valores são fixos na planilha de 2025 |
| Acentos quebrados (`Requisi��o`) | CSV lido como UTF-8 — o GAL exporta em ISO-8859-1 |
