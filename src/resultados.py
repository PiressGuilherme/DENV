"""Import de resultados de PCR (Ct por sorotipo) a partir de xlsx/csv.

Formato esperado do arquivo (uma linha por amostra):

    NI, DEN1, DEN2, DEN3, DEN4

Célula de sorotipo vazia = não detectado; preenchida = valor de Ct.

Como export.py, este módulo é PURO: recebe bytes + um retrato do banco e devolve
um plano de import, sem tocar em conexão nem em UI. Quem persiste é
``db.gravar_resultados``; quem exibe é app.py. Isso torna toda a triagem
testável sem subir servidor nem PostgreSQL.

Regras de triagem (decisões do usuário):
    - Só grava em amostras que estão em 'PCR feito'.
    - NUNCA sobrescreve resultado já existente — pula e reporta.
    - NI que não resolve para exatamente uma amostra é reportado, nunca adivinhado.
"""

from __future__ import annotations

import io
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional

import pandas as pd

from src import db
from src.parsing import montar_chave, parse_ni

# --------------------------------------------------------------------------- #
# Cabeçalhos aceitos                                                            #
# --------------------------------------------------------------------------- #
# A planilha vem do equipamento/bancada e a grafia varia ("DEN1", "DENV-1",
# "Den 1"). Normalizamos o cabeçalho (sem acento, sem separador, maiúsculo) e
# comparamos contra os aliases — em vez de exigir uma grafia exata.

ALIASES_NI: frozenset[str] = frozenset(
    {"NI", "NUMEROINTERNO", "NINTERNO", "NUMINTERNO", "AMOSTRA"}
)

# Texto que representa "não detectado" numa célula de Ct. Comparado após
# _normalizar_marcador (acento removido, maiúsculo, espaços colapsados) —
# NÃO após _normalizar_cabecalho, que descarta pontuação e faria qualquer
# string de lixo ("??") colapsar em "" e passar por negativo.
NAO_DETECTADO: frozenset[str] = frozenset(
    {"-", "--", "NEG", "NEGATIVO", "ND", "NAO DETECTADO", "NAO DETECTAVEL",
     "NAODETECTADO", "NAODETECTAVEL", "UNDET", "UNDETERMINED", "NA", "N/A", "0"}
)

# --------------------------------------------------------------------------- #
# Motivos de exclusão                                                           #
# --------------------------------------------------------------------------- #

MOTIVO_NI_AUSENTE = "NI ausente"
MOTIVO_NI_INVALIDO = "NI inválido"
MOTIVO_NI_DUPLICADO = "NI duplicado no arquivo"
MOTIVO_NAO_ENCONTRADO = "NI não encontrado"
MOTIVO_AMBIGUO = "NI ambíguo (mais de um ano)"
MOTIVO_FORA_DE_FASE = "Fora da fase PCR feito"
MOTIVO_JA_TEM_RESULTADO = "Já tem resultado"
MOTIVO_CT_INVALIDO = "Ct inválido"
MOTIVO_SEM_CT = "Sem nenhum Ct"


class ArquivoInvalido(Exception):
    """Arquivo ilegível ou sem as colunas obrigatórias."""


# --------------------------------------------------------------------------- #
# Normalização                                                                  #
# --------------------------------------------------------------------------- #


def _normalizar_cabecalho(texto: object) -> str:
    """'Número Interno' -> 'NUMEROINTERNO'; 'DENV-1' -> 'DENV1'."""
    s = unicodedata.normalize("NFKD", str(texto))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.upper() if c.isalnum())


def _normalizar_marcador(texto: str) -> str:
    """'Não Detectado' -> 'NAO DETECTADO'. Preserva pontuação (ver NAO_DETECTADO)."""
    s = unicodedata.normalize("NFKD", texto)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.upper().split())


def _alias_sorotipo(sorotipo: str) -> frozenset[str]:
    """Grafias aceitas de um sorotipo ('DEN1' -> {'DEN1', 'DENV1', 'D1'})."""
    numero = sorotipo[-1]
    return frozenset({f"DEN{numero}", f"DENV{numero}", f"D{numero}", sorotipo.upper()})


