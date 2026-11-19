import logging
import sqlite3
import datetime
import pytz
import yfinance as yf
import pandas as pd
import pandas_ta as ta
from tefas import Crawler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# --- 1. YAPILANDIRMA (SADECE BU İKİ SATIRI DÜZENLEYİN) ---
TELEGRAM_TOKEN = "BURAYA_BOTFATHER_TOKEN_YAZIN"
ALLOWED_CHAT_ID = 123456789  # @userinfobot'tan aldığınız kendi sayısal ID'niz

TIMEZONE = pytz.timezone("Europe/Istanbul")
DB_PATH = "portfoy_hafiza.db"
WAITING_BUDGET = 1

# İzleme Listeleri
BIST_TICKERS = ["THYAO.IS", "TUPRS.IS", "ASELS.IS", "BIMAS.IS", "KCHOL.IS", "SISE.IS", "FROTO.IS"]
US_TICKERS = ["MSFT", "NVDA", "AAPL", "GOOGL", "QQQ", "SPY"]
TEFAS_FUNDS = ["AFT", "YAY", "AFA", "TI2", "MAC", "IIH"]

# Hedef Portföy Ağırlıkları (Toplam = 1.00)
TARGET_WEIGHTS = {
    "BIST": 0.30,    # %30 Borsa İstanbul
    "US": 0.25,      # %25 ABD Borsaları
    "TEFAS": 0.20,   # %20 TEFAS Fonları
    "GOLD": 0.15,    # %15 Gram Altın
    "BUFFER": 0.10,  # %10 Para Piyasası Fonu (Tampon)
}

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


# --- 2. VERİTABANI (PORTFÖY HAFIZASI) ---

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_class TEXT,
            symbol TEXT,
            quantity REAL,
            avg_cost_tl REAL,
            updated_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_orders_to_db(orders: list):
    """Onaylanan alım emirlerini veritabanına ekler veya mevcut miktarı günceller."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    for order in orders:
        c.execute("SELECT id, quantity, avg_cost_tl FROM holdings WHERE symbol = ?", (order["symbol"],))
        row = c.fetchone()
        if row:
            old_id, old_qty, old_cost = row
            new_qty = old_qty + order["quantity"]
            new_avg = ((old_qty * old_cost) + order["cost_tl"]) / new_qty if new_qty > 0 else 0
            c.execute(
                "UPDATE holdings SET quantity = ?, avg_cost_tl = ?, updated_at = ? WHERE id = ?",
                (new_qty, new_avg, now, old_id),
            )
        else:
            c.execute(
                "INSERT INTO holdings (asset_class, symbol, quantity, avg_cost_tl, updated_at) VALUES (?, ?, ?, ?, ?)",
                (order["asset_class"], order["symbol"], order["quantity"], order["unit_price_tl"], now),
            )
    conn.commit()
    conn.close()


# --- 3. CANLI FİYAT VE TEKNİK ANALİZ MOTORU ---

def get_usd_try() -> float:
    return float(yf.Ticker("TRY=X").history(period="1d")["Close"].iloc[-1])


def get_gram_gold_try(usd_try: float) -> float:
    ons = float(yf.Ticker("GC=F").history(period="1d")["Close"].iloc[-1])
    return round((ons / 31.1035) * usd_try, 2)


def get_current_portfolio_values(usd_try: float, gram_gold: float) -> dict:
    """Veritabanındaki varlıkların güncel TL değerlerini hesaplar."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT asset_class, symbol, quantity FROM holdings")
    rows = c.fetchall()
    conn.close()

    values = {"BIST": 0.0, "US": 0.0, "TEFAS": 0.0, "GOLD": 0.0, "BUFFER": 0.0}
    tefas = Crawler()
    today = datetime.date.today()
    start = today - datetime.timedelta(days=10)

    for asset_class, symbol, qty in rows:
        if qty <= 0:
            continue
        try:
            if asset_class == "BIST":
                price = float(yf.Ticker(f"{symbol}.IS").history(period="1d")["Close"].iloc[-1])
                values["BIST"] += qty * price
            elif asset_class == "US":
                price_usd = float(yf.Ticker(symbol).history(period="1d")["Close"].iloc[-1])
                values["US"] += qty * price_usd * usd_try
            elif asset_class == "GOLD":
                values["GOLD"] += qty * gram_gold
            elif asset_class == "BUFFER":
                values["BUFFER"] += qty
            elif asset_class == "TEFAS":
                df = tefas.fetch(start=start.strftime("%Y-%m-%d"), end=today.strftime("%Y-%m-%d"), name=symbol)
                if not df.empty:
                    price = float(df.sort_values("date")["price"].iloc[-1])
                    values["TEFAS"] += qty * price
        except Exception as e:
            logger.error(f"Değerleme hatası ({symbol}): {e}")
    return values


