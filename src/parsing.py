"""Parsing e regras de negócio do Número Interno (NI) de amostras de dengue.

Este módulo é a base de toda a ordenação cronológica do sistema. As regras aqui
implementadas seguem a Seção 3 da ESPECIFICACAO.md (decisões do usuário — não reabrir):

- 3.1 Ano-de-verdade = ano da Data da Coleta (vence o ano embutido no NI).
- 3.2 Chave de amostra = "{prefixo}{numero_sequencial}/{ano_verdade_2digitos}".
- 3.3 numero_sequencial é INTEGER — é o que conserta o "A-Z" do Excel que coloca
      D1264 depois de D11633.
- 3.4 Inconsistências geram flags (texto separado por ';'), nunca bloqueiam.

O parser NÃO assume o prefixo "D": aceita qualquer prefixo alfabético (D, SR, FA,
H — e variações de caixa, que são normalizadas para maiúsculas). O sufixo de ano
pode vir com 2, 3 ou 4 dígitos ("25", "026", "2025"); todos são normalizados para
um ano de 4 dígitos.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Union

# Regex de NI: prefixo alfabético opcional, número sequencial, '/', ano.
# Espaços ao redor da barra são tolerados. Prefixo vazio assume default 'D'.
_NI_RE = re.compile(r"^\s*([A-Za-z]*)(\d+)\s*/\s*(\d+)\s*$")

PREFIXO_DEFAULT = "D"

# Limiar de plausibilidade do ano embutido no NI (Seção 3.4: ANO_NI_IMPOSSIVEL).
ANO_IMPOSSIVEL_MIN = 2027

# ---- Flags (Seção 3.4) -----------------------------------------------------
FLAG_ANO_NI_DIVERGE = "ANO_NI_DIVERGE"
FLAG_ANO_NI_IMPOSSIVEL = "ANO_NI_IMPOSSIVEL"
FLAG_COLETA_ANTES_SINTOMA = "COLETA_ANTES_SINTOMA"
FLAG_SEM_DATA_COLETA = "SEM_DATA_COLETA"


DateLike = Union[date, datetime, None]


@dataclass(frozen=True)
class NIParsed:
    """Resultado do parse de um Número Interno.

    Attributes:
        prefixo: prefixo alfabético normalizado em maiúsculas (ex.: "D", "SR").
        numero_sequencial: número como INTEGER — a chave de ordenação.
        ni_ano: ano de 4 dígitos embutido no NI (para auditoria).
    """

    prefixo: str
    numero_sequencial: int
    ni_ano: int


def _normalizar_ano(ano_raw: str) -> int:
    """Normaliza o sufixo de ano do NI para um ano de 4 dígitos.

    Casos reais observados na planilha: "25", "26" (2 díg.), "026"/"025" (3 díg.
    com zero à esquerda), "2025" (4 díg.), "28"/"29" (impossíveis, mas parseáveis).

    Regra:
        - 4 dígitos: usado como está (ex.: "2025" -> 2025).
        - 1–3 dígitos: interpretado como ano dentro do século 2000 (ex.: "26" ->
          2026, "026" -> 2026, "5" -> 2005).
    """
    n = int(ano_raw)
    if len(ano_raw) >= 4:
        return n
    return 2000 + n


def parse_ni(ni: Optional[str]) -> Optional[NIParsed]:
    """Faz o parse de um Número Interno bruto.

    Retorna ``None`` quando o NI é ausente ou não-parseável (ex.: "D3809" sem
    '/ano', string vazia, ``None``). O importador usa esse ``None`` para descartar
    a linha (Seção 7, passo 2).

    Args:
        ni: o Número Interno como veio da planilha (ex.: "D1264/25").

    Returns:
        NIParsed ou None.
    """
    if ni is None:
        return None
    texto = str(ni).strip()
    if not texto:
        return None

    m = _NI_RE.match(texto)
    if not m:
        return None

    prefixo_raw, numero_raw, ano_raw = m.groups()
    prefixo = (prefixo_raw or PREFIXO_DEFAULT).upper()
    numero_sequencial = int(numero_raw)
    ni_ano = _normalizar_ano(ano_raw)

    return NIParsed(
        prefixo=prefixo,
        numero_sequencial=numero_sequencial,
        ni_ano=ni_ano,
    )


def _ano_de(d: DateLike) -> Optional[int]:
    """Extrai o ano de um date/datetime, tolerando None/NaT."""
    if d is None:
        return None
    # pandas NaT e floats NaN não são date/datetime; trate como ausência.
    if isinstance(d, datetime):
        return d.year
    if isinstance(d, date):
        return d.year
    return None


# --------------------------------------------------------------------------- #
# Reclassificação 2026 (decisão posterior do usuário)                          #
# --------------------------------------------------------------------------- #
# Exceção à regra "a Data da Coleta vence" (3.1): as amostras de prefixo D com
# NI já numerado em 2026 e número de 1 a 976 pertencem à SÉRIE 2026, mesmo que a
# coleta tenha caído no fim de dezembro/2025. Para esse grupo, o ano-de-verdade
# é forçado a 2026 — assim elas perdem a flag ANO_NI_DIVERGE (ni_ano passa a
# bater com ano_verdade) e reordenam corretamente após as de 2025.
_RECLASS_2026_PREFIXO = "D"
_RECLASS_2026_NI_ANO = 2026
_RECLASS_2026_NUM_MIN = 1
_RECLASS_2026_NUM_MAX = 976


def reclassificar_2026(prefixo: str, numero_sequencial: int, ni_ano: Optional[int]) -> bool:
    """True se a amostra cai na regra de reclassificação para 2026."""
    return (
        prefixo == _RECLASS_2026_PREFIXO
        and ni_ano == _RECLASS_2026_NI_ANO
        and _RECLASS_2026_NUM_MIN <= numero_sequencial <= _RECLASS_2026_NUM_MAX
    )


def ano_verdade(
    ni_ano: Optional[int],
    data_coleta: DateLike,
    *,
    prefixo: Optional[str] = None,
    numero_sequencial: Optional[int] = None,
) -> Optional[int]:
    """Calcula o ano-de-verdade da amostra (Seção 3.1).

    A Data da Coleta vence o ano embutido no NI. Se a Data da Coleta estiver
    ausente, cai para o ano do NI. Se ambos faltarem, retorna ``None``.

    Exceção (reclassificação 2026): se ``prefixo``/``numero_sequencial`` forem
    informados e a amostra cair em :func:`reclassificar_2026`, o ano é forçado a
    2026, sobrepondo a Data da Coleta.

    Args:
        ni_ano: ano de 4 dígitos embutido no NI (ou None).
        data_coleta: a Data da Coleta (date/datetime) ou None/NaT.
        prefixo: prefixo da amostra (para a regra de reclassificação).
        numero_sequencial: número da amostra (para a regra de reclassificação).

    Returns:
        Ano-de-verdade (int) ou None.
    """
    if (
        prefixo is not None
        and numero_sequencial is not None
        and reclassificar_2026(prefixo, numero_sequencial, ni_ano)
    ):
        return _RECLASS_2026_NI_ANO

    ano_coleta = _ano_de(data_coleta)
    if ano_coleta is not None:
        return ano_coleta
    return ni_ano


def montar_chave(prefixo: str, numero_sequencial: int, ano: int) -> str:
    """Monta a chave de amostra única (Seção 3.2): "D1264/25".

    O ano é exibido em 2 dígitos na chave (formato do NI), mas a fonte de verdade
    para ordenação continua sendo ``numero_sequencial`` (int) + ``ano`` (int).
    """
    return f"{prefixo}{numero_sequencial}/{ano % 100:02d}"


def calcular_flags(
    *,
    ni_ano: Optional[int],
    ano_verdade_: Optional[int],
    data_coleta: DateLike,
    data_sintomas: DateLike,
) -> str:
    """Calcula as flags de inconsistência (Seção 3.4).

    Sinaliza, nunca corrige nem bloqueia. Retorna string com flags separadas por
    ';' (vazia se nenhuma). Ordem estável para facilitar testes/filtros.

    Flags:
        - SEM_DATA_COLETA: sem Data da Coleta (caiu no fallback do NI).
        - ANO_NI_DIVERGE: ano do NI != ano-de-verdade (inclui viradas de ano
          legítimas, ex.: D001/26 coletada em 30/12/2025).
        - ANO_NI_IMPOSSIVEL: ano do NI >= 2027 (erro de digitação tipo D828/29).
        - COLETA_ANTES_SINTOMA: Data da Coleta anterior ao 1º Sintoma.
    """
    flags: list[str] = []

    tem_coleta = _ano_de(data_coleta) is not None
    if not tem_coleta:
        flags.append(FLAG_SEM_DATA_COLETA)

    # Divergência só faz sentido quando temos ambos os anos.
    if (
        ni_ano is not None
        and ano_verdade_ is not None
        and ni_ano != ano_verdade_
    ):
        flags.append(FLAG_ANO_NI_DIVERGE)

    if ni_ano is not None and ni_ano >= ANO_IMPOSSIVEL_MIN:
        flags.append(FLAG_ANO_NI_IMPOSSIVEL)

    ac = _ano_de(data_coleta)
    asint = _ano_de(data_sintomas)
    # Comparação completa de datas (não só ano) quando ambas presentes.
    if data_coleta is not None and data_sintomas is not None and ac is not None and asint is not None:
        dc = data_coleta if isinstance(data_coleta, datetime) else None
        ds = data_sintomas if isinstance(data_sintomas, datetime) else None
        # Aceita tanto datetime quanto date na comparação.
        coleta_cmp = data_coleta.date() if isinstance(data_coleta, datetime) else data_coleta
        sintoma_cmp = data_sintomas.date() if isinstance(data_sintomas, datetime) else data_sintomas
        if isinstance(coleta_cmp, date) and isinstance(sintoma_cmp, date) and coleta_cmp < sintoma_cmp:
            flags.append(FLAG_COLETA_ANTES_SINTOMA)

    return ";".join(flags)


def chave_ordenacao(prefixo: str, numero_sequencial: int, ano_verdade_: int) -> tuple[int, str, int]:
    """Chave de ordenação cronológica canônica (Seção 3.3).

    Espelha em Python o ``ORDER BY ano_verdade ASC, prefixo ASC,
    numero_sequencial ASC`` do banco. ``numero_sequencial`` é INTEGER — é o que
    garante D1264 < D11633.
    """
    return (ano_verdade_, prefixo, numero_sequencial)
