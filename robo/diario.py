"""Robô do Diário Oficial da DPE-AM para o painel do Polo do Baixo Amazonas.

Roda no GitHub Actions todo dia de madrugada:
1. descobre as edições novas em diario.defensoria.am.def.br;
2. baixa cada PDF e extrai o texto (inclusive tabelas);
3. separa os atos (portarias, resoluções...) e fica só com os que citam o polo,
   as comarcas (Parintins, Nhamundá, Barreirinha) ou um defensor do polo;
4. pede à Claude um resumo estruturado de cada ato;
5. confere tudo por regras fixas e marca como "auto" só o que é claro
   (DP do polo, defensor cadastrado, período válido);
6. grava diario.json, que o site lê.

Variáveis de ambiente:
  ANTHROPIC_API_KEY  chave da API (segredo do repositório)
  DESDE              opcional: reler a partir desta edição (ex.: 2700)
  MAX_EDICOES        opcional: limite de edições por execução (padrão 40)
  MODELO             opcional: modelo da Claude (padrão claude-haiku-5-5)
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
MODELO = os.environ.get("MODELO", "claude-haiku-5-5")
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


# ---------------------------------------------------------------- Claude

PROMPT = """Você lê atos do Diário Oficial da Defensoria Pública do Estado do Amazonas (DPE-AM) para o painel interno do POLO DO BAIXO AMAZONAS.

O polo tem sede em Parintins e duas cidades satélites, Nhamundá e Barreirinha (com a Unidade Descentralizada do Interior, UDI). Tem 12 defensorias: 1ª a 6ª em Parintins, 7ª a 9ª em Nhamundá, 10ª a 12ª em Barreirinha. Defensores cadastrados no polo: {defensores}.

Regras:
- Inclua só atos que afetam o Polo do Baixo Amazonas: suas defensorias, defensores, servidores, comarcas (Parintins, Nhamundá, Barreirinha), plantão ou coordenação. Ignore atos só de outros polos (Médio Amazonas, Médio Solimões, Médio Madeira, Alto Solimões, Juruá, Purus, Rio Negro-Solimões etc.), mesmo que citem alguém conhecido.
- Não junte incisos de portarias vizinhas. Cada ato é um número de portaria/resolução.
- Leia o ato inteiro e separe DEFENSOR(A) PÚBLICO(A) de SERVIDOR(A), estagiário ou residente. Servidor designado para apoio (fila, atendimento, secretaria) vai na categoria "servidores" e NÃO gera sugestão de designação.
- Linhas entre [TABELA ...] e [FIM DA TABELA] são tabelas: cada linha é um registro, colunas separadas por " | ". Respeite origem → destino. Se a tabela e o texto corrido divergirem, confie na tabela.
- Datas no formato AAAA-MM-DD. "a contar de" sem fim = fim null. Período "de 01/10/26 até 31/12/2026" = início e fim.
- "dp" é o número da defensoria do POLO DO BAIXO AMAZONAS (1 a 12). Se a DP for de outro polo, não sugira nada.
- "trecho" é a frase exata do ato que menciona o polo, a comarca ou a pessoa (até 300 caracteres, copiada do texto).
- Categorias permitidas: designacao, substituicao, afastamento, plantao, lotacao, coordenacao, servidores, geral.

Para cada ato relevante, sugestões (lista, pode ser vazia):
- {{"tipo":"designacao","dp":N,"pessoa":"nome completo","eh_defensor":true,"inicio":"AAAA-MM-DD","fim":"AAAA-MM-DD ou null"}} para designação, acumulação, substituição ou titularidade numa DP do polo;
- {{"tipo":"afastamento","pessoa":"nome completo","eh_defensor":true,"afastamento":"ferias|licenca|folga|viagem|outro","inicio":"AAAA-MM-DD","fim":"AAAA-MM-DD"}} para férias, licença, folga, viagem a serviço de defensor do polo;
- {{"tipo":"cessacao","dp":N,"pessoa":"...","inicio":"AAAA-MM-DD"}} quando o ato torna sem efeito ou cessa uma designação.

Responda SÓ com JSON, sem comentários:
{{"atos":[{{"numero":"Portaria Nº .../2026-GSPG","categorias":[...],"resumo":"uma frase","trecho":"...","sugestoes":[...]}}]}}
Se nenhum ato for relevante, responda {{"atos":[]}}.

Edição {edicao}, publicada em {data}. Atos pré-selecionados:

