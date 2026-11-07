"""
Alerta de Passagens - monitora passagens baratas saindo do Rio de Janeiro
e avisa no Telegram quando aparece um preço muito abaixo do normal.

Fonte dos preços: Travelpayouts / Aviasales Data API (grátis).
Roda no GitHub Actions a cada 15 minutos. O histórico fica em state/state.json
(guardado no cache do GitHub Actions entre as execuções).
"""

import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------------
# Configuração (pode ser alterada no arquivo .github/workflows/monitor.yml)
# ----------------------------------------------------------------------------
ORIGIN = os.getenv("ORIGIN", "RIO")                 # RIO = Galeão + Santos Dumont
CURRENCY = os.getenv("CURRENCY", "brl")
MARKET = os.getenv("MARKET", "br")
DISCOUNT = float(os.getenv("DISCOUNT", "0.40"))     # 0.40 = 40% abaixo do normal
MONTHS_AHEAD = int(os.getenv("MONTHS_AHEAD", "8"))  # quantos meses à frente buscar
MIN_TRIP_DAYS = int(os.getenv("MIN_TRIP_DAYS", "3"))
MAX_TRIP_DAYS = int(os.getenv("MAX_TRIP_DAYS", "30"))
HISTORY_DAYS = int(os.getenv("HISTORY_DAYS", "30"))
MIN_HISTORY_DAYS = int(os.getenv("MIN_HISTORY_DAYS", "3"))  # dias de histórico p/ confiar na média
SUMMARY_HOUR = int(os.getenv("SUMMARY_HOUR", "9"))  # resumo diário às 9h (horário de Brasília)
MAX_ALERTS_PER_RUN = int(os.getenv("MAX_ALERTS_PER_RUN", "5"))
INTERNATIONAL_ONLY = os.getenv("INTERNATIONAL_ONLY", "true").lower() == "true"  # só destinos fora do país
HOME_COUNTRY = os.getenv("HOME_COUNTRY", "BR")
TZ = ZoneInfo(os.getenv("TIMEZONE", "America/Sao_Paulo"))

TP_TOKEN = os.getenv("TRAVELPAYOUTS_TOKEN", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"

STATE_FILE = os.getenv("STATE_FILE", "state/state.json")
API = "https://api.travelpayouts.com"


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def log(msg):
    print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M}] {msg}", flush=True)