def _como_ct(valor: object) -> tuple[Optional[float], bool]:
    """Converte célula em Ct.

    Returns:
        (ct, valido). ct=None significa "não detectado". valido=False sinaliza
        conteúdo que não é nem número nem marcador de negativo reconhecido —
        nesse caso a linha inteira é recusada, em vez de virar um None silencioso
        que pareceria um negativo legítimo.
    """
    if valor is None:
        return None, True
    try:
        if pd.isna(valor):
            return None, True
    except (TypeError, ValueError):
        pass

    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        ct = float(valor)
    else:
        texto = str(valor).strip()
        if not texto:
            return None, True
        if _normalizar_marcador(texto) in NAO_DETECTADO:
            return None, True
        # Planilha PT-BR usa vírgula decimal.
        try:
            ct = float(texto.replace(",", "."))
        except ValueError:
            return None, False

    # Ct 0 não existe numa PCR real; tratamos como célula "zerada" = negativo.
    if ct == 0:
        return None, True
    if not (db.CT_MIN < ct <= db.CT_MAX):
        return None, False
    return ct, True


# --------------------------------------------------------------------------- #
# Leitura do arquivo                                                            #
# --------------------------------------------------------------------------- #


def ler_arquivo(conteudo: bytes, nome: str) -> pd.DataFrame:
    """Lê xlsx/csv em DataFrame com colunas já canonizadas ('ni', 'den1_ct', ...).

    Raises:
        ArquivoInvalido: extensão não suportada, arquivo ilegível ou faltando a
            coluna de NI / todas as colunas de sorotipo.
    """
    extensao = nome.lower().rsplit(".", 1)[-1] if "." in nome else ""
    try:
        if extensao in ("xlsx", "xlsm", "xls"):
            df = pd.read_excel(io.BytesIO(conteudo), dtype=object)
        elif extensao == "csv":
            df = _ler_csv(conteudo)
        else:
            raise ArquivoInvalido(
                f"Extensão não suportada: '.{extensao}'. Use .xlsx ou .csv."
            )
    except ArquivoInvalido:
        raise
    except Exception as e:
        raise ArquivoInvalido(f"Não foi possível ler o arquivo: {e}") from e

    return _canonizar_colunas(df)


def _ler_csv(conteudo: bytes) -> pd.DataFrame:
    """CSV tolerante a separador (',' ou ';') e encoding (utf-8/BOM/latin-1)."""
    ultimo_erro: Optional[Exception] = None
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            # sep=None + engine='python' deixa o pandas inferir o separador —
            # planilhas PT-BR salvas do Excel saem com ';'.
            return pd.read_csv(
                io.BytesIO(conteudo), dtype=object, sep=None, engine="python",
                encoding=encoding,
            )
        except Exception as e:  # noqa: BLE001 — tenta o próximo encoding
            ultimo_erro = e
    raise ArquivoInvalido(f"CSV ilegível: {ultimo_erro}")


def _canonizar_colunas(df: pd.DataFrame) -> pd.DataFrame:
    """Mapeia os cabeçalhos do arquivo para nomes internos e valida presença."""
    mapa: dict[str, str] = {}
    for coluna in df.columns:
        norm = _normalizar_cabecalho(coluna)
        if norm in ALIASES_NI:
            mapa[coluna] = "ni"
            continue
        for sorotipo in db.SOROTIPOS:
            if norm in _alias_sorotipo(sorotipo):
                mapa[coluna] = db.coluna_ct(sorotipo)
                break

    canonizadas = set(mapa.values())
    if "ni" not in canonizadas:
        raise ArquivoInvalido(
            "Coluna de NI não encontrada. Esperado um cabeçalho 'NI' "
            f"(colunas lidas: {', '.join(str(c) for c in df.columns)})."
        )
    if not canonizadas & set(db.COLUNAS_CT):
        raise ArquivoInvalido(
            "Nenhuma coluna de sorotipo encontrada. Esperado DEN1, DEN2, DEN3 "
            "e/ou DEN4."
        )

    df = df.rename(columns=mapa)
    # Descarta colunas extras e duplicadas, preservando a ordem canônica.
    manter = ["ni"] + [c for c in db.COLUNAS_CT if c in canonizadas]
    return df.loc[:, ~df.columns.duplicated()][manter]