{texto}"""


def perguntar_claude(cliente, edicao: dict, atos: list[str], defensores: list[str]) -> list[dict]:
    texto = "\n\n=====\n\n".join(atos)
    if len(texto) > 60000:
        texto = texto[:60000]
    prompt = PROMPT.format(defensores=", ".join(defensores) or "(nenhum)", edicao=edicao["numero"],
                           data=edicao["data"] or "?", texto=texto)
    for tentativa in range(3):
        try:
            r = cliente.messages.create(model=MODELO, max_tokens=4000, messages=[{"role": "user", "content": prompt}])
            bruto = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
            ini, fim = bruto.find("{"), bruto.rfind("}")
            return json.loads(bruto[ini:fim + 1]).get("atos", [])
        except Exception as e:  # noqa: BLE001
            log(f"  Claude falhou (tentativa {tentativa + 1}):", e)
            time.sleep(5 * (tentativa + 1))
    return []


# ---------------------------------------------------------------- validação

def data_ok(s) -> bool:
    try:
        d = dt.date.fromisoformat(str(s))
        return 2020 <= d.year <= 2035
    except Exception:  # noqa: BLE001
        return False


def achar_defensor(nome: str, defensores: list[str]) -> str | None:
    t = [x for x in norm(nome).split() if x not in ("de", "da", "do", "dos", "das", "e")]
    if not t:
        return None
    for d in defensores:
        dt_ = norm(d).split()
        if t[0] == dt_[0] and all(x in dt_ for x in t[1:]):
            return d
        if len(t) >= 2 and t[0] == dt_[0] and t[-1] == dt_[-1]:
            return d
    return None


def converter(ato: dict, edicao: dict, idx: int, defensores: list[str]) -> list[dict]:
    """Transforma a resposta da Claude no formato do site, marcando 'auto' só quando é claro."""
    numero = str(ato.get("numero") or "").strip()
    trecho = str(ato.get("trecho") or "").strip()[:400]
    resumo = str(ato.get("resumo") or "").strip()
    cats = [c for c in (norm(x) for x in ato.get("categorias") or []) if c in CATEGORIAS] or ["geral"]
    citacao = norm(numero + " " + trecho)
    # a ligação com o polo tem de estar no texto citado, não só no resumo
    if not (any(c in citacao for c in CIDADES) or achar_defensor(trecho, defensores) or any(norm(d) in citacao for d in defensores)):
        if "geral" not in cats:
            return []
    if any(p in citacao for p in OUTROS_POLOS) and "baixo amazonas" not in citacao:
        return []
    base = {"numero": numero, "categorias": cats, "resumo": resumo, "trecho": trecho}
    sugestoes = ato.get("sugestoes") or []
    saida = []
    for j, s in enumerate(sugestoes or [None]):
        item = dict(base, id=f"{edicao['numero']}-{idx}-{j}", sugestao=None, auto=False)
        if s:
            tipo = s.get("tipo")
            pessoa = achar_defensor(s.get("pessoa") or "", defensores)
            nome = pessoa or str(s.get("pessoa") or "").strip()
            dp = s.get("dp")
            dp_ok = isinstance(dp, int) and 1 <= dp <= 12 or (isinstance(dp, str) and dp.isdigit() and 1 <= int(dp) <= 12)
            if tipo == "designacao" and dp_ok and s.get("eh_defensor") is not False and data_ok(s.get("inicio")):
                fim = s.get("fim") if data_ok(s.get("fim")) else ""
                item["sugestao"] = {"tipo": "designacao", "dp": f"dp{int(dp)}", "defensor": nome, "inicio": s["inicio"], "fim": fim}
                item["auto"] = bool(pessoa) and "baixo amazonas" in citacao and (not fim or fim >= s["inicio"])
            elif tipo == "afastamento" and s.get("eh_defensor") is not False and data_ok(s.get("inicio")) and data_ok(s.get("fim")):
                af = s.get("afastamento") if s.get("afastamento") in TIPOS_AF else "outro"
                item["sugestao"] = {"tipo": "afastamento", "defensor": nome, "afastamento": af, "inicio": s["inicio"], "fim": s["fim"]}
                item["auto"] = bool(pessoa) and s["fim"] >= s["inicio"]
            elif tipo == "cessacao":
                item["resumo"] = resumo or f"Cessa designação de {nome}"
        if j > 0 and not item["sugestao"]:
            continue
        saida.append(item)
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

    import anthropic
    cliente = anthropic.Anthropic()

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
        if atos:
            for i, ato in enumerate(perguntar_claude(cliente, info, atos, defensores)):
                convertidos += converter(ato, info, i, defensores)
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
