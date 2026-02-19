"""
Trade Manager - Risk/Reward Based Position Management for MetaTrader5
Features:
- Breakeven at 1:1 RR
- Partial closes at RR targets (1:3, 1:6, 1:10)
- Risk-based lot calculation
- Trade logging/journaling
- Multi-symbol support

Strategy:
- Move SL to breakeven at 1:1 RR
- Close 30% at 1:3 RR
- Close 30% at 1:6 RR
- Close 30% at 1:10 RR
- Leave 10% runner for manual close
"""

import MetaTrader5 as mt5
import time
from datetime import datetime
import json
import os
from dataclasses import dataclass, field
from typing import Optional, List, Dict
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trade_manager.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class TradeConfig:
    """Configuration for trade management"""
    # Symbols to manage (empty = all symbols)
    symbols: List[str] = None
    
    # Breakeven Settings (based on Risk/Reward)
    breakeven_enabled: bool = True
    breakeven_rr: float = 1.0                # Move SL to BE at this RR (1:1)
    breakeven_padding_pips: float = 1.0      # Small profit to lock in at BE
    
    # Partial Close Settings (based on Risk/Reward)
    partial_close_enabled: bool = True
    partial_close_targets: List[Dict] = None  # [{rr: 3, close_percent: 30}, ...]
    
    # Risk Management
    risk_percent: float = 1.0                # Risk X% of account per trade
    max_daily_loss_percent: float = 5.0      # Stop trading after X% daily loss
    max_positions: int = 10                  # Maximum concurrent positions
    
    # Profit Target
    daily_profit_target: float = 0.0         # Close all if daily profit hits (0=disabled)
    
    # General
    magic_number: int = 0                    # Filter by magic (0 = all)
    check_interval_seconds: float = 1.0      # How often to check positions
    
    # Email Notifications
    email_enabled: bool = False
    email_smtp_server: str = "smtp.gmail.com"
    email_smtp_port: int = 587
    email_sender: str = ""                   # Your email address
    email_password: str = ""                 # App password (not regular password)
    email_recipient: str = ""                # Where to send alerts (can be same as sender)
    
    def __post_init__(self):
        if self.symbols is None:
            self.symbols = []
        if self.partial_close_targets is None:
            # Default: 30% at 1:3, 30% at 1:6, 30% at 1:10
            # Remaining 10% is the runner (manual close)
            self.partial_close_targets = [
                {"rr": 3, "close_percent": 30},
                {"rr": 6, "close_percent": 30},
                {"rr": 10, "close_percent": 30}
            ]