def http_get_json(url, headers=None, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": "alerta-passagens/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def brl(value):
    return "R$ " + f"{value:,.0f}".replace(",", ".")


def fmt_date(iso):
    return datetime.fromisoformat(iso).strftime("%d/%m/%Y")


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
def resolve_chat_id(state):
    """Descobre sozinho o chat do usuário (a partir do 'oi' enviado ao bot) e guarda no histórico."""
    global TG_CHAT_ID
    if TG_CHAT_ID:
        return
    if state.get("chat_id"):
        TG_CHAT_ID = state["chat_id"]
        return
    if not TG_TOKEN:
        return
    try:
        data = http_get_json(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates")
        for upd in reversed(data.get("result", [])):
            msg = upd.get("message") or upd.get("my_chat_member") or {}
            chat = msg.get("chat", {})
            if chat.get("id"):
                TG_CHAT_ID = str(chat["id"])
                state["chat_id"] = TG_CHAT_ID
                log("Chat do Telegram identificado automaticamente.")
                return
        log("Chat do Telegram ainda não identificado: mande um 'oi' para o seu bot.")
    except Exception as e:  # noqa: BLE001
        log(f"Não foi possível identificar o chat do Telegram: {e}")


def telegram(text):
    if not (TG_TOKEN and TG_CHAT_ID):
        log("Telegram não configurado; mensagem não enviada:\n" + text)
        return False
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()).get("ok", False)
    except Exception as e:  # noqa: BLE001
        log(f"Erro ao enviar Telegram: {e}")
        return False


# ----------------------------------------------------------------------------
# Travelpayouts
# ----------------------------------------------------------------------------
def months_to_search(n):
    today = date.today()
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def fetch_month(month, market):
    params = {
        "origin": ORIGIN,
        "departure_at": month,
        "one_way": "false",
        "sorting": "price",
        "currency": CURRENCY,
        "limit": 1000,
        "page": 1,
    }
    if market:
        params["market"] = market
    url = f"{API}/aviasales/v3/prices_for_dates?" + urllib.parse.urlencode(params)
    data = http_get_json(url, headers={"X-Access-Token": TP_TOKEN})
    if not data.get("success", True):
        raise RuntimeError(f"API respondeu com erro: {data.get('error')}")
    return data.get("data") or []


def fetch_offers():
    """Busca as passagens de ida e volta mais baratas saindo de ORIGIN nos próximos meses."""
    offers = []
    for month in months_to_search(MONTHS_AHEAD):
        try:
            rows = fetch_month(month, MARKET)
        except Exception as e:  # noqa: BLE001
            log(f"Falha ao buscar {month}: {e}")
            rows = []
        offers.extend(rows)
        time.sleep(0.4)
    if not offers and MARKET:
        log("Nenhum resultado com market definido; tentando sem market.")
        for month in months_to_search(MONTHS_AHEAD):
            try:
                offers.extend(fetch_month(month, None))
            except Exception as e:  # noqa: BLE001
                log(f"Falha ao buscar {month}: {e}")
            time.sleep(0.4)
    return normalize(offers)


def normalize(rows):
    out = []
    seen = set()
    for r in rows:
        try:
            dep = r["departure_at"]
            ret = r.get("return_at")
            if not ret:
                continue
            d1 = datetime.fromisoformat(dep).date()
            d2 = datetime.fromisoformat(ret).date()
            trip_days = (d2 - d1).days
            if trip_days < MIN_TRIP_DAYS or trip_days > MAX_TRIP_DAYS:
                continue
            if d1 < date.today():
                continue
            key = f"{r['destination']}|{d1}|{d2}"
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "key": key,
                "dest": r["destination"],
                "dest_airport": r.get("destination_airport") or r["destination"],
                "orig_airport": r.get("origin_airport") or ORIGIN,
                "price": float(r["price"]),
                "airline": r.get("airline") or "",
                "dep": dep,
                "ret": ret,
                "days": trip_days,
                "transfers": int(r.get("transfers") or 0),
                "return_transfers": int(r.get("return_transfers") or 0),
                "link": r.get("link") or "",
            })
        except (KeyError, ValueError, TypeError):
            continue
    return out


def refresh_reference_data(state):
    """Nomes de cidades e companhias (atualiza uma vez por semana)."""
    ref = state.get("ref", {})
    updated = ref.get("updated")
    fresh = updated and (date.today() - date.fromisoformat(updated)).days < 7
    has_country_codes = ref.get("version") == 2
    if fresh and has_country_codes:
        return
    try:
        cities = http_get_json(f"{API}/data/en/cities.json", timeout=90)
        countries = http_get_json(f"{API}/data/en/countries.json", timeout=90)
        airlines = http_get_json(f"{API}/data/en/airlines.json", timeout=90)
        country_names = {c["code"]: (c.get("name") or c["code"]) for c in countries}
        state["ref"] = {
            "version": 2,
            "updated": date.today().isoformat(),
            "cities": {
                c["code"]: [
                    c.get("name") or c["code"],
                    country_names.get(c.get("country_code"), ""),
                    c.get("country_code") or "",
                ]
                for c in cities
            },
            "airlines": {a["code"]: (a.get("name") or a["code"]) for a in airlines if a.get("code")},
        }
        log("Dados de referência (cidades/companhias) atualizados.")
    except Exception as e:  # noqa: BLE001
        log(f"Não foi possível atualizar nomes de cidades: {e}")


def city_name(state, code):
    c = state.get("ref", {}).get("cities", {}).get(code)
    if not c:
        return code
    name, country = c[0], c[1]
    return f"{name}, {country}" if country else name


