"""
Riepilogo settimanale via email dei tuoi conti Hyperliquid (RAPS, BTC 1349, ...). Solo lettura: non invia ordini.

Per ogni conto: valore attuale, P&L ultimi 7 e 30 giorni e da sempre (dall'endpoint "portfolio" di Hyperliquid,
che esclude depositi e prelievi), chiusure e P&L netto dai fill degli ultimi 7 giorni, posizioni aperte con
P&L non realizzato e distanza dalla liquidazione. In fondo: totale e avvisi.

Configurazione (.env in questa cartella):
    REPORT_ACCOUNTS=RAPS=0xIndirizzoContoRAPS,BTC 1349=0xIndirizzoSubAccountBTC
    HL_ENV=mainnet
    SMTP_USER / SMTP_PASSWORD / EMAIL_TO / SMTP_HOST / SMTP_PORT   (come negli altri sistemi)
    LIQ_WARN_PCT=15       avviso se la liquidazione e' a meno del 15% dal prezzo di mercato

Uso:
    python weekly_report.py            # calcola e manda la email
    python weekly_report.py --print    # stampa a schermo senza mandare nulla
"""
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from dotenv import load_dotenv

load_dotenv()

MAINNET = "https://api.hyperliquid.xyz"
TESTNET = "https://api.hyperliquid-testnet.xyz"


def get_info():
    from hyperliquid.info import Info
    url = TESTNET if os.environ.get("HL_ENV", "mainnet").lower() == "testnet" else MAINNET
    return Info(url, skip_ws=True)


def parse_accounts() -> list:
    raw = os.environ.get("REPORT_ACCOUNTS", "").strip()
    out = []
    for part in raw.split(","):
        if "=" in part:
            name, addr = part.rsplit("=", 1)
            out.append((name.strip(), addr.strip()))
    return out


def money(x: float) -> str:
    """+$12.30 / -$4.00 (il segno davanti al simbolo)."""
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def px(x: float) -> str:
    return f"{x:,.2f}" if x >= 1 else f"{x:.6g}"


def pnl_from_bucket(portfolio: dict, *names) -> float:
    """P&L di un periodo: ultimo - primo valore di pnlHistory (cumulato, esclude depositi/prelievi)."""
    for n in names:
        b = portfolio.get(n)
        if b and b.get("pnlHistory"):
            h = b["pnlHistory"]
            return float(h[-1][1]) - float(h[0][1])
    return float("nan")


def get_portfolio(info, addr) -> dict:
    try:
        raw = info.post("/info", {"type": "portfolio", "user": addr})
        return {name: data for name, data in raw}
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] portfolio non disponibile per {addr}: {e}", file=sys.stderr)
        return {}