class TradeManager:
    def __init__(self, config: TradeConfig = None):
        self.config = config or TradeConfig()
        self.connected = False
        self.running = False
        self.partial_closes_done = {}  # {ticket: [closed_rr_targets]}
        self.breakeven_done = set()    # tickets that hit breakeven
        self.daily_start_balance = 0
        self.trade_journal = []
        # Track initial risk (SL distance) for each position
        self.position_risk = {}  # {ticket: initial_sl_distance_in_price}
        
    def connect(self) -> bool:
        """Initialize connection to MetaTrader5"""
        if not mt5.initialize():
            logger.error(f"MT5 initialize() failed, error: {mt5.last_error()}")
            return False
        
        account_info = mt5.account_info()
        if account_info is None:
            logger.error("Failed to get account info")
            return False
            
        logger.info(f"Connected to MT5 - Account: {account_info.login}, "
                   f"Balance: ${account_info.balance:.2f}, "
                   f"Server: {account_info.server}")
        
        self.connected = True
        self.daily_start_balance = account_info.balance
        return True
    
    def disconnect(self):
        """Shutdown MT5 connection"""
        mt5.shutdown()
        self.connected = False
        logger.info("Disconnected from MT5")
    
    def send_email(self, subject: str, body: str):
        """Send email notification"""
        if not self.config.email_enabled:
            return
        
        if not all([self.config.email_sender, self.config.email_password, self.config.email_recipient]):
            logger.warning("Email not configured properly - skipping notification")
            return
        
        try:
            msg = MIMEMultipart()
            msg['From'] = self.config.email_sender
            msg['To'] = self.config.email_recipient
            msg['Subject'] = f"🔔 Trade Manager: {subject}"
            
            # Add timestamp to body
            full_body = f"{body}\n\n---\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            msg.attach(MIMEText(full_body, 'plain'))
            
            with smtplib.SMTP(self.config.email_smtp_server, self.config.email_smtp_port) as server:
                server.starttls()
                server.login(self.config.email_sender, self.config.email_password)
                server.send_message(msg)
            
            logger.info(f"Email sent: {subject}")
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
    
    def get_pip_value(self, symbol: str) -> float:
        """Get pip value for a symbol (handles JPY pairs, gold, etc.)"""
        info = mt5.symbol_info(symbol)
        if info is None:
            return 0.0001  # Default
        
        # For gold (XAUUSD) pip is typically 0.01
        if "XAU" in symbol.upper() or "GOLD" in symbol.upper():
            return 0.01
        # For JPY pairs, pip is 0.01
        elif "JPY" in symbol.upper():
            return 0.01
        # For most forex pairs
        else:
            return 0.0001
    
    def pips_to_price(self, symbol: str, pips: float) -> float:
        """Convert pips to price movement"""
        return pips * self.get_pip_value(symbol)
    
    def price_to_pips(self, symbol: str, price_diff: float) -> float:
        """Convert price movement to pips"""
        pip_value = self.get_pip_value(symbol)
        if pip_value == 0:
            return 0
        return price_diff / pip_value
    
    def get_positions(self) -> List:
        """Get positions filtered by config"""
        positions = mt5.positions_get()
        if positions is None:
            return []
        
        filtered = []
        for pos in positions:
            # Filter by symbol
            if self.config.symbols and pos.symbol not in self.config.symbols:
                continue
            # Filter by magic number
            if self.config.magic_number and pos.magic != self.config.magic_number:
                continue
            filtered.append(pos)
        
        return filtered
    
    def get_current_price(self, symbol: str, position_type: int) -> Optional[float]:
        """Get current price for closing a position"""
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        # For buy positions, close at bid; for sell, close at ask
        return tick.bid if position_type == mt5.ORDER_TYPE_BUY else tick.ask
    
    def get_initial_risk(self, position) -> float:
        """Get or calculate the initial risk (SL distance) for a position"""
        ticket = position.ticket
        
        # Return cached value if available
        if ticket in self.position_risk:
            return self.position_risk[ticket]
        
        # Calculate initial risk from current SL (assumes SL hasn't been moved yet)
        if position.sl == 0:
            logger.warning(f"Position {ticket} has no SL set - cannot calculate RR")
            return 0
        
        if position.type == mt5.ORDER_TYPE_BUY:
            risk = position.price_open - position.sl
        else:
            risk = position.sl - position.price_open
        
        # Store for future reference
        self.position_risk[ticket] = abs(risk)
        logger.info(f"Tracked position {ticket}: Initial risk = {abs(risk):.5f} price units")
        
        return abs(risk)
    
    def calculate_current_rr(self, position) -> float:
        """Calculate current Risk/Reward ratio for a position"""
        current_price = self.get_current_price(position.symbol, position.type)
        if current_price is None:
            return 0
        
        initial_risk = self.get_initial_risk(position)
        if initial_risk == 0:
            return 0
        
        # Calculate current profit in price
        if position.type == mt5.ORDER_TYPE_BUY:
            profit_price = current_price - position.price_open
        else:
            profit_price = position.price_open - current_price
        
        # RR = profit / risk
        return profit_price / initial_risk
    
    def calculate_profit_pips(self, position) -> float:
        """Calculate current profit in pips for a position (for display)"""
        current_price = self.get_current_price(position.symbol, position.type)
        if current_price is None:
            return 0
        
        if position.type == mt5.ORDER_TYPE_BUY:
            profit_price = current_price - position.price_open
        else:
            profit_price = position.price_open - current_price
            
        return self.price_to_pips(position.symbol, profit_price)
    
    def modify_sl(self, position, new_sl: float) -> bool:
        """Modify stop loss for a position"""
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": position.ticket,
            "symbol": position.symbol,
            "sl": new_sl,
            "tp": position.tp
        }
        
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            error = result.comment if result else mt5.last_error()
            logger.error(f"Failed to modify SL for {position.ticket}: {error}")
            return False
        
        logger.info(f"Modified SL for ticket {position.ticket}: {position.sl:.5f} -> {new_sl:.5f}")
        return True
    
    def partial_close(self, position, percent: float) -> bool:
        """Close a percentage of a position"""
        symbol_info = mt5.symbol_info(position.symbol)
        if symbol_info is None:
            return False
        
        # Calculate volume to close
        close_volume = position.volume * (percent / 100)
        # Round to lot step
        lot_step = symbol_info.volume_step
        close_volume = round(close_volume / lot_step) * lot_step
        close_volume = max(close_volume, symbol_info.volume_min)
        
        if close_volume >= position.volume:
            close_volume = position.volume
        
        # Determine close type and price
        close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(position.symbol)
        close_price = tick.bid if position.type == mt5.ORDER_TYPE_BUY else tick.ask
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": close_volume,
            "type": close_type,
            "price": close_price,
            "position": position.ticket,
            "type_filling": mt5.ORDER_FILLING_IOC,
            "comment": f"Partial close {percent}%"
        }
        
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            error = result.comment if result else mt5.last_error()
            logger.error(f"Partial close failed for {position.ticket}: {error}")
            return False
        
        logger.info(f"Partial closed {close_volume} lots ({percent}%) of ticket {position.ticket}")
        self.log_trade_action(position, "PARTIAL_CLOSE", f"{percent}% = {close_volume} lots")
        return True
    
    def close_position(self, position) -> bool:
        """Fully close a position"""
        close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(position.symbol)
        close_price = tick.bid if position.type == mt5.ORDER_TYPE_BUY else tick.ask
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": close_type,
            "price": close_price,
            "position": position.ticket,
            "type_filling": mt5.ORDER_FILLING_IOC,
            "comment": "Trade Manager Close"
        }
        
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            error = result.comment if result else mt5.last_error()
            logger.error(f"Close failed for {position.ticket}: {error}")
            return False
        
        logger.info(f"Closed position {position.ticket}")
        self.log_trade_action(position, "CLOSE", f"Profit: ${position.profit:.2f}")
        return True
    
    def manage_breakeven(self, position) -> bool:
        """Move SL to breakeven at 1:1 RR"""
        if not self.config.breakeven_enabled:
            return False
            
        if position.ticket in self.breakeven_done:
            return False
        
        current_rr = self.calculate_current_rr(position)
        
        if current_rr >= self.config.breakeven_rr:
            padding = self.pips_to_price(position.symbol, self.config.breakeven_padding_pips)
            
            if position.type == mt5.ORDER_TYPE_BUY:
                new_sl = position.price_open + padding
                if position.sl >= new_sl:
                    return False
            else:
                new_sl = position.price_open - padding
                if position.sl != 0 and position.sl <= new_sl:
                    return False
            
            if self.modify_sl(position, new_sl):
                self.breakeven_done.add(position.ticket)
                self.log_trade_action(position, "BREAKEVEN", f"SL moved to BE at 1:{self.config.breakeven_rr} RR")
                
                # Send email notification
                pos_type = "BUY" if position.type == mt5.ORDER_TYPE_BUY else "SELL"
                self.send_email(
                    f"Breakeven Hit - {position.symbol}",
                    f"Position secured at breakeven!\n\n"
                    f"Symbol: {position.symbol}\n"
                    f"Type: {pos_type}\n"
                    f"Ticket: {position.ticket}\n"
                    f"Entry: {position.price_open:.5f}\n"
                    f"New SL: {new_sl:.5f}\n"
                    f"Current Profit: ${position.profit:.2f}"
                )
                return True
        
        return False
    
    def manage_partial_close(self, position) -> bool:
        """Manage partial closes at RR targets (30% at 1:3, 30% at 1:6, 30% at 1:10)"""
        if not self.config.partial_close_enabled:
            return False
        
        current_rr = self.calculate_current_rr(position)
        ticket = position.ticket
        
        if ticket not in self.partial_closes_done:
            self.partial_closes_done[ticket] = []
        
        for target in self.config.partial_close_targets:
            target_rr = target["rr"]
            close_percent = target["close_percent"]
            
            # Skip if already closed at this target
            if target_rr in self.partial_closes_done[ticket]:
                continue
            
            if current_rr >= target_rr:
                if self.partial_close(position, close_percent):
                    self.partial_closes_done[ticket].append(target_rr)
                    logger.info(f"Partial close at 1:{target_rr} RR for ticket {ticket}")
                    
                    # Send email notification
                    pos_type = "BUY" if position.type == mt5.ORDER_TYPE_BUY else "SELL"
                    partials_done = len(self.partial_closes_done[ticket])
                    remaining = 100 - (partials_done * 30)  # 30% each partial
                    self.send_email(
                        f"Partial Close #{partials_done} - {position.symbol}",
                        f"Partial profit taken at 1:{target_rr} RR!\n\n"
                        f"Symbol: {position.symbol}\n"
                        f"Type: {pos_type}\n"
                        f"Ticket: {ticket}\n"
                        f"Closed: {close_percent}%\n"
                        f"Remaining: {remaining}%\n"
                        f"Current RR: {current_rr:.2f}\n"
                        f"Profit: ${position.profit:.2f}"
                    )
                    return True
        
        return False
    
    def check_daily_limits(self) -> bool:
        """Check if daily loss/profit limits are hit"""
        account = mt5.account_info()
        if account is None:
            return True
        
        daily_pnl = account.balance - self.daily_start_balance
        daily_pnl_percent = (daily_pnl / self.daily_start_balance) * 100
        
        # Check daily loss limit
        if daily_pnl_percent <= -self.config.max_daily_loss_percent:
            logger.warning(f"Daily loss limit hit: {daily_pnl_percent:.2f}%")
            return False
        
        # Check daily profit target
        if self.config.daily_profit_target > 0 and daily_pnl >= self.config.daily_profit_target:
            logger.info(f"Daily profit target hit: ${daily_pnl:.2f}")
            self.close_all_positions()
            return False
        
        return True
    
    def close_all_positions(self):
        """Close all managed positions"""
        positions = self.get_positions()
        for pos in positions:
            self.close_position(pos)
    
    def calculate_lot_size(self, symbol: str, sl_pips: float) -> float:
        """Calculate lot size based on risk percentage"""
        account = mt5.account_info()
        if account is None:
            return 0.01
        
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            return 0.01
        
        # Risk amount in account currency
        risk_amount = account.balance * (self.config.risk_percent / 100)
        
        # Get pip value per lot
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return 0.01
        
        pip_value = self.get_pip_value(symbol)
        
        # Calculate contract value per pip
        # For forex: pip_value * contract_size / price
        # Simplified calculation
        if symbol_info.trade_contract_size > 0:
            point_value = symbol_info.trade_tick_value
            pip_per_point = pip_value / symbol_info.point
            pip_value_per_lot = point_value * pip_per_point
        else:
            pip_value_per_lot = 10  # Default assumption
        
        # Calculate lots
        if sl_pips > 0 and pip_value_per_lot > 0:
            lots = risk_amount / (sl_pips * pip_value_per_lot)
        else:
            lots = 0.01
        
        # Round to lot step and apply min/max
        lot_step = symbol_info.volume_step
        lots = round(lots / lot_step) * lot_step
        lots = max(symbol_info.volume_min, min(lots, symbol_info.volume_max))
        
        return lots
    
    def log_trade_action(self, position, action: str, details: str = ""):
        """Log trade action to journal"""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "ticket": position.ticket,
            "symbol": position.symbol,
            "type": "BUY" if position.type == mt5.ORDER_TYPE_BUY else "SELL",
            "volume": position.volume,
            "action": action,
            "details": details,
            "profit": position.profit
        }
        self.trade_journal.append(entry)
        
        # Save to file
        journal_file = "trade_journal.json"
        try:
            existing = []
            if os.path.exists(journal_file):
                with open(journal_file, 'r') as f:
                    existing = json.load(f)
            existing.append(entry)
            with open(journal_file, 'w') as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save journal: {e}")
    
    def print_status(self):
        """Print current positions status"""
        positions = self.get_positions()
        account = mt5.account_info()
        
        print("\n" + "="*80)
        print(f"TRADE MANAGER STATUS - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("="*80)
        
        if account:
            daily_pnl = account.equity - self.daily_start_balance
            print(f"Balance: ${account.balance:.2f} | Equity: ${account.equity:.2f} | "
                  f"Daily P/L: ${daily_pnl:.2f}")
        
        print("-"*80)
        print(f"{'Ticket':<10} {'Symbol':<12} {'Type':<6} {'Lots':<8} {'Profit':<10} {'RR':<8} {'BE':<4} {'Partials':<12}")
        print("-"*80)
        
        total_profit = 0
        for pos in positions:
            current_rr = self.calculate_current_rr(pos)
            pos_type = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
            be_str = "Yes" if pos.ticket in self.breakeven_done else "No"
            partials = self.partial_closes_done.get(pos.ticket, [])
            partials_str = ",".join([f"1:{rr}" for rr in partials]) if partials else "None"
            print(f"{pos.ticket:<10} {pos.symbol:<12} {pos_type:<6} {pos.volume:<8.2f} "
                  f"${pos.profit:<9.2f} {current_rr:<8.2f} {be_str:<4} {partials_str:<12}")
            total_profit += pos.profit
        
        print("-"*80)
        print(f"Total Positions: {len(positions)} | Total Profit: ${total_profit:.2f}")
        print("="*80 + "\n")
    
    def manage_position(self, position):
        """Apply all management rules to a position"""
        # Track initial risk for new positions
        self.get_initial_risk(position)
        
        # 1. Breakeven check at 1:1 RR (one-time)
        self.manage_breakeven(position)
        
        # 2. Partial close check at RR targets
        self.manage_partial_close(position)
        
        # Runner (remaining 10%) is left for manual close
    
    def run(self, show_status_interval: int = 30):
        """Main loop to manage all positions"""
        if not self.connected:
            if not self.connect():
                return
        
        self.running = True
        last_status_time = 0
        
        logger.info("Trade Manager started - Press Ctrl+C to stop")
        print("\nTrade Manager Configuration (RR-Based):")
        print(f"  Breakeven: At 1:{self.config.breakeven_rr} RR -> lock {self.config.breakeven_padding_pips} pips profit")
        print(f"  Partial Closes:")
        for target in self.config.partial_close_targets:
            print(f"    - {target['close_percent']}% at 1:{target['rr']} RR")
        print(f"  Runner: 10% left for manual close")
        print(f"  Max Daily Loss: {self.config.max_daily_loss_percent}%")
        print(f"  Email Notifications: {'Enabled' if self.config.email_enabled else 'Disabled'}")
        print()
        
        # Send startup notification
        account = mt5.account_info()
        if account:
            self.send_email(
                "Trade Manager Started",
                f"Trade Manager is now running and monitoring your positions.\\n\\n"
                f"Account: {account.login}\\n"
                f"Balance: ${account.balance:.2f}\\n"
                f"Equity: ${account.equity:.2f}\\n\\n"
                f"Settings:\\n"
                f"- Breakeven at 1:{self.config.breakeven_rr} RR\\n"
                f"- Partials: 30% at 1:3, 30% at 1:6, 30% at 1:10\\n"
                f"- Runner: 10% for manual close"
            )
        
        try:
            while self.running:
                # Check daily limits
                if not self.check_daily_limits():
                    logger.warning("Daily limit reached - stopping")
                    break
                
                # Get and manage positions
                positions = self.get_positions()
                for position in positions:
                    self.manage_position(position)
                
                # Clean up tracking for closed positions
                open_tickets = {p.ticket for p in positions}
                self.partial_closes_done = {k: v for k, v in self.partial_closes_done.items() 
                                           if k in open_tickets}
                self.breakeven_done = self.breakeven_done.intersection(open_tickets)
                self.position_risk = {k: v for k, v in self.position_risk.items()
                                     if k in open_tickets}
                
                # Periodic status update
                current_time = time.time()
                if current_time - last_status_time >= show_status_interval:
                    self.print_status()
                    last_status_time = current_time
                
                time.sleep(self.config.check_interval_seconds)
                
        except KeyboardInterrupt:
            logger.info("Trade Manager stopped by user")
        finally:
            self.running = False
            self.disconnect()


def load_config_from_file(filepath: str = "config.json") -> TradeConfig:
    """Load configuration from JSON file"""
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            data = json.load(f)
            return TradeConfig(**data)
    return TradeConfig()


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    # Load config from file or use defaults
    config = load_config_from_file("config.json")
    
    # Or configure programmatically:
    # config = TradeConfig(
    #     symbols=["XAUUSD.m", "EURUSD"],
    #     breakeven_rr=1.0,           # Move SL to BE at 1:1 RR
    #     breakeven_padding_pips=1.0,
    #     partial_close_targets=[
    #         {"rr": 3, "close_percent": 30},   # 30% at 1:3 RR
    #         {"rr": 6, "close_percent": 30},   # 30% at 1:6 RR
    #         {"rr": 10, "close_percent": 30}   # 30% at 1:10 RR
    #     ],                                     # Remaining 10% = runner
    #     risk_percent=1.0,
    #     max_daily_loss_percent=3.0
    # )
    
    manager = TradeManager(config)
    manager.run(show_status_interval=30)