def country_code(state, code):
    c = state.get("ref", {}).get("cities", {}).get(code)
    return c[2] if c and len(c) > 2 else ""


def filter_international(state, offers):
    """Mantém só destinos fora do país de origem (destinos de país desconhecido também ficam de fora)."""
    if not INTERNATIONAL_ONLY:
        return offers
    out = []
    for o in offers:
        cc = country_code(state, o["dest"])
        if cc and cc != HOME_COUNTRY:
            out.append(o)
    return out


def airline_name(state, code):
    return state.get("ref", {}).get("airlines", {}).get(code, code)


# ----------------------------------------------------------------------------
# Histórico e detecção de promoção
# ----------------------------------------------------------------------------
def update_history(state, offers):
    """Guarda o menor preço visto por destino em cada dia."""
    hist = state.setdefault("history", {})
    today = date.today().isoformat()
    for o in offers:
        day_mins = hist.setdefault(o["dest"], {})
        if today not in day_mins or o["price"] < day_mins[today]:
            day_mins[today] = o["price"]
    cutoff = (date.today() - timedelta(days=HISTORY_DAYS)).isoformat()
    for dest in list(hist):
        hist[dest] = {d: p for d, p in hist[dest].items() if d >= cutoff}
        if not hist[dest]:
            del hist[dest]


def baseline_for(state, dest, current_prices):
    """
    Preço "normal" do destino.
    - Com histórico suficiente: mediana dos mínimos diários dos dias ANTERIORES.
    - Sem histórico: mediana das outras datas disponíveis agora para o mesmo destino.
    """
    today = date.today().isoformat()
    past = [p for d, p in state.get("history", {}).get(dest, {}).items() if d != today]
    if len(past) >= MIN_HISTORY_DAYS:
        return statistics.median(past), "histórico"
    if len(current_prices) >= 6:
        return statistics.median(current_prices), "outras datas"
    return None, None


def find_deals(state, offers):
    by_dest = {}
    for o in offers:
        by_dest.setdefault(o["dest"], []).append(o)

    deals = []
    for dest, items in by_dest.items():
        prices = [o["price"] for o in items]
        base, source = baseline_for(state, dest, prices)
        if not base:
            continue
        for o in items:
            drop = 1 - o["price"] / base
            if drop >= DISCOUNT:
                deals.append({**o, "baseline": base, "drop": drop, "base_source": source})
    deals.sort(key=lambda d: d["drop"], reverse=True)
    return deals


def should_alert(state, deal):
    """Evita repetir o mesmo alerta a cada 15 min."""
    sent = state.setdefault("alerted", {})
    prev = sent.get(deal["key"])
    if not prev:
        return True
    cheaper_again = deal["price"] <= prev["price"] * 0.95
    old = (date.today() - date.fromisoformat(prev["date"])).days >= 3
    return cheaper_again or old


def mark_alerted(state, deal):
    state.setdefault("alerted", {})[deal["key"]] = {"price": deal["price"], "date": date.today().isoformat()}
    cutoff = (date.today() - timedelta(days=45)).isoformat()
    state["alerted"] = {k: v for k, v in state["alerted"].items() if v["date"] >= cutoff}


# ----------------------------------------------------------------------------
# Mensagens
# ----------------------------------------------------------------------------
def links(o):
    aviasales = ("https://www.aviasales.com" + o["link"]) if o["link"] else None
    d1 = datetime.fromisoformat(o["dep"]).date().isoformat()
    d2 = datetime.fromisoformat(o["ret"]).date().isoformat()
    q = f"Flights from {o['orig_airport']} to {o['dest_airport']} on {d1} through {d2}"
    google = "https://www.google.com/travel/flights?hl=pt-BR&curr=BRL&q=" + urllib.parse.quote(q)
    parts = []
    if aviasales:
        parts.append(f'<a href="{aviasales}">Ver vendedores (Aviasales)</a>')
    parts.append(f'<a href="{google}">Conferir no Google Flights</a>')
    return " | ".join(parts)


