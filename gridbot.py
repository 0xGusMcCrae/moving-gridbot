import asyncio
import eth_account
import os

from dotenv import load_dotenv
from grid import Grid
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from http.client import RemoteDisconnected
from requests.exceptions import ConnectionError
from statistics import mean
from time import time

load_dotenv()

class GridBot():

    def __init__(self):
        """
        - All settings to be set in .env file
        - agent_private_key = private key given when setting up api access on hyperliquid
        - account_address = address of the wallet used to set up hyperliquid account
        - market = ticker of market to be traded
        """
        self.test_run = os.getenv("TEST_RUN") == "True"
        self.agent = eth_account.Account.from_key(os.getenv("AGENT_PRIVATE_KEY") if not self.test_run else os.getenv("TESTNET_PRIVATE_KEY"))
        self.exchange = Exchange(
            self.agent, 
            base_url=constants.MAINNET_API_URL if not self.test_run else constants.TESTNET_API_URL,
            account_address=os.getenv("ACCOUNT_ADDRESS"),
        )
        self.info = Info(constants.MAINNET_API_URL if not self.test_run else constants.TESTNET_API_URL, skip_ws=True)
        self.market = os.getenv("MARKET") # invalid markets will be rejected by the HL api.
        self.max_leverage = float(os.getenv("MAXIMUM_LEVERAGE"))
        self.size_grid_interval = float(os.getenv("SIZE_GRID_INTERVAL")) # percentage of price as decimal
        self.num_grid_intervals = int(os.getenv("NUM_GRID_INTERVALS")) #on each side of the midline
        self.unit_size = float(os.getenv("UNIT_SIZE")) #number of coins for each order

    def calculate_sma(self) -> float: #can be improved so it doesn't need to continually fetch the entire range. Keep the values on hand? Might not be necessary though. Shouldn't be that time/computation intensive. And is only calculated once per hour
        """Calculates the current value for the 50 hour simple moving average"""
        end_time = self.get_current_time()
        start_time = end_time - 50*60*60*1000 
        candles = self.info.candles_snapshot(self.market, '1h', start_time, end_time)
        candle_closes = [float(candle['c']) for candle in candles]
        return mean(candle_closes)
        
    def open_limit_order(self, is_buy: bool, size: float, limit_price: int) -> bool:
        """Opens a new limit order"""
        order_result = self.exchange.order(self.market, is_buy, size, limit_price, {"limit": {"tif": "Gtc"}})
        print(f"{size} {self.market} {'buy' if is_buy else 'sell'} order placed at {limit_price}.")
        return order_result["status"] == "ok" #probably should have it return order id instead? But I can also just query info for all my open orders
    
    def close_limit_order(self, order_id: int) -> bool:
        """Closes limit order with specified order id"""
        cancel_result = self.exchange.cancel(self.market, order_id)
        return cancel_result["status"] == "ok" #make sure this is the correct status

    def cancel_all_orders(self):
        """Cancels all open limit orders"""
        open_orders = self.info.open_orders(self.exchange.account_address)
        for order in open_orders:
            self.close_limit_order(order["oid"])
        
    def reset_grid(self, sma_price: float):
        """Adjusts grid based on current sma location"""
        print(f"Resetting grid...")
        self.cancel_all_orders()
        grid = Grid(sma_price, self.size_grid_interval, self.num_grid_intervals)
        current_price = self.get_current_price()
        for price in grid.lines:
            self.open_limit_order(True if price < current_price else False, self.unit_size, price)

    def get_current_price(self) -> float:
        """Returns the midpoint between current bid and ask prices"""
        return float(self.info.all_mids()[self.market])
    
    @staticmethod
    def get_current_time() -> int:
        """returns current unix timestamp in milliseconds"""
        return int(time()*1000 // 1) 
    
    # def check_for_filled_orders(self):
    #     """Checks for filled orders and sets matching buy/sells as needed"""
    #     pass
    
    async def run(self):
        """Main function loop"""
        while True:
            sma = self.calculate_sma()
            self.reset_grid(sma)
            await asyncio.sleep(900)
            # print(f"{self.info.user_fills(self.exchange.account_address)}")
        
    def close(self):
        """Ends the bot's current session"""
        print(f"Winding down all open orders and positions...")
        self.cancel_all_orders()
        self.exchange.market_close(self.market)
    
        

if __name__ == "__main__":
    bot = GridBot()
    while True:
        try:
            asyncio.run(bot.run())
        except (RemoteDisconnected, ConnectionError): 
            #probably make this its own function. Could also just redo the bot instantiation. It's ~the same thing and cleaner
            print("Connection to hyperliquid lost. Re-establishing connection...")
            bot.exchange = Exchange(
                bot.agent, 
                base_url=constants.MAINNET_API_URL if not bot.test_run else constants.TESTNET_API_URL,
                account_address=os.getenv("ACCOUNT_ADDRESS"),
            )
            bot.info = Info(constants.MAINNET_API_URL if not bot.test_run else constants.TESTNET_API_URL, skip_ws=True)
            print("Successfully reconnected.")
        except KeyboardInterrupt:
            asyncio.run(bot.close())
            break