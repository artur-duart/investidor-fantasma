"""Investidor fantasma: aporta todo dia 30 seguindo a estrategia e gera data/estado.json."""
import calendar
import datetime as dt
import json
import math
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

BASE = Path(__file__).parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
ATIVOS = {a["ticker"]: a for a in CFG["ativos"]}
DATA = BASE / "data"
OPS = DATA / "operacoes.json"
TZ = ZoneInfo("America/Sao_Paulo")
UM_DIA = dt.timedelta(days=1)


def simbolo(a):
    return {"Brasil": a["ticker"] + ".SA", "Bitcoin": "BTC-USD"}.get(a["classe"], a["ticker"])


def em_dolar(t):
    return ATIVOS[t]["classe"] != "Brasil"


def baixar(simb, inicio):
    df = yf.Ticker(simb).history(start=inicio, auto_adjust=False)
    if df.empty:
        raise RuntimeError(f"Sem dados para {simb}")
    df.index = df.index.tz_localize(None).normalize()
    return df


def baixar_cdi(inicio, fim):
    """Indice acumulado do CDI (serie 12 do SGS/Banco Central, em % ao dia)."""
    url = ("https://api.bcb.gov.br/dados/serie/bcdata.sgs.12/dados?formato=json"
           f"&dataInicial={inicio:%d/%m/%Y}&dataFinal={fim:%d/%m/%Y}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        dados = json.load(r)
    s = pd.Series({pd.Timestamp(dt.datetime.strptime(x["data"], "%d/%m/%Y")): float(x["valor"]) for x in dados})
    return (1 + s.sort_index() / 100).cumprod()


def ultimo(serie, data):
    s = serie[serie.index <= pd.Timestamp(data)].dropna()
    return float(s.iloc[-1]) if len(s) else None


class Mercado:
    def __init__(self):
        ini = dt.date.fromisoformat(CFG["inicio"]) - dt.timedelta(days=10)
        self.hist = {t: baixar(simbolo(a), ini.isoformat()) for t, a in ATIVOS.items()}
        self.fx = baixar("BRL=X", ini.isoformat())["Close"]
        self.bench = {}
        for b in CFG.get("benchmarks", []):
            try:
                self.bench[b["nome"]] = baixar(b["simbolo"], ini.isoformat())["Close"]
            except Exception as e:
                print(f"Aviso: benchmark {b['nome']} indisponivel ({e})")
        self.cdi = None
        if CFG.get("benchmark_cdi", True):
            try:
                self.cdi = baixar_cdi(ini, dt.date.today())
            except Exception as e:
                print(f"Aviso: CDI indisponivel ({e})")

    def cambio(self, data):
        return ultimo(self.fx, data)

    def preco_local(self, t, data):
        return ultimo(self.hist[t]["Close"], data)

    def preco(self, t, data):
        p = self.preco_local(t, data)
        return p * self.cambio(data) if em_dolar(t) else p

    def serie(self, t, coluna):
        df = self.hist[t]
        return df[coluna][df[coluna] > 0] if coluna in df else pd.Series(dtype=float)


def compras(dados):
    return [c | {"data": a["data"]} for a in dados["aportes"] for c in a["compras"]]


def quantidade(t, data, comps, mkt):
    q = 0.0
    splits = mkt.serie(t, "Stock Splits")
    for c in comps:
        if c["ticker"] != t or c["data"] > data.isoformat():
            continue
        fator = 1.0
        for d, r in splits.items():
            if dt.date.fromisoformat(c["data"]) < d.date() <= data:
                fator *= float(r)
        q += c["quantidade"] * fator
    return q


def proventos(comps, mkt, ate):
    lista = []
    for t in ATIVOS:
        for d, v in mkt.serie(t, "Dividends").items():
            d = d.date()
            if d > ate:
                continue
            q = quantidade(t, d - UM_DIA, comps, mkt)
            if q <= 0:
                continue
            valor = q * float(v)
            if em_dolar(t):
                valor *= (1 - CFG["ir_dividendos_eua"]) * mkt.cambio(d)
            lista.append({"data": d.isoformat(), "ticker": t, "valor": round(valor, 2)})
    return sorted(lista, key=lambda p: p["data"])


def caixa(dados, provs, data):
    d = data.isoformat()
    entradas = sum(a["valor"] for a in dados["aportes"] if a["data"] <= d)
    entradas += sum(p["valor"] for p in provs if p["data"] <= d)
    gastos = sum(c["valor_gasto"] for a in dados["aportes"] if a["data"] <= d for c in a["compras"])
    return round(entradas - gastos, 2)


def data_aporte(n):
    ini = dt.date.fromisoformat(CFG["inicio"])
    y, m = divmod(ini.month - 1 + n, 12)
    y, m = y + ini.year, m + 1
    return dt.date(y, m, min(CFG["dia_aporte"], calendar.monthrange(y, m)[1]))


def valor_aporte(n):
    return round(CFG["aporte_inicial"] * (1 + CFG["reajuste_anual"]) ** (n // 12), 2)


def posicoes(comps, mkt, data):
    return {t: quantidade(t, data, comps, mkt) for t in ATIVOS}


def valores(qtds, mkt, data):
    return {t: q * mkt.preco(t, data) if q > 0 else 0.0 for t, q in qtds.items()}


def metas():
    n = {}
    for a in CFG["ativos"]:
        n[a["classe"]] = n.get(a["classe"], 0) + 1
    return {t: CFG["classes"][a["classe"]] / n[a["classe"]] for t, a in ATIVOS.items()}


def escolher(vals):
    total = sum(vals.values())
    fatia = lambda v: v / total if total > 0 else 0.0
    por_classe = lambda c: sum(v for t, v in vals.items() if ATIVOS[t]["classe"] == c)
    classe = max(CFG["classes"], key=lambda c: CFG["classes"][c] - fatia(por_classe(c)))
    m, ordem = metas(), list(ATIVOS)
    cand = [t for t in ATIVOS if ATIVOS[t]["classe"] == classe and ATIVOS[t].get("status", "Ativa") == "Ativa"]
    cand.sort(key=lambda t: (-round(m[t] - fatia(vals[t]), 9), -ATIVOS[t]["nota"], ordem.index(t)))
    return classe, cand[: CFG["ativos_por_aporte"]]


def benchmarks(dados, mkt, data):
    """Quanto valeriam os mesmos aportes, nas mesmas datas, em cada alternativa."""
    aps = [a for a in dados["aportes"] if a["data"] <= data.isoformat()]
    res = {}
    if mkt.cdi is not None and len(mkt.cdi):
        fim = ultimo(mkt.cdi, data)
        res["CDI (bruto)"] = sum(a["valor"] * fim / ultimo(mkt.cdi, dt.date.fromisoformat(a["data"])) for a in aps)
    for nome, serie in mkt.bench.items():
        p = ultimo(serie, data)
        res[nome] = sum(a["valor"] / ultimo(serie, dt.date.fromisoformat(a["data"])) * p for a in aps)
    return {k: round(v, 2) for k, v in res.items()}


def executar(n, d, dados, mkt):
    comps = compras(dados)
    provs = proventos(comps, mkt, d)
    classe, escolhidos = escolher(valores(posicoes(comps, mkt, d), mkt, d))
    valor = valor_aporte(n)
    parte = (caixa(dados, provs, d) + valor) / len(escolhidos)
    regs = []
    for t in escolhidos:
        preco = mkt.preco(t, d)
        if ATIVOS[t]["classe"] == "Brasil":
            q = math.floor(parte / preco)
            gasto = q * preco
        else:
            liquido = parte * (1 - CFG["custo_cambio"]) if ATIVOS[t]["classe"] == "Exterior" else parte
            q = round(liquido / preco, 6)
            gasto = parte
        if q > 0:
            regs.append({"ticker": t, "quantidade": q, "preco": round(preco, 4), "valor_gasto": round(gasto, 2)})
    return {"numero": n + 1, "data": d.isoformat(), "valor": valor, "classe": classe, "compras": regs}


def gerar_estado(dados, mkt, agora):
    hoje = agora.date()
    comps = compras(dados)
    provs = proventos(comps, mkt, hoje)
    qt = posicoes(comps, mkt, hoje)
    vals = valores(qt, mkt, hoje)
    cx = caixa(dados, provs, hoje)
    carteira = sum(vals.values())
    patrimonio = carteira + cx
    investido = sum(a["valor"] for a in dados["aportes"])
    custo = {t: sum(c["valor_gasto"] for c in comps if c["ticker"] == t) for t in ATIVOS}
    prov_t = {t: sum(p["valor"] for p in provs if p["ticker"] == t) for t in ATIVOS}
    m = metas()
    n_prox = len(dados["aportes"])
    classe, escolhidos = escolher(vals)

    ativos = []
    for t, a in ATIVOS.items():
        q, v, c = qt[t], vals[t], custo[t]
        pm = c / q if q > 0 else None
        pa = mkt.preco(t, hoje)
        ativos.append({
            "ticker": t, "classe": a["classe"], "setor": a["setor"], "nota": a["nota"],
            "status": a.get("status", "Ativa"), "quantidade": q, "custo": c,
            "preco_medio": pm, "preco_atual": pa,
            "preco_local": mkt.preco_local(t, hoje) if em_dolar(t) else None,
            "variacao": pa / pm - 1 if pm else None,
            "rentabilidade": (v + prov_t[t] - c) / c if c > 0 else None,
            "saldo": v, "proventos": prov_t[t],
            "pct_carteira": v / carteira if carteira > 0 else 0.0, "pct_ideal": m[t],
            "comprar": t in escolhidos,
        })

    evolucao, mes = [], dt.date.fromisoformat(CFG["inicio"]).replace(day=1)
    while mes <= hoje:
        fim = min(mes.replace(day=calendar.monthrange(mes.year, mes.month)[1]), hoje)
        pat = sum(valores(posicoes(comps, mkt, fim), mkt, fim).values()) + caixa(dados, provs, fim)
        apl = sum(a["valor"] for a in dados["aportes"] if a["data"] <= fim.isoformat())
        evolucao.append({"mes": fim.strftime("%m/%y"), "aplicado": round(apl, 2), "patrimonio": round(pat, 2),
                         "bench": benchmarks(dados, mkt, fim)})
        mes = (mes.replace(day=28) + dt.timedelta(days=4)).replace(day=1)

    ganho = carteira - sum(custo.values())
    dividendos = sum(p["valor"] for p in provs)
    corte = (hoje - dt.timedelta(days=365)).isoformat()
    return {
        "atualizado_em": agora.strftime("%d/%m/%Y %H:%M"),
        "inicio": CFG["inicio"],
        "resumo": {
            "patrimonio": patrimonio, "investido": investido, "caixa": cx,
            "lucro": patrimonio - investido, "ganho_capital": ganho, "dividendos": dividendos,
            "proventos_12m": sum(p["valor"] for p in provs if p["data"] > corte),
            "rentabilidade": (patrimonio - investido) / investido if investido else 0.0,
        },
        "benchmarks": benchmarks(dados, mkt, hoje),
        "metas_classe": CFG["classes"],
        "alocacao": {c: sum(vals[t] for t in ATIVOS if ATIVOS[t]["classe"] == c) / carteira if carteira else 0.0
                     for c in CFG["classes"]},
        "proximo_aporte": {"data": data_aporte(n_prox).isoformat(), "valor": valor_aporte(n_prox),
                           "caixa": cx, "classe": classe, "ativos": escolhidos},
        "evolucao": evolucao,
        "ativos": ativos,
        "aportes": list(reversed(dados["aportes"])),
        "proventos": provs,
    }


def main():
    agora = dt.datetime.now(TZ)
    DATA.mkdir(exist_ok=True)
    dados = json.loads(OPS.read_text(encoding="utf-8")) if OPS.exists() else {"aportes": []}
    mkt = Mercado()
    while True:
        n = len(dados["aportes"])
        d = data_aporte(n)
        if d > agora.date() or (d == agora.date() and agora.hour < 18):
            break
        reg = executar(n, d, dados, mkt)
        dados["aportes"].append(reg)
        print(f"Aporte {reg['numero']} em {reg['data']}: {[c['ticker'] for c in reg['compras']]}")
    OPS.write_text(json.dumps(dados, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    estado = gerar_estado(dados, mkt, agora)
    (DATA / "estado.json").write_text(json.dumps(estado, ensure_ascii=False, indent=1, default=float), encoding="utf-8")


if __name__ == "__main__":
    main()