def stops(n):
    return "direto" if n == 0 else f"{n} parada" + ("s" if n > 1 else "")


def deal_message(state, d):
    airline = airline_name(state, d["airline"]) if d["airline"] else "várias"
    return (
        f"🔥 <b>PROMOÇÃO: {city_name(state, d['dest'])}</b>\n"
        f"💰 <b>{brl(d['price'])}</b> ida e volta "
        f"(normal ~{brl(d['baseline'])}, <b>{d['drop']:.0%} mais barato</b>)\n"
        f"🛫 Ida: {fmt_date(d['dep'])} — {d['orig_airport']} → {d['dest_airport']} ({stops(d['transfers'])})\n"
        f"🛬 Volta: {fmt_date(d['ret'])} ({stops(d['return_transfers'])}) — {d['days']} dias\n"
        f"✈️ Companhia: {airline}\n"
        f"{links(d)}\n"
        f"<i>Preço encontrado recentemente; confirme no site antes de comprar.</i>"
    )


def summary_message(state, offers, title="☀️ <b>Resumo do dia</b>"):
    best = {}
    for o in offers:
        if o["dest"] not in best or o["price"] < best[o["dest"]]["price"]:
            best[o["dest"]] = o
    top = sorted(best.values(), key=lambda o: o["price"])[:10]
    kind = "internacionais " if INTERNATIONAL_ONLY else ""
    lines = [f"{title} — destinos {kind}mais baratos saindo do Rio:"]
    for o in top:
        lines.append(
            f"• {city_name(state, o['dest'])}: <b>{brl(o['price'])}</b> "
            f"({fmt_date(o['dep'])} a {fmt_date(o['ret'])})"
        )
    if not top:
        lines.append("Nenhuma oferta encontrada agora.")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Principal
# ----------------------------------------------------------------------------
def main():
    if not TP_TOKEN:
        log("ERRO: falta o segredo TRAVELPAYOUTS_TOKEN.")
        sys.exit(1)

    state = load_state()
    resolve_chat_id(state)
    refresh_reference_data(state)

    all_offers = fetch_offers()
    offers = filter_international(state, all_offers)
    log(f"{len(all_offers)} ofertas de ida e volta encontradas; "
        f"{len(offers)} para {len({o['dest'] for o in offers})} destinos "
        f"{'internacionais' if INTERNATIONAL_ONLY else 'no total'}.")

    if not all_offers:
        fails = state.get("consecutive_failures", 0) + 1
        state["consecutive_failures"] = fails
        if fails == 8:  # ~2 horas sem dados
            telegram("⚠️ O Alerta de Passagens está há cerca de 2 horas sem conseguir buscar preços. "
                     "Pode ser instabilidade na API ou o token expirou.")
        save_state(state)
        return
    state["consecutive_failures"] = 0

    if TEST_MODE:
        ok = telegram(summary_message(state, offers, title="✅ <b>Teste OK!</b> O sistema está funcionando"))
        log("Mensagem de teste enviada." if ok else "Falha ao enviar mensagem de teste.")

    deals = find_deals(state, offers)
    log(f"{len(deals)} ofertas acima de {DISCOUNT:.0%} de desconto.")

    sent = 0
    for d in deals:
        if sent >= MAX_ALERTS_PER_RUN:
            break
        if should_alert(state, d):
            if telegram(deal_message(state, d)):
                mark_alerted(state, d)
                sent += 1
    log(f"{sent} alertas enviados.")

    update_history(state, offers)

    now = datetime.now(TZ)
    today = now.date().isoformat()
    if now.hour >= SUMMARY_HOUR and state.get("last_summary") != today:
        if telegram(summary_message(state, offers)):
            state["last_summary"] = today

    state["last_run"] = now.isoformat()
    save_state(state)


if __name__ == "__main__":
    main()
