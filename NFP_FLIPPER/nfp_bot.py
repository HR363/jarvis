import MetaTrader5 as mt5
import time
from datetime import datetime, timedelta
import pytz

# --- STRATEGY SETTINGS ---
SYMBOL = "XAUUSD"       
# Stop Loss and Take Profit Settings (in Pips)
STOP_LOSS_PIPS = 10     
TAKE_PROFIT_PIPS = 100   
BREAKEVEN_TRIGGER_PIPS = 15 # Move SL to BE when price moves this many pips in profit
BREAKEVEN_PADDING = 2       # Small profit to lock in (pips)


# --- LAYERING STRATEGY ---
ORDERS_CONFIG = [
    { "distance": 20, "lot": 0.05 },  # Layer 1: Smallest risk close to price
    { "distance": 30, "lot": 0.5 },  # Layer 2: Medium risk
    { "distance": 40, "lot": 2 }   # Layer 3: High reward if trend flies
]

DELETE_PENDING_AFTER = 120 # Delete pending orders 2 minutes after news if not triggered

def connect_mt5():
    if not mt5.initialize():
        print("initialize() failed, error code =", mt5.last_error())
        return False
    print(f"Connected to MetaTrader 5. Terminal: {mt5.version()}")
    return True

def get_current_price(symbol):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        print(f"Symbol {symbol} not found")
        return None, None
    return tick.bid, tick.ask

def send_order(symbol, order_type, price, sl, tp, lot_size, comment="NFP Bot"):
    request = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": symbol,
        "volume": lot_size,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "type_time": mt5.ORDER_TIME_SPECIFIED,
        "expiration": int(time.time() + DELETE_PENDING_AFTER),
        "comment": comment,
        "type_filling": mt5.ORDER_FILLING_RETURN, # Try ORDER_FILLING_IOC or ORDER_FILLING_FOK if this fails
    }
    
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"Order failed: {result.retcode} ({result.comment})")
        return None
    
    print(f"Placed {lot_size} lot {'BUY' if order_type == mt5.ORDER_TYPE_BUY_STOP else 'SELL'} STOP @ {price:.5f}")
    return result.order

def check_breakeven():
    positions = mt5.positions_get(symbol=SYMBOL)
    if positions is None or len(positions) == 0:
        return

    symbol_info = mt5.symbol_info(SYMBOL)
    point = symbol_info.point
    trigger_points = BREAKEVEN_TRIGGER_PIPS * 10 * point
    padding_points = BREAKEVEN_PADDING * 10 * point

    for pos in positions:
        # BUY POSITION
        if pos.type == mt5.ORDER_TYPE_BUY:
            current_bid = mt5.symbol_info_tick(SYMBOL).bid
            profit_distance = current_bid - pos.price_open
            
            target_sl = pos.price_open + padding_points
            
            # Condition: Profit > Trigger AND Current SL is worse than Target SL
            if profit_distance > trigger_points and pos.sl < target_sl:
                request = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": pos.ticket,
                    "symbol": SYMBOL,
                    "sl": target_sl,
                    "tp": pos.tp
                }
                res = mt5.order_send(request)
                if res.retcode == mt5.TRADE_RETCODE_DONE:
                    print(f"PROTECTION: Moved BUY {pos.ticket} SL to BreakEven (+{BREAKEVEN_PADDING} pips)")

        # SELL POSITION
        elif pos.type == mt5.ORDER_TYPE_SELL:
            current_ask = mt5.symbol_info_tick(SYMBOL).ask
            profit_distance = pos.price_open - current_ask
            
            target_sl = pos.price_open - padding_points

            # Condition: Profit > Trigger AND (No SL OR Current SL is worse than Target SL)
            if profit_distance > trigger_points and (pos.sl == 0.0 or pos.sl > target_sl):
                request = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": pos.ticket,
                    "symbol": SYMBOL,
                    "sl": target_sl,
                    "tp": pos.tp
                }
                res = mt5.order_send(request)
                if res.retcode == mt5.TRADE_RETCODE_DONE:
                    print(f"PROTECTION: Moved SELL {pos.ticket} SL to BreakEven (+{BREAKEVEN_PADDING} pips)")

def main():
    if not connect_mt5():
        return

    # Check if symbol exists and is visible
    symbol_info = mt5.symbol_info(SYMBOL)
    if symbol_info is None:
        print(f"Symbol {SYMBOL} not found. Adding to Market Watch...")
        mt5.symbol_select(SYMBOL, True)
        symbol_info = mt5.symbol_info(SYMBOL)
        
    if not symbol_info.visible:
        print(f"{SYMBOL} is not visible, trying to switch on")
        if not mt5.symbol_select(SYMBOL, True):
            print(f"symbol_select({SYMBOL}) failed, exit")
            return

    # 1. Manual Trigger
    print("\n--- BOT READY ---")
    print(f"Strategy: Straddle {SYMBOL} with 3 layers.")
    print("NOTE: The previous version used your COMPUTER'S local time, not MT5 server time.")
    print("      Switching to manual mode avoids any timezone confusion.")
    
    input("\n>>> Press ENTER now to PLACE ORDERS immediately <<<")

    print("\n--- EXECUTING NFP STRATEGY ---")
    
    # 2. Get Price ONCE to ensure all layers are based on same reference price
    bid, ask = get_current_price(SYMBOL)
    if bid is None: return

    point = symbol_info.point
    print(f"Current Reference Price - Bid: {bid}, Ask: {ask}")

    # 3. Loop through layers and place orders
    for i, layer in enumerate(ORDERS_CONFIG):
        dist_pips = layer["distance"]
        lot = layer["lot"]
        
        # Convert pips to price points (handling 3/5 digit brokers)
        # Usually point is 0.00001 for 5 decimals. 1 Pip = 10 Points.
        dist_points = dist_pips * 10 * point 
        sl_points = STOP_LOSS_PIPS * 10 * point
        tp_points = TAKE_PROFIT_PIPS * 10 * point
        
        # --- BUY SIDE ---
        # Buy Stop is placed ABOVE Ask
        buy_price = ask + dist_points
        buy_sl = buy_price - sl_points
        buy_tp = buy_price + tp_points
        
        send_order(SYMBOL, mt5.ORDER_TYPE_BUY_STOP, buy_price, buy_sl, buy_tp, lot, f"NFP_L{i+1}_Buy")

        # --- SELL SIDE ---
        # Sell Stop is placed BELOW Bid
        sell_price = bid - dist_points
        sell_sl = sell_price + sl_points
        sell_tp = sell_price - tp_points
        
        send_order(SYMBOL, mt5.ORDER_TYPE_SELL_STOP, sell_price, sell_sl, sell_tp, lot, f"NFP_L{i+1}_Sell")

    print("\n--- ORDERS PLACED. MONITORING FOR BREAKEVEN ---")
    print("Press Ctrl+C to stop the bot.")
    
    try:
        while True:
            check_breakeven()
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nStopping bot...")
    
    mt5.shutdown()

if __name__ == "__main__":
    main()