# --------------------------------------------------------------------------- #
# Plano de import                                                               #
# --------------------------------------------------------------------------- #


@dataclass
class LinhaResultado:
    """Uma linha do arquivo, já resolvida e triada."""

    linha_num: int                          # 1-based, como o usuário vê no Excel
    ni: str
    cts: dict[str, Optional[float]] = field(default_factory=dict)
    chave: Optional[str] = None             # resolvida contra o banco
    motivo: Optional[str] = None            # None = aplicável

    @property
    def aplicavel(self) -> bool:
        return self.motivo is None

    def para_registro(self) -> dict:
        """Formato consumido por db.gravar_resultados."""
        return {"chave": self.chave, **{c: self.cts.get(c) for c in db.COLUNAS_CT}}


@dataclass
class PlanoImport:
    """Resultado da triagem: o que será gravado e o que foi descartado (e por quê)."""

    aplicaveis: list[LinhaResultado] = field(default_factory=list)
    ignoradas: list[LinhaResultado] = field(default_factory=list)
    linhas_lidas: int = 0

    def por_motivo(self) -> dict[str, list[LinhaResultado]]:
        """Ignoradas agrupadas por motivo, para o relatório da UI."""
        grupos: dict[str, list[LinhaResultado]] = {}
        for linha in self.ignoradas:
            grupos.setdefault(linha.motivo or "?", []).append(linha)
        return dict(sorted(grupos.items(), key=lambda kv: -len(kv[1])))

    def registros(self) -> list[dict]:
        return [l.para_registro() for l in self.aplicaveis]


def _montar_indice(linhas_banco: Iterable[dict]) -> tuple[dict, dict]:
    """Índices de resolução: por chave e por (prefixo, numero_sequencial)."""
    por_chave: dict[str, dict] = {}
    por_numero: dict[tuple[str, int], list[dict]] = {}
    for r in linhas_banco:
        por_chave[r["chave"]] = r
        por_numero.setdefault((r["prefixo"], r["numero_sequencial"]), []).append(r)
    return por_chave, por_numero


def _resolver(ni: str, por_chave: dict, por_numero: dict) -> tuple[Optional[dict], Optional[str]]:
    """Resolve um NI cru para a linha do banco.

    A chave usa o ANO-DE-VERDADE (ano da coleta), não o ano do NI: 'D1264/25'
    coletada em 2026 está gravada como 'D1264/26' (ver parsing.montar_chave e a
    reclassificação 2026). Casar só por string crua perderia essas amostras
    silenciosamente — daí o fallback por prefixo+número.
    """
    p = parse_ni(ni)
    if p is None:
        return None, MOTIVO_NI_INVALIDO

    exata = por_chave.get(montar_chave(p.prefixo, p.numero_sequencial, p.ni_ano))
    if exata is not None:
        return exata, None

    candidatas = por_numero.get((p.prefixo, p.numero_sequencial), [])
    if not candidatas:
        return None, MOTIVO_NAO_ENCONTRADO
    if len(candidatas) > 1:
        # Mesmo número em anos diferentes. Gravar Ct na amostra errada é pior do
        # que não gravar: reporta e deixa a decisão com o usuário.
        return None, MOTIVO_AMBIGUO
    return candidatas[0], None