def account_section(info, name: str, addr: str, warn_pct: float) -> tuple:
    """Ritorna (testo, valore_totale, pnl7, avvisi)."""
    state = info.user_state(addr)
    perp_value = float(state.get("marginSummary", {}).get("accountValue", 0.0))
    spot_usdc = 0.0
    try:
        for b in info.spot_user_state(addr).get("balances", []):
            if b["coin"] == "USDC":
                spot_usdc = float(b["total"])
    except Exception:  # noqa: BLE001
        pass
    total = perp_value + spot_usdc

    pf = get_portfolio(info, addr)
    p7 = pnl_from_bucket(pf, "perpWeek", "week")
    p30 = pnl_from_bucket(pf, "perpMonth", "month")
    pall = pnl_from_bucket(pf, "perpAllTime", "allTime")

    now = datetime.now(timezone.utc)
    start = int((now - timedelta(days=7)).timestamp() * 1000)
    fills = info.user_fills_by_time(addr, start, int(now.timestamp() * 1000))
    fills = [f for f in fills if not f["coin"].startswith("@") and "/" not in f["coin"]]
    closes = [f for f in fills if float(f.get("closedPnl", 0)) != 0]
    fees = sum(float(f.get("fee", 0)) for f in fills)
    realized = sum(float(f["closedPnl"]) for f in closes)
    wins = sum(1 for f in closes if float(f["closedPnl"]) > 0)

    lines = [f"=== {name} ===", f"Valore conto: ${total:,.2f}  (perp ${perp_value:,.2f} + spot USDC ${spot_usdc:,.2f})"]
    def pct(p):
        base = total - p
        return f" ({p / base:+.1%})" if base > 0 and p == p else ""
    for label, p in [("P&L 7 giorni", p7), ("P&L 30 giorni", p30), ("P&L da sempre", pall)]:
        lines.append(f"{label}: " + ("n/d" if p != p else f"{money(p)}{pct(p)}"))
    lines.append(f"Fill ultimi 7 gg: {len(fills)} | chiusure: {len(closes)} (in utile {wins}) | "
                 f"P&L realizzato {money(realized)} | fee ${fees:,.2f} | netto {money(realized - fees)}")

    warns = []
    pos = [p["position"] for p in state.get("assetPositions", [])]
    if pos:
        lines.append("Posizioni aperte:")
        mids = {}
        try:
            mids = info.all_mids()
        except Exception:  # noqa: BLE001
            pass
        for p in sorted(pos, key=lambda x: -abs(float(x["positionValue"]))):
            szi = float(p["szi"])
            liq = p.get("liquidationPx")
            liq_txt = ""
            if liq and p["coin"] in mids:
                mid, liq = float(mids[p["coin"]]), float(liq)
                dist = abs(mid - liq) / mid
                liq_txt = f" | liq {px(liq)} ({dist:.0%} dal prezzo)"
                if dist * 100 < warn_pct:
                    warns.append(f"{name}: {p['coin']} a {dist:.0%} dalla liquidazione")
            lines.append(f"  {p['coin']:<10} {'LONG' if szi > 0 else 'SHORT':<5} notional ${abs(float(p['positionValue'])):>10,.2f} "
                         f"| entry {px(float(p['entryPx']))} | P&L non realizz. {money(float(p['unrealizedPnl']))}{liq_txt}")
    else:
        lines.append("Posizioni aperte: nessuna")
    return "\n".join(lines), total, p7, warns


def build_report() -> tuple:
    accounts = parse_accounts()
    if not accounts:
        raise SystemExit("REPORT_ACCOUNTS non impostato nel .env (formato: NOME=0xindirizzo,NOME2=0xindirizzo2)")
    warn_pct = float(os.environ.get("LIQ_WARN_PCT", "15"))
    info = get_info()
    sections, total, p7_tot, warns = [], 0.0, 0.0, []
    for name, addr in accounts:
        try:
            txt, val, p7, w = account_section(info, name, addr, warn_pct)
            sections.append(txt)
            total += val
            p7_tot += p7 if p7 == p7 else 0.0
            warns += w
        except Exception as e:  # noqa: BLE001
            sections.append(f"=== {name} ===\n[ERRORE] lettura fallita: {e}")
            warns.append(f"{name}: lettura del conto fallita ({e})")
    head = f"RIEPILOGO SETTIMANALE - {datetime.now(timezone.utc):%Y-%m-%d} (UTC)\n"
    foot = f"=== TOTALE ===\nValore complessivo: ${total:,.2f} | P&L ultimi 7 giorni: {money(p7_tot)}"
    if warns:
        foot += "\n\nAVVISI:\n" + "\n".join(f"  - {w}" for w in warns)
    body = head + "\n\n".join(sections) + "\n\n" + foot + "\n\nNota: il P&L dall'endpoint 'portfolio' esclude depositi e prelievi.\n"
    subject = f"[Riepilogo settimanale] ${total:,.0f} | 7gg {money(p7_tot)}" + (" | ATTENZIONE" if warns else "")
    return subject, body


def send(subject: str, body: str) -> None:
    user, pwd = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
    to = os.environ.get("EMAIL_TO", "") or user
    if not (user and pwd and to):
        raise SystemExit("SMTP_USER / SMTP_PASSWORD / EMAIL_TO mancanti nel .env")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    with smtplib.SMTP(os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "587")), timeout=30) as s:
        s.starttls()
        s.login(user, pwd)
        s.send_message(msg)
    print(f"Email inviata a {to}")


if __name__ == "__main__":
    subject, body = build_report()
    if "--print" in sys.argv:
        print(subject + "\n\n" + body)
    else:
        print(body)
        send(subject, body)