def calculate_rebalanced_allocations(new_budget: float, current_vals: dict) -> dict:
    """Satış yapmadan yeni nakdi hedef ağırlıktan geride kalan sınıflara yönlendirir."""
    total_existing = sum(current_vals.values())
    if total_existing == 0:
        return {k: new_budget * w for k, w in TARGET_WEIGHTS.items()}

    projected_total = total_existing + new_budget
    deficits = {}
    for k, target_w in TARGET_WEIGHTS.items():
        target_val = projected_total * target_w
        gap = max(0.0, target_val - current_vals.get(k, 0.0))
        deficits[k] = gap

    total_gap = sum(deficits.values())
    if total_gap == 0:
        return {k: new_budget * w for k, w in TARGET_WEIGHTS.items()}

    return {k: (deficits[k] / total_gap) * new_budget for k in TARGET_WEIGHTS}


def analyze_stock(ticker: str) -> dict | None:
    try:
        df = yf.Ticker(ticker).history(period="1y")
        if len(df) < 200:
            return None
        df["RSI"] = ta.rsi(df["Close"], length=14)
        df["EMA200"] = ta.ema(df["Close"], length=200)
        macd = ta.macd(df["Close"])
        df = pd.concat([df, macd], axis=1)

        last = df.iloc[-1]
        price = float(last["Close"])
        rsi = float(last["RSI"])
        ema200 = float(last["EMA200"])
        macd_val = float(last["MACD_12_26_9"])
        macd_sig = float(last["MACDs_12_26_9"])

        score = 50
        score += 20 if price > ema200 else -10
        if 30 <= rsi <= 45:
            score += 25
        elif rsi < 30:
            score += 15
        elif 45 < rsi <= 60:
            score += 10
        elif rsi > 70:
            score -= 25
        if macd_val > macd_sig:
            score += 15

        return {
            "ticker": ticker.replace(".IS", ""),
            "price": round(price, 2),
            "rsi": round(rsi, 1),
            "above_ema200": price > ema200,
            "score": max(0, min(100, score)),
        }
    except Exception:
        return None


def analyze_tefas() -> dict:
    try:
        tefas = Crawler()
        end_date = datetime.date.today()
        start_date = end_date - datetime.timedelta(days=35)
        best = {"code": "AFT", "return_1m": 0.0, "price": 1.0}
        max_ret = -999.0
        for code in TEFAS_FUNDS:
            df = tefas.fetch(start=start_date.strftime("%Y-%m-%d"), end=end_date.strftime("%Y-%m-%d"), name=code)
            if not df.empty and len(df) > 2:
                df = df.sort_values("date")
                p_start, p_end = float(df["price"].iloc[0]), float(df["price"].iloc[-1])
                ret = ((p_end - p_start) / p_start) * 100
                if ret > max_ret:
                    max_ret = ret
                    best = {"code": code, "return_1m": round(ret, 2), "price": p_end}
        return best
    except Exception:
        return {"code": "AFT", "return_1m": 0.0, "price": 1.0}