def montar_plano(df: pd.DataFrame, linhas_banco: Iterable[dict]) -> PlanoImport:
    """Triagem completa do arquivo contra o retrato do banco."""
    por_chave, por_numero = _montar_indice(linhas_banco)
    plano = PlanoImport(linhas_lidas=len(df))
    vistos: dict[str, int] = {}

    colunas_ct = [c for c in db.COLUNAS_CT if c in df.columns]

    for pos, registro in enumerate(df.to_dict("records"), start=2):  # 2 = 1ª linha após o cabeçalho
        bruto = registro.get("ni")
        ni = "" if bruto is None or pd.isna(bruto) else str(bruto).strip()
        linha = LinhaResultado(linha_num=pos, ni=ni)

        if not ni:
            linha.motivo = MOTIVO_NI_AUSENTE
            plano.ignoradas.append(linha)
            continue

        # _normalizar_marcador (e não _normalizar_cabecalho): descartar a barra
        # faria 'D1/25' e 'D12/5' colidirem como se fossem a mesma amostra.
        chave_dedup = _normalizar_marcador(ni)
        if chave_dedup in vistos:
            linha.motivo = f"{MOTIVO_NI_DUPLICADO} (linha {vistos[chave_dedup]})"
            plano.ignoradas.append(linha)
            continue
        vistos[chave_dedup] = pos

        amostra, motivo = _resolver(ni, por_chave, por_numero)
        if motivo is not None:
            linha.motivo = motivo
            plano.ignoradas.append(linha)
            continue

        linha.chave = amostra["chave"]

        # Elegibilidade de fase. Amostras já sequenciadas seguem elegíveis:
        # pcr_feito continua 1, e resultado que chega atrasado é caso real.
        if amostra["rejeitada"] or not amostra["pcr_feito"]:
            linha.motivo = MOTIVO_FORA_DE_FASE
            plano.ignoradas.append(linha)
            continue

        if amostra["data_resultado"] is not None:
            linha.motivo = MOTIVO_JA_TEM_RESULTADO
            plano.ignoradas.append(linha)
            continue

        invalidas: list[str] = []
        for coluna in colunas_ct:
            ct, valido = _como_ct(registro.get(coluna))
            if not valido:
                invalidas.append(coluna.replace("_ct", "").upper())
            linha.cts[coluna] = ct

        if invalidas:
            linha.motivo = f"{MOTIVO_CT_INVALIDO} ({', '.join(invalidas)})"
            plano.ignoradas.append(linha)
            continue

        if all(v is None for v in linha.cts.values()):
            linha.motivo = MOTIVO_SEM_CT
            plano.ignoradas.append(linha)
            continue

        plano.aplicaveis.append(linha)

    return plano


# --------------------------------------------------------------------------- #
# Sorotipo derivado                                                             #
# --------------------------------------------------------------------------- #


def ct_detectado(valor: object) -> bool:
    """Retorna ``True`` somente para um Ct numérico realmente detectado.

    O import do termociclador preserva ``-1`` como sentinela de "não
    detectado". Por isso, testar apenas ``is not None`` produz falso positivo
    para resultados negativos.
    """
    if valor is None:
        return False
    try:
        return float(valor) > 0
    except (TypeError, ValueError):
        return False


def sorotipo_de(r) -> str:
    """Rótulo legível do sorotipo a partir dos Ct ('DENV-2', 'DENV-1+2', ...).

    É o que a bancada quer ler e filtrar — não os quatro Ct crus. Distingue
    'sem resultado' (nunca importado) de 'Não detectado' (importado, todos
    negativos) via data_resultado.
    """
    detectados = [
        sorotipo[-1]  # o número do sorotipo ('DEN2' -> '2')
        for sorotipo in db.SOROTIPOS
        if ct_detectado(r[db.coluna_ct(sorotipo)])
    ]
    if detectados:
        # Coinfecção vira 'DENV-1+2', sem repetir o prefixo.
        return "DENV-" + "+".join(detectados)
    return "Não detectado" if r["data_resultado"] is not None else ""


def ignoradas_para_csv(plano: PlanoImport) -> bytes:
    """CSV das linhas ignoradas, para o usuário corrigir a planilha de origem.

    Com centenas de NIs, ler os motivos na tela não escala — o usuário quer
    levar a lista de volta para a origem.
    """
    df = pd.DataFrame(
        [
            {"Linha": l.linha_num, "NI": l.ni, "Motivo": l.motivo}
            for l in plano.ignoradas
        ],
        columns=["Linha", "NI", "Motivo"],
    )
    return df.to_csv(index=False).encode("utf-8-sig")
