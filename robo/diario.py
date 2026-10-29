"""Robô do Diário Oficial da DPE-AM para o painel do Polo do Baixo Amazonas.

Roda no GitHub Actions todo dia de madrugada:
1. descobre as edições novas em diario.defensoria.am.def.br;
2. baixa cada PDF e extrai o texto (inclusive tabelas);
3. separa os atos (portarias, resoluções...) e fica só com os que citam o polo,
   as comarcas (Parintins, Nhamundá, Barreirinha) ou um defensor do polo;
4. lê cada ato com regras fixas, sem IA e sem custo: separa os incisos,
   acha a DP do polo, o defensor cadastrado e o período;
5. marca como "auto" só o que é claro (DESIGNAR + uma DP do polo + defensor
   cadastrado + período válido); o resto fica para um admin conferir;
6. grava diario.json, que o site lê.

Variáveis de ambiente:
  DESDE              opcional: reler a partir desta edição (ex.: 2700)
  MAX_EDICOES        opcional: limite de edições por execução (padrão 40)
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import sys
import time
import unicodedata

import requests
from bs4 import BeautifulSoup

BASE = "https://diario.defensoria.am.def.br/"
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARQ_DIARIO = os.path.join(RAIZ, "diario.json")
ARQ_ESTADO = os.path.join(RAIZ, "robo", "estado.json")
ARQ_NOMES = os.path.join(RAIZ, "robo", "defensores.json")
MAX_EDICOES = int(os.environ.get("MAX_EDICOES") or 40)
CIDADES = ["baixo amazonas", "parintins", "nhamunda", "barreirinha"]
OUTROS_POLOS = ["medio amazonas", "medio solimoes", "medio madeira", "alto solimoes", "jurua", "purus",
                "rio negro", "alto rio negro", "madeira"]
MESES = {"janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
         "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12}
CATEGORIAS = ["designacao", "substituicao", "afastamento", "plantao", "lotacao", "coordenacao", "servidores", "geral"]
TIPOS_AF = ["ferias", "licenca", "folga", "viagem", "outro"]
SESSAO = requests.Session()
SESSAO.headers["User-Agent"] = "painel-polo-baixo-amazonas (robô do Diário; contato polobaixoamazonas@defensoria.am.def.br)"


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s.lower()).strip()


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- arquivos

def ler_json(caminho, padrao):
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return padrao


def gravar_json(caminho, dados):
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=1)
        f.write("\n")


# ---------------------------------------------------------------- edições

RE_EDICAO = re.compile(r"Edicao_(\d+)-(\d{4})(?:__publicada_em_(\d{1,2})_([A-Za-zçÇ]+)_de_(\d{4})|_(\d{2})(\d{2})(\d{4}))?", re.I)


def info_edicao(url: str):
    m = RE_EDICAO.search(url)
    if not m:
        return None
    num = int(m.group(1))
    data = ""
    if m.group(3):
        mes = MESES.get(norm(m.group(4)))
        if mes:
            data = f"{m.group(5)}-{mes:02d}-{int(m.group(3)):02d}"
    elif m.group(6):
        data = f"{m.group(8)}-{m.group(7)}-{m.group(6)}"
    return {"numero": num, "data": data, "url": url}


def links_da_pagina(url: str) -> list[str]:
    r = SESSAO.get(url, timeout=40)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")
    return [requests.compat.urljoin(url, a["href"]) for a in soup.find_all("a", href=True)]


def edicoes_publicadas() -> dict[int, dict]:
    achadas: dict[int, dict] = {}
    paginas = [BASE]
    try:
        links = links_da_pagina(BASE)
    except Exception as e:  # noqa: BLE001
        log("Não consegui abrir a página do Diário:", e)
        return achadas
    ano = str(dt.date.today().year)
    # pastas anuais e páginas de listagem do ano corrente
    for l in links:
        if l.startswith(BASE) and ano in l and ".pdf" not in l.lower():
            paginas.append(l)
    vistas = set()
    for p in paginas[:12]:
        if p in vistas:
            continue
        vistas.add(p)
        try:
            ls = links if p == BASE else links_da_pagina(p)
        except Exception:  # noqa: BLE001
            continue
        for l in ls:
            if ".pdf" in l.lower() and "Edicao_" in l:
                info = info_edicao(l)
                if info:
                    achadas.setdefault(info["numero"], info)
    return achadas


def procurar_edicao(numero: int, depois_de: str) -> dict | None:
    """Acha o PDF de uma edição que não apareceu nas listagens, testando as datas seguintes."""
    nomes_mes = {v: k for k, v in MESES.items()}
    nomes_mes[3] = "marco"
    base = dt.date.fromisoformat(depois_de) if depois_de else dt.date.today() - dt.timedelta(days=30)
    for i in range(0, 12):
        d = base + dt.timedelta(days=i)
        for mes_nome in {nomes_mes[d.month], "março" if d.month == 3 else nomes_mes[d.month]}:
            for dia in {str(d.day), f"{d.day:02d}"}:
                url = f"{BASE}wp-content/uploads/{d.year}/{d.month:02d}/Edicao_{numero}-{d.year}__publicada_em_{dia}_{mes_nome}_de_{d.year}.pdf"
                try:
                    if SESSAO.head(url, timeout=20).status_code == 200:
                        return {"numero": numero, "data": d.isoformat(), "url": url}
                except Exception:  # noqa: BLE001
                    pass
    return None


# ---------------------------------------------------------------- PDF

def texto_do_pdf(conteudo: bytes) -> str:
    import pdfplumber

    partes = []
    with pdfplumber.open(io.BytesIO(conteudo)) as pdf:
        for i, pag in enumerate(pdf.pages, 1):
            partes.append(pag.extract_text() or "")
            # o Diário é diagramado em duas colunas e o texto corrido mistura tabelas;
            # as tabelas vão de novo, uma linha por registro
            try:
                for tab in pag.extract_tables():
                    linhas = [" | ".join((c or "").replace("\n", " ").strip() for c in lin) for lin in tab if any(lin)]
                    if linhas:
                        partes.append(f"[TABELA pág. {i}]\n" + "\n".join(linhas) + "\n[FIM DA TABELA]")
            except Exception:  # noqa: BLE001
                pass
    return "\n".join(partes)


RE_ATO = re.compile(r"\*?\s*(PORTARIA|RESOLU[ÇC][ÃA]O|EDITAL|ATO|AVISO|DESPACHO|EXTRATO|INSTRU[ÇC][ÃA]O NORMATIVA)\s+N[º°o.]*\s*\d+[\d./\-]*\s*[-–/]?\s*[A-Z0-9/\-]*", re.I)


def separar_atos(texto: str) -> list[str]:
    pos = [m.start() for m in RE_ATO.finditer(texto)]
    if not pos:
        return [texto]
    atos = []
    for i, p in enumerate(pos):
        fim = pos[i + 1] if i + 1 < len(pos) else len(texto)
        atos.append(texto[p:fim].strip())
    # tabelas extraídas ao fim da página vão junto do ato anterior; ok para a leitura
    return atos


def termos_defensores(nomes: list[str]) -> list[str]:
    termos = []
    for n in nomes:
        t = [x for x in norm(n).split() if x not in ("de", "da", "do", "dos", "das", "e")]
        if len(t) >= 2:
            termos.append(f"{t[0]} {t[-1]}")
            termos.append(" ".join(t[:2]))
    return sorted(set(termos))


def relevante(ato: str, termos: list[str]) -> bool:
    n = norm(ato)
    return any(c in n for c in CIDADES) or any(t in n for t in termos)


# ---------------------------------------------------------------- leitura por regras

def norm1(s: str) -> str:
    """Minúsculas e sem acento, com o MESMO tamanho do texto original (para recortar trechos)."""
    out = []
    for c in s:
        b = unicodedata.normalize("NFD", c)[0].lower()
        out.append(b if len(b) == 1 else c)
    return "".join(out)


def ano4(a: str | None, padrao: int) -> int:
    if not a:
        return padrao
    return 2000 + int(a) if len(a) == 2 else int(a)


RE_DATA = re.compile(r"(\d{1,2})[º°o]?\s*/\s*(\d{1,2})\s*/\s*(\d{2,4})")
RE_DATA_EXT = re.compile(r"(\d{1,2})[º°o]?\s+de\s+(janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)(?:\s+de\s+(\d{4}))?")


def datas_no_texto(t: str, ano_padrao: int) -> list[dt.date]:
    """Todas as datas do trecho, na ordem: 01/10/26, 1º/10/2026, 1º de outubro de 2026."""
    achadas = []
    for m in RE_DATA.finditer(t):
        try:
            achadas.append((m.start(), dt.date(ano4(m.group(3), ano_padrao), int(m.group(2)), int(m.group(1)))))
        except ValueError:
            pass
    for m in RE_DATA_EXT.finditer(t):
        try:
            achadas.append((m.start(), dt.date(ano4(m.group(3), ano_padrao), MESES[m.group(2)], int(m.group(1)))))
        except ValueError:
            pass
    return [d for _, d in sorted(achadas)]


def periodo(t: str, ano_padrao: int):
    """(início, fim) de um inciso; fim None quando é só "a contar de" uma data."""
    ds = datas_no_texto(t, ano_padrao)
    if not ds:
        return None, None
    if len(ds) == 1:
        return (ds[0], None) if re.search(r"a contar de|a partir de", t) else (ds[0], ds[0])
    return min(ds), max(ds)


def defensores_no_texto(t: str, defensores: list[str]) -> list[str]:
    achados = []
    for d in defensores:
        toks = [x for x in norm(d).split() if x not in ("de", "da", "do", "dos", "das", "e")]
        if norm(d) in t or (len(toks) >= 2 and f"{toks[0]}" in t and toks[-1] in t and re.search(rf"\b{toks[0]}\b[\w\s]{{0,40}}\b{toks[-1]}\b", t)):
            achados.append(d)
    return achados


RE_DP_POLO = re.compile(r"(\d{1,2})\s*[ªaº°]?\s*defensoria publica do polo do baixo amazonas")
VERBOS = r"(designar|conceder|cessar|tornar sem efeito|revogar|dispensar|nomear|lotar|remover|alterar|retificar|interromper|suspender|autorizar)"


def incisos(texto: str) -> list[tuple[int, int]]:
    """Divide o ato em incisos/frases que começam com um verbo de comando (posições no texto)."""
    n = norm1(texto)
    pos = [m.start() for m in re.finditer(rf"(?:^|[\s;:.\-])(?:[ivxlc]+\s*[-–.]\s*)?{VERBOS}\b", n)]
    if not pos:
        return [(0, len(texto))]
    pos.append(len(texto))
    return [(pos[i], pos[i + 1]) for i in range(len(pos) - 1)]


def numero_do_ato(texto: str) -> str:
    m = RE_ATO.match(texto.strip()) or RE_ATO.search(texto[:200])
    return re.sub(r"\s+", " ", m.group(0)).strip(" -–*") if m else "Ato"


def categorias_de(n: str) -> list[str]:
    cats = []
    if "designar" in n or "acumula" in n or "substitu" in n:
        cats.append("designacao")
    if re.search(r"ferias|licenca|folga|afastamento|recesso", n):
        cats.append("afastamento")
    if "plantao" in n:
        cats.append("plantao")
    if re.search(r"remoc|remover|lotac|lotar", n):
        cats.append("lotacao")
    if "coordena" in n:
        cats.append("coordenacao")
    if re.search(r"servidor|estagiari|assessor|residente|analista|assistente", n) and "defensor" not in n:
        cats.append("servidores")
    return cats or ["geral"]


def ler_ato(texto: str, edicao: dict, idx: int, defensores: list[str]) -> list[dict]:
    """Interpreta um ato por regras. Devolve itens no formato do site."""
    ano = int((edicao.get("data") or str(dt.date.today()))[:4])
    numero = numero_do_ato(texto)
    n_todo = norm1(texto)
    itens = []
    # portarias com quadro: cada linha "Nome | Xª Defensoria Pública do Polo do Baixo Amazonas" vira uma designação
    if re.search(r"\bdesignar\b", n_todo) and not re.search(r"tornar sem efeito|cessar|revogar", n_todo):
        cab = n_todo.split("[tabela")[0]
        ini_t, fim_t = periodo(cab, ano)
        for k, linha in enumerate(l for l in texto.splitlines() if "|" in l):
            nl = norm1(linha)
            dps = sorted({int(x) for x in RE_DP_POLO.findall(nl) if 1 <= int(x) <= 12})
            pessoas = defensores_no_texto(nl, defensores)
            li, lf = periodo(nl, ano)
            li, lf = (li, lf) if li else (ini_t, fim_t)
            if len(dps) == 1 and len(pessoas) == 1 and li:
                dp, p_ = dps[0], pessoas[0]
                itens.append({"id": f"{edicao['numero']}-{idx}-t{k}", "numero": numero, "categorias": ["designacao"],
                              "resumo": f"Designa {p_} para a {dp}ª DP do Polo do Baixo Amazonas" + (f", de {li:%d/%m/%Y} a {lf:%d/%m/%Y}" if lf else f", a contar de {li:%d/%m/%Y}"),
                              "trecho": re.sub(r"\s+", " ", linha)[:320],
                              "sugestao": {"tipo": "designacao", "dp": f"dp{dp}", "defensor": p_, "inicio": li.isoformat(), "fim": lf.isoformat() if lf else ""},
                              "auto": not lf or lf >= li})
        if itens:
            return itens
    for j, (a, b) in enumerate(incisos(texto)):
        orig = texto[a:b].strip()
        n = norm1(orig)
        cita_polo = any(c in n for c in CIDADES)
        pessoas = defensores_no_texto(n, defensores)
        if not cita_polo and not pessoas:
            continue
        if any(p in n for p in OUTROS_POLOS) and "baixo amazonas" not in n:
            continue
        trecho = re.sub(r"\s+", " ", orig)[:320]
        item = {"id": f"{edicao['numero']}-{idx}-{j}", "numero": numero, "categorias": categorias_de(n),
                "resumo": "", "trecho": trecho, "sugestao": None, "auto": False}
        dps = sorted({int(x) for x in RE_DP_POLO.findall(n) if 1 <= int(x) <= 12})
        ini, fim = periodo(n, ano)
        eh_servidor = bool(re.search(r"\b(servidor|servidora|estagiari|assessor|residente|analista|assistente)\b", n)) and "defensor" not in n
        anula = bool(re.search(r"tornar sem efeito|cessar|revogar|dispensar|interromper|suspender", n))
        if re.search(r"\bdesignar\b", n) and dps and pessoas and ini and not eh_servidor and not anula:
            p = pessoas[0]
            fim_iso = fim.isoformat() if fim else ""
            for dp in dps:
                it = dict(item, id=f"{item['id']}-{dp}")
                it["sugestao"] = {"tipo": "designacao", "dp": f"dp{dp}", "defensor": p, "inicio": ini.isoformat(), "fim": fim_iso}
                it["resumo"] = f"Designa {p} para a {dp}ª DP do Polo do Baixo Amazonas" + (f", de {ini:%d/%m/%Y} a {fim:%d/%m/%Y}" if fim else f", a contar de {ini:%d/%m/%Y}")
                it["auto"] = len(pessoas) == 1 and (not fim or fim >= ini)
                itens.append(it)
            continue
        if re.search(r"ferias|licenca|folga", n) and pessoas and ini and fim and not eh_servidor and not anula and "conceder" in n:
            tipo = "ferias" if "ferias" in n else "licenca" if "licenca" in n else "folga"
            p = pessoas[0]
            item["sugestao"] = {"tipo": "afastamento", "defensor": p, "afastamento": tipo, "inicio": ini.isoformat(), "fim": fim.isoformat()}
            item["resumo"] = f"Concede {'férias' if tipo == 'ferias' else 'licença' if tipo == 'licenca' else 'folga'} a {p}, de {ini:%d/%m/%Y} a {fim:%d/%m/%Y}"
            item["auto"] = len(pessoas) == 1 and fim >= ini
            itens.append(item)
            continue
        # sem padrão claro: mostra para conferir
        verbo = re.search(VERBOS, n)
        item["resumo"] = (("Torna sem efeito ou encerra ato" if anula else (verbo.group(1).capitalize() if verbo else "Ato")) +
                          (f" envolvendo {', '.join(pessoas)}" if pessoas else "") +
                          (f" — {', '.join(str(d) + 'ª DP' for d in dps)}" if dps else "") +
                          (" (servidor/estagiário)" if eh_servidor else "")).strip()
        itens.append(item)
    # um item por inciso pode repetir o mesmo ato sem nada útil; enxuga
    vistos, saida = set(), []
    for it in itens:
        chave = (it["resumo"], it["trecho"][:80])
        if chave in vistos:
            continue
        vistos.add(chave)
        saida.append(it)
    return saida


# ---------------------------------------------------------------- principal

def main() -> int:
    estado = ler_json(ARQ_ESTADO, {"ultima": 0, "processadas": []})
    diario = ler_json(ARQ_DIARIO, [])
    defensores = ler_json(ARQ_NOMES, [])
    termos = termos_defensores(defensores)

    publicadas = edicoes_publicadas()
    if not publicadas:
        log("Nenhuma edição encontrada; nada a fazer.")
        return 0
    maior = max(publicadas)
    desde = int(os.environ.get("DESDE") or 0)
    inicio = desde or (estado.get("ultima") or (maior - 60)) + (0 if desde else 1)
    numeros = list(range(inicio, maior + 1))[:MAX_EDICOES]
    if not numeros:
        log(f"Sem edições novas (última lida: {estado.get('ultima')}).")
        return 0
    log(f"Edições a ler: {numeros[0]} a {numeros[-1]} ({len(numeros)})")

    por_numero = {e["edicao"]: e for e in diario}
    ultima_data = ""
    for n in numeros:
        info = publicadas.get(n) or procurar_edicao(n, ultima_data)
        if not info:
            log(f"Edição {n}: PDF não encontrado (pode não ter sido publicada).")
            continue
        ultima_data = info["data"] or ultima_data
        try:
            r = SESSAO.get(info["url"], timeout=90)
            r.raise_for_status()
            texto = texto_do_pdf(r.content)
        except Exception as e:  # noqa: BLE001
            log(f"Edição {n}: erro ao ler o PDF:", e)
            continue
        atos = [a for a in separar_atos(texto) if relevante(a, termos)]
        log(f"Edição {n} ({info['data']}): {len(atos)} atos pré-selecionados")
        convertidos = []
        for i, ato in enumerate(atos):
            convertidos += ler_ato(ato, info, i, defensores)
        if convertidos:
            por_numero[n] = {"edicao": n, "data": info["data"], "url": info["url"], "atos": convertidos}
            log(f"  {len(convertidos)} itens, {sum(1 for c in convertidos if c['auto'])} automáticos")
        elif n in por_numero and desde:
            del por_numero[n]
        estado["ultima"] = max(estado.get("ultima") or 0, n)

    gravar_json(ARQ_DIARIO, sorted(por_numero.values(), key=lambda e: e["edicao"]))
    estado["atualizado_em"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    gravar_json(ARQ_ESTADO, estado)
    log("Pronto.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
