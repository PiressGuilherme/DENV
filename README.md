# Reprocesso Dengue — LACEN-RS

Tracker local para controle do **reprocesso de amostras de dengue**. Acompanha cada
amostra por um fluxo de três etapas — **Coletada → Extraída → PCR feito** — além de um
estado terminal alternativo de **Rejeição** (volume insuficiente / não encontrada).

Stack: **NiceGUI + SQLite + pandas**. Single-user, roda 100% local no navegador.

> As regras de negócio e decisões de projeto estão em
> [`CLAUDE_REPROCESSO_DENGUE.md`](CLAUDE_REPROCESSO_DENGUE.md).

---

## Requisitos

- **Python 3.11+** (testado em 3.14)
- A planilha de origem em `data/dengue_coleta_dentro_prazo_mun_ordenado.xlsx`
  (já versionada neste repositório).

---

## Instalação

### Linux / macOS

```bash
# 1. Clonar e entrar na pasta
git clone https://github.com/PiressGuilherme/DENV.git
cd DENV

# 2. Criar e ativar o ambiente virtual
python3 -m venv .venv
source .venv/bin/activate

# 3. Instalar as dependências
pip install -r requirements.txt
```

> Em algumas distros o pacote de venv é separado. Se `python3 -m venv` falhar,
> instale-o antes — ex. Debian/Ubuntu: `sudo apt install python3-venv`.

### Windows (PowerShell)

```powershell
git clone https://github.com/PiressGuilherme/DENV.git
cd DENV
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> Se a ativação for bloqueada pela política de execução, rode uma vez:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

---

## Uso

Com o ambiente virtual ativado:

```bash
# 1. Popular o banco a partir da planilha (gera reprocesso.db)
python -m src.importer

# 2. Subir a aplicação
python -m src.app
```

Acesse no navegador: **http://localhost:8080**

O importador é **idempotente**: reexecutá-lo atualiza os campos descritivos da planilha
mas **preserva todo o progresso** de reprocesso/rejeição já registrado. Ao final ele
imprime contagens de sanidade (≈ 5.506 amostras únicas: 3.415 de 2025, 2.091 de 2026).

### Rodar os testes

```bash
pytest -q
```

---

## Estrutura

```
DENV/
├── data/                # planilha de origem (read-only)
├── src/
│   ├── parsing.py       # parse do Número Interno, ano-de-verdade, flags
│   ├── importer.py      # xlsx -> dedup -> SQLite (idempotente)
│   ├── db.py            # schema, queries, fluxo de fases, migrações
│   ├── export.py        # export da visão atual em xlsx/csv
│   └── app.py           # UI NiceGUI (abas por fase, filtros, lote, export)
├── tests/               # pytest
├── requirements.txt
└── reprocesso.db        # gerado pelo importer (não versionado)
```

`reprocesso.db` e `.venv/` são ignorados pelo git (ver [`.gitignore`](.gitignore)). A
planilha de origem é read-only e nunca é sobrescrita pela aplicação.

---

## Notas

- **Porta**: a aplicação sobe na 8080. Para mudar, ajuste `ui.run(..., port=...)` no
  fim de [`src/app.py`](src/app.py).
- **Acesso de outra máquina na rede**: o NiceGUI já escuta em todas as interfaces;
  basta liberar a porta 8080 no firewall e acessar por `http://<ip-da-maquina>:8080`.
- **Reset do banco**: apague `reprocesso.db` e rode `python -m src.importer` de novo
  (isto descarta todo o progresso de reprocesso/rejeição).