def generate_plan_and_orders(budget_tl: float) -> tuple[str, list]:
    usd_try = get_usd_try()
    gram_gold = get_gram_gold_try(usd_try)
    current_vals = get_current_portfolio_values(usd_try, gram_gold)
    allocs = calculate_rebalanced_allocations(budget_tl, current_vals)

    bist_res = sorted([r for t in BIST_TICKERS if (r := analyze_stock(t))], key=lambda x: x["score"], reverse=True)[:2]
    us_res = sorted([r for t in US_TICKERS if (r := analyze_stock(t))], key=lambda x: x["score"], reverse=True)[:2]
    top_tefas = analyze_tefas()

    orders = []
    spent_total = 0.0
    total_existing = sum(current_vals.values())

    lines = [
        f"⚖️ *REBALANCING & AYLIK ALIM PLANI*",
        f"💰 *Yeni Bütçe:* {budget_tl:,.0f} TL | *Mevcut Portföy:* {total_existing:,.0f} TL",
        f"💵 *Kur:* {usd_try:.2f} TL | *Gram Altın:* {gram_gold:,.0f} TL",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # 1. BIST
    lines.append("\n🇹🇷 *1. BORSA İSTANBUL (Tam Lot)*")
    if allocs["BIST"] > 100 and bist_res:
        per_bist = allocs["BIST"] / len(bist_res)
        for item in bist_res:
            lots = int(per_bist // item["price"])
            if lots > 0:
                cost = lots * item["price"]
                spent_total += cost
                orders.append({
                    "asset_class": "BIST", "symbol": item["ticker"],
                    "quantity": lots, "unit_price_tl": item["price"], "cost_tl": cost
                })
                lines.append(f"• *{item['ticker']}* (Skor: {item['score']} | RSI: {item['rsi']}) 👉 *{lots} Lot Al* ({cost:,.0f} TL)")
    else:
        lines.append("• _Hedef ağırlık üzerinde olduğu için bu ay BIST alımı pas geçildi._")

    # 2. ABD
    lines.append("\n🇺🇸 *2. ABD BORSALARI (Kesirli Pay)*")
    if allocs["US"] > 100 and us_res:
        per_us_tl = allocs["US"] / len(us_res)
        for item in us_res:
            usd_amt = per_us_tl / usd_try
            shares = round(usd_amt / item["price"], 4)
            spent_total += per_us_tl
            orders.append({
                "asset_class": "US", "symbol": item["ticker"],
                "quantity": shares, "unit_price_tl": item["price"] * usd_try, "cost_tl": per_us_tl
            })
            lines.append(f"• *{item['ticker']}* (Skor: {item['score']} | RSI: {item['rsi']}) 👉 *{shares} Adet Al* ({per_us_tl:,.0f} TL)")
    else:
        lines.append("• _Hedef ağırlık üzerinde olduğu için bu ay ABD alımı pas geçildi._")

    # 3. TEFAS
    lines.append("\n📈 *3. TEFAS FONU*")
    if allocs["TEFAS"] > 100:
        fund_qty = round(allocs["TEFAS"] / top_tefas["price"], 2)
        spent_total += allocs["TEFAS"]
        orders.append({
            "asset_class": "TEFAS", "symbol": top_tefas["code"],
            "quantity": fund_qty, "unit_price_tl": top_tefas["price"], "cost_tl": allocs["TEFAS"]
        })
        lines.append(f"• *{top_tefas['code']}* (1A Getiri: %{top_tefas['return_1m']}) 👉 *{allocs['TEFAS']:,.0f} TL Al*")

    # 4. ALTIN & TAMPON
    lines.append("\n🥇 *4. ALTIN & LİKİT TAMPON*")
    if allocs["GOLD"] > 100:
        grams = round(allocs["GOLD"] / gram_gold, 2)
        spent_total += allocs["GOLD"]
        orders.append({
            "asset_class": "GOLD", "symbol": "GRAM_ALTIN",
            "quantity": grams, "unit_price_tl": gram_gold, "cost_tl": allocs["GOLD"]
        })
        lines.append(f"• *Gram Altın:* 👉 *{allocs['GOLD']:,.0f} TL Al* ({grams} gr)")

    remaining = max(0.0, budget_tl - spent_total)
    if remaining > 0:
        orders.append({
            "asset_class": "BUFFER", "symbol": "PPF_NAKIT",
            "quantity": remaining, "unit_price_tl": 1.0, "cost_tl": remaining
        })
        lines.append(f"• *Para Piyasası Fonu (PPF):* 👉 *{remaining:,.0f} TL Park Et*")

    return "\n".join(lines), orders


# --- 4. TELEGRAM HANDLER'LARI ---

async def scheduled_monthly_prompt(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=ALLOWED_CHAT_ID,
        text="🗓️ *Bugün ayın 15'i!* Portföy dengeleme ve analiz için /analiz yazarak bu ayki bütçenizi girin.",
        parse_mode="Markdown",
    )


async def show_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    usd_try = get_usd_try()
    gram_gold = get_gram_gold_try(usd_try)
    vals = get_current_portfolio_values(usd_try, gram_gold)
    total = sum(vals.values())
    if total == 0:
        await update.message.reply_text("📭 Portföy hafızanız henüz boş. /analiz ile ilk planınızı oluşturup onaylayın.")
        return

    msg = [f"💼 *GÜNCEL PORTFÖY DURUMU (Toplam: {total:,.0f} TL)*\n"]
    for k, v in vals.items():
        pct = (v / total) * 100
        target_pct = TARGET_WEIGHTS[k] * 100
        msg.append(f"• *{k}:* {v:,.0f} TL (Mevcut: %{pct:.1f} | Hedef: %{target_pct:.0f})")
    await update.message.reply_text("\n".join(msg), parse_mode="Markdown")


async def start_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return ConversationHandler.END
    await update.message.reply_text("💰 Bu ayki yatırım bütçeniz kaç TL? (Örn: `40000`):", parse_mode="Markdown")
    return WAITING_BUDGET


async def receive_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return ConversationHandler.END
    text = update.message.text.replace(".", "").replace(",", "").strip()
    if not text.isdigit():
        await update.message.reply_text("⚠️ Geçerli bir rakam girin (Örn: `40000`).")
        return WAITING_BUDGET

    budget = float(text)
    await update.message.reply_text("⏳ *Mevcut portföy ağırlıkları ve canlı teknik veriler hesaplanıyor...*", parse_mode="Markdown")

    report, orders = generate_plan_and_orders(budget)
    context.user_data["pending_orders"] = orders

    keyboard = [[InlineKeyboardButton("✅ Emirleri Uyguladım (Portföye İşle)", callback_data="confirm_orders")]]
    await update.message.reply_text(report, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "confirm_orders":
        orders = context.user_data.get("pending_orders", [])
        if orders:
            save_orders_to_db(orders)
            context.user_data["pending_orders"] = []
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("💾 *Alımlar portföy hafızasına işlendi!* Gelecek ayki rebalancing bu bakiyelere göre hesaplanacak.", parse_mode="Markdown")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ İptal edildi.")
    return ConversationHandler.END


def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("analiz", start_analysis)],
        states={WAITING_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_budget)]},
        fallbacks=[CommandHandler("iptal", cancel)],
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("portfoy", show_portfolio))
    app.add_handler(CallbackQueryHandler(button_callback))

    app.job_queue.run_monthly(
        scheduled_monthly_prompt,
        when=datetime.time(hour=10, minute=30, tzinfo=TIMEZONE),
        day=15,
    )

    logger.info("Rebalancing özellikli Telegram Yatırım Botu aktif...")
    app.run_polling()


if __name__ == "__main__":
    main()
