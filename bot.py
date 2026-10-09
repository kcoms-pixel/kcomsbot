import requests, time

BOT_TOKEN = "8171259755:AAHhiOkyZ6KOTZ6bZ6KOTZ6bZ6KOTZ6"
CHAT_ID = "7482627767"

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=15)
        print("Telegram sent!")
    except Exception as e:
        print(f"Telegram error: {e}")

print("KCOMS BOT V6 - KRAKEN - NO RATE LIMIT")
send_telegram("🚀 Kcoms_bot V6 LIVE! 24/7 on Koyeb!")

wins = 0
losses = 0
total_pnl = 0.0
active_trade = None

def get_price():
    try:
        url = "https://api.kraken.com/0/public/Ticker?pair=BTCUSD"
        r = requests.get(url, timeout=15).json()
        price = float(r['result']['XXBTZUSD']['c'][0])
        print(f"Got price: {price}")
        return price
    except Exception as e:
        print(f"Price error: {e}")
        return None

def check_signal():
    global active_trade, wins, losses, total_pnl
    price = get_price()
    if not price:
        return

    print(f"Price: {price} | Trade: {active_trade}")

    if not active_trade:
        active_trade = {'side': 'LONG', 'entry': price, 'tp': price*1.004, 'sl': price*0.998}
        msg = f"📈 LONG ENTRY {price:.2f}\nTP: {active_trade['tp']:.2f} (+0.4%)\nSL: {active_trade['sl']:.2f} (-0.2%)"
        send_telegram(msg)
        print("Entry sent")
    else:
        tp = active_trade['tp']
        sl = active_trade['sl']
        hit = None
        if price >= tp: hit = "WIN"
        elif price <= sl: hit = "LOSS"

        if hit:
            if hit == "WIN":
                wins += 1
                total_pnl += 0.4
                msg = f"✅ TP HIT! WIN +0.4%\nLONG {active_trade['entry']:.2f} -> {tp:.2f}\n📊 TODAY: {wins}W - {losses}L\nTotal PnL: {total_pnl:.1f}%"
            else:
                losses += 1
                total_pnl -= 0.2
                msg = f"❌ SL HIT! LOSS -0.2%\nLONG {active_trade['entry']:.2f} -> {sl:.2f}\n📊 TODAY: {wins}W - {losses}L\nTotal PnL: {total_pnl:.1f}%"
            send_telegram(msg)
            active_trade = None
            print(f"{hit} sent!")

while True:
    try:
        check_signal()
        print("Waiting 30 sec...")
        time.sleep(30)
    except Exception as e:
        print(f"Loop Error: {e}")
        time.sleep(30)
