"""
Notification Module
Discord and Telegram alerts
"""
import logging
from typing import Dict, Any, Optional, FrozenSet
import aiohttp
from datetime import datetime

logger = logging.getLogger(__name__)

# Discord: only these strategies may trigger trade / exit alerts (execution outcomes).
DISCORD_TRADE_STRATEGIES: FrozenSet[str] = frozenset(
    {"bitcoin", "sol_lag", "eth_lag", "hype_lag", "xrp_dump_hedge"}
)

# Short Discord titles for executed trades / exits (per-strategy)
STRATEGY_ALERT_TITLE = {
    "bitcoin": "BTC",
    "sol_lag": "SOL lag",
    "eth_lag": "ETH lag",
    "hype_lag": "HYPE lag",
    "xrp_dump_hedge": "XRP dump-hedge",
}


def _strategy_trade_title(strategy: Optional[str]) -> str:
    if not strategy:
        return "Trade"
    return STRATEGY_ALERT_TITLE.get(strategy, "Trade")


def _discord_trade_allowed(strategy: Optional[str]) -> bool:
    return bool(strategy and strategy in DISCORD_TRADE_STRATEGIES)


class NotificationManager:
    """Manages notifications via Discord and Telegram"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config.get("notifications", {})
        self.enabled = self.config.get("enabled", True)
        self.alert_on_trade = self.config.get("alert_on_trade", True)
        self.alert_on_error = self.config.get("alert_on_error", True)
        self.alert_on_exit = self.config.get("alert_on_exit", self.alert_on_trade)
        self.alert_on_status = self.config.get("alert_on_status", False)

        # Discord
        self.discord_webhook = self.config.get("discord_webhook", "")

        # Telegram
        self.telegram_bot_token = self.config.get("telegram_bot_token", "")
        self.telegram_chat_id = self.config.get("telegram_chat_id", "")

        self.session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def send_discord(self, message: str, embed: Dict = None) -> bool:
        """Send message to Discord webhook"""
        if not self.enabled or not self.discord_webhook:
            return False

        try:
            session = await self._get_session()
            payload = {"content": message}
            if embed:
                payload["embeds"] = [embed]

            async with session.post(self.discord_webhook, json=payload) as response:
                if response.status == 204:
                    return True
                logger.error(f"Discord webhook failed: {response.status}")
                return False
        except Exception as e:
            logger.error(f"Error sending Discord notification: {e}")
            return False

    async def send_telegram(self, message: str, parse_mode: str = "Markdown") -> bool:
        """Send message to Telegram"""
        if not self.enabled or not self.telegram_bot_token or not self.telegram_chat_id:
            return False

        try:
            session = await self._get_session()
            url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
            payload = {
                "chat_id": self.telegram_chat_id,
                "text": message,
                "parse_mode": parse_mode,
            }

            async with session.post(url, json=payload) as response:
                data = await response.json()
                if data.get("ok"):
                    return True
                logger.error(f"Telegram send failed: {data}")
                return False
        except Exception as e:
            logger.error(f"Error sending Telegram notification: {e}")
            return False

    async def notify(self, message: str, channel: str = "both") -> bool:
        """Send notification to specified channel(s)"""
        if not self.enabled:
            return False

        results = []

        if channel in ["discord", "both"]:
            results.append(await self.send_discord(message))

        if channel in ["telegram", "both"]:
            results.append(await self.send_telegram(message))

        return any(results)

    async def notify_trade(self, trade_info: Dict[str, Any]) -> bool:
        """Notify about a new trade entry (crypto auto-trade strategies only)."""
        if not self.enabled or not self.discord_webhook:
            return False
        st_raw = trade_info.get("strategy")
        if not _discord_trade_allowed(st_raw):
            return False

        st = _strategy_trade_title(st_raw)
        side = trade_info.get("side", "N/A")
        size = float(trade_info.get("size") or 0)
        edge = float(trade_info.get("edge") or 0)
        price = float(trade_info.get("price") or 0)
        q = trade_info.get("question", "N/A")

        embed = {
            "title": f"{st} — entry  [{side}]",
            "color": 3447003,  # blue
            "fields": [
                {"name": "Market", "value": q[:90], "inline": False},
                {"name": "Side", "value": side, "inline": True},
                {"name": "Price", "value": f"${price:.3f}", "inline": True},
                {"name": "Size", "value": f"${size:.2f}", "inline": True},
                {"name": "Edge", "value": f"{edge:.1%}", "inline": True},
            ],
            "footer": {
                "text": f"PolyBot AI • {datetime.now().strftime('%H:%M:%S')}"
            },
        }
        return await self.send_discord(None, embed)

    async def notify_exit(self, exit_info: Dict[str, Any]) -> bool:
        """Notify about a closed position (crypto auto-trade strategies only)."""
        if not self.alert_on_exit:
            return False
        st_raw = exit_info.get("strategy")
        if not _discord_trade_allowed(st_raw):
            return False

        st = _strategy_trade_title(st_raw)
        pnl = float(exit_info.get("pnl") or 0)
        win = pnl >= 0
        reason = exit_info.get("reason", "N/A")
        q = exit_info.get("question", "N/A")
        entry_price = float(exit_info.get("entry_price") or 0)
        size = float(exit_info.get("size") or 0)
        pnl_pct = ""
        if entry_price > 0 and size > 0:
            pnl_pct = f" ({100 * pnl / (size * entry_price):+.0f}%)"

        result_text = "WIN ✅" if win else "LOSS ❌"
        result_color = "✅ WIN" if win else "❌ LOSS"

        embed = {
            "title": f"{st} — closed  [{pnl:+.2f}]",
            "color": 65280 if win else 16711680,
            "fields": [
                {"name": "Market", "value": q[:90], "inline": True},
                {
                    "name": "Entry→Exit",
                    "value": f"${entry_price:.2f}  →  ${exit_info.get('price', 0):.2f}",
                    "inline": True,
                },
                {
                    "name": "PnL",
                    "value": f"${pnl:+.2f}{pnl_pct}",
                    "inline": True,
                },
                {"name": "Result", "value": result_text, "inline": True},
                {"name": "Reason", "value": str(reason)[:200], "inline": False},
                {
                    "name": "Exited",
                    "value": f"{exit_info.get('side', '')} @ ${exit_info.get('price', 0):.2f}",
                    "inline": True,
                },
            ],
            "footer": {
                "text": f"PolyBot AI • {datetime.now().strftime('%H:%M:%S')}"
            },
        }

        return await self.send_discord(None, embed)

    async def notify_kill_global(self, strategy: str, reason: str = "global kill switch") -> bool:
        """Notify when global kill switch halts a strategy."""
        if not self.enabled or not self.discord_webhook:
            return False
        st = _strategy_trade_title(strategy)
        embed = {
            "title": f"⛔ {st} — KILLED",
            "color": 16711680,
            "fields": [
                {"name": "Strategy", "value": st, "inline": True},
                {"name": "Trigger", "value": reason, "inline": True},
                {
                    "name": "Action",
                    "value": "all trades suspended for this lane",
                    "inline": False,
                },
            ],
            "footer": {
                "text": f"PolyBot AI • {datetime.now().strftime('%H:%M:%S')}"
            },
        }
        return await self.send_discord(None, embed)

    async def notify_kill_lane(self, lane: str, reason: str, streak: int) -> bool:
        """Notify when a lane is paused due to consecutive losses."""
        if not self.enabled or not self.discord_webhook:
            return False
        lane_upper = lane.upper()
        embed = {
            "title": f"⚠️ {lane_upper} — LANE PAUSED",
            "color": 16776960,
            "fields": [
                {"name": "Lane", "value": lane_upper, "inline": True},
                {"name": "Streak", "value": f"{streak} consecutive losses", "inline": True},
                {"name": "Reason", "value": reason, "inline": True},
                {"name": "Status", "value": "paused (kill switch)", "inline": False},
            ],
            "footer": {
                "text": f"PolyBot AI • {datetime.now().strftime('%H:%M:%S')}"
            },
        }
        return await self.send_discord(None, embed)

    async def notify_error(self, error_msg: str) -> bool:
        """Notify about error"""
        if not self.alert_on_error:
            return False

        message = f"\U000026a0\ufe0f ERROR: {error_msg}"

        embed = {
            "title": "Bot Error",
            "color": 16711680,
            "description": error_msg,
            "footer": {
                "text": f"PolyBot AI • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            },
        }

        return await self.send_discord(message, embed)

    async def notify_status(self, status_info: Dict[str, Any]) -> bool:
        """Notify about bot status"""
        if not self.alert_on_status:
            return False

        run = "\U0001f7e2 Running" if status_info.get("running") else "\U0001f534 Stopped"
        message = f"""
\U0001f4ca BOT STATUS UPDATE

Positions: {status_info.get('positions', 0)}
Daily PnL: ${status_info.get('daily_pnl', 0):.2f}
Trades Today: {status_info.get('trades_today', 0)}
Status: {run}
"""

        return await self.notify(message)
