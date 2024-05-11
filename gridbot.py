"""ADD MODULE DOCSTRING"""

from requests.exceptions import ConnectionError
from http.client import RemoteDisconnected
import os
from statistics import mean
from time import sleep, time
from typing import Dict, List

import eth_account
from dotenv import load_dotenv
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from grid import Grid
from logger_config import LoggerConfig

logger_config = LoggerConfig()
log = logger_config.get_logger()

load_dotenv()

class GridBot():
    """ADD CLASS DOCSTRING"""

    def __init__(self):  # add remaining type annotations
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
        self.market = os.getenv("MARKET")  # invalid markets will be rejected by the HL api.
        self.max_leverage = float(os.getenv("MAXIMUM_LEVERAGE"))
        self.size_grid_interval = float(os.getenv("SIZE_GRID_INTERVAL"))  # percentage of price as decimal
        self.num_grid_intervals = int(os.getenv("NUM_GRID_INTERVALS"))  # on each side of the midline
        self.unit_size = float(os.getenv("UNIT_SIZE"))  # number of coins for each order
        self.gridline_to_order: Dict[int, List[int, float, int, float]] = {}  # tracks open order ids and fills at each gridline
        for key in range(self.num_grid_intervals*2+1):
            self.gridline_to_order[key] = [0, 0, 0, 0]  # map each gridline to its order id and filled size for both buy and sell orders. [buy oid, buy sz filled, sell oid, sell sz filled]
        self.order_id_to_gridline: Dict[int, int] = {}
        self.closing_order_to_opening_order: Dict[int, int] = {}  # track for example a short limit being opened in response to a long limit being filled
        self.seen_fill_hashes = set()
        self.grid = None
        self.start_time = time()
        self.epochs = 0

    def calculate_sma(self) -> float:
        """Calculates the current value for the 50 hour simple moving average"""
        end_time = self.get_current_time()
        start_time = end_time - 50*60*60*1000
        candles = self.safe_external_call(self.info.candles_snapshot, self.market, '1h', start_time, end_time)
        candle_closes = [float(candle['c']) for candle in candles]
        return mean(candle_closes)

    def open_limit_order(self, gridline: int, is_buy: bool, size: float, limit_price: float) -> int:
        """Opens a new limit order"""
        order_result = self.safe_external_call(self.exchange.order,self.market, is_buy, size, limit_price, {"limit": {"tif": "Gtc"}})
        try:
            order_id = order_result["response"]["data"]["statuses"][0]["resting"]["oid"]
            log.info(f"{size} {self.market} {'buy' if is_buy else 'sell'} order placed at {limit_price}.")
        except KeyError:
            try:  # if the order was market filled upon placement
                order_id = order_result["response"]["data"]["statuses"][0]["filled"]["oid"]
                log.info(f"{size} {self.market} {'buy' if is_buy else 'sell'} order market filled at at {limit_price}.")
            except Exception as e:
                log.warning(f"Failed to place order: {order_result} due to exception: {e}")
                return -1 #should probably do something with this return
        self.order_id_to_gridline[order_id] = gridline
        return order_id

    def close_limit_order(self, order_id: int) -> bool:
        """Closes limit order with specified order id"""
        cancel_result = self.safe_external_call(self.exchange.cancel, self.market, order_id)
        return cancel_result["status"] == "ok"  #either do something with this return or delete it

    def cancel_all_orders(self):
        """Cancels all open limit orders"""
        open_orders = self.safe_external_call(self.info.open_orders, self.exchange.account_address)
        for order in open_orders:
            self.close_limit_order(order["oid"])
            self.order_id_to_gridline.pop(order["oid"], None)  # do i need to keep this entry if the order has been filled? Well, I guess it wouldn't show up here if it was...
            # I also need to remove orders from gridline_to_order_id, right? - I don't think I do, it updates them on a new order being placed.

    def reset_grid(self, sma_price: float):
        """Adjusts grid based on current sma location"""
        log.info("\nResetting grid...\n")
        self.cancel_all_orders()
        # self.check_fills()  # do I need this in here since it's also being called in run()? I don't think I do
        self.grid = Grid(sma_price, self.size_grid_interval, self.num_grid_intervals)
        current_price = self.get_current_price()
        for i, gridline in enumerate(self.grid.lines):
            # I need to change the below if statement now that fills are separated into buy and sell
            if self.gridline_to_order[i][1] != self.unit_size or self.gridline_to_order[i][3] != self.unit_size:  # if not fully filled
                #the above needs to be adjusted to allow for closing order replacement (and its currently only checking for buy order fills)
                #so now i did allow for that but I'm wondering if the check is even needed at all atp - when would it ever be false?
                #something does need to be there because it needs to not replace filled opening orders whose closing orders havent been filled
                #maybe move the check down here and do like if is_buy and above gridline or vice versa(i.e. is an opening order), then don't place an order if it's filled
                #but still makes sure that the closing order replacement below isn't nullified when it shouldn't be
                is_buy = gridline <= sma_price
                # these are needed to carry forward closing orders when the grid is reset
                is_closing_long = False # i.e. is it a closing long order matching an opened short?
                is_closing_short = False # i.e. is it a closing short order matching an opened long?

                # if gridline is above the sma and a short was filled at the gridline above it, set a buy order
                if i < len(self.grid.lines) - 1 and gridline >= sma_price and self.gridline_to_order[i+1][3] > 0:  # same as below, I think I can just delete this /// No you can't delete it because you need to make sure the fill response orders are kept around after all orders are cancelled And along those lines I think you need to include something in this function to update the closing_order_to_opening_order dictionary where necessary.
                    is_buy = True
                    is_closing_long = True
                # if gridline is below the sma and a buy was filled at the gridline below it, set a sell order
                elif i > 0 and gridline <= sma_price and self.gridline_to_order[i-1][1] > 0:  # this needs to be changed with the way I've got this handled in check_fills. I don't think I even need to worry about fills ehre anymore.
                    is_buy = False
                    is_closing_short = True
                # if the gridline is between price and sma and no adjacent order filled, do nothing
                elif current_price > gridline > sma_price or sma_price > gridline > current_price:
                    continue

                #if an opening order has been filled
                if is_buy and not is_closing_long and self.gridline_to_order[i][1] == self.unit_size:
                    continue

                order_id = self.open_limit_order(
                    i,
                    is_buy,
                    self.unit_size - self.gridline_to_order[i][1 if is_buy else 3],
                    gridline
                )
                self.gridline_to_order[i][0 if is_buy else 2] = order_id
                if is_closing_long:
                    self.closing_order_to_opening_order[order_id] = self.gridline_to_order[i+1][2]
                if is_closing_short:
                    self.closing_order_to_opening_order[order_id] = self.gridline_to_order[i-1][0]
        # DEBUGGING
        log.info(f"\n Gridline to order: {self.gridline_to_order} \n")
        log.info(f"order id to gridline: {self.order_id_to_gridline} \n")
        log.info(f"closing order to opening: {self.closing_order_to_opening_order} \n")

    def check_fills(self):
        """Match fills to previously open orders"""
        fills = self.safe_external_call(self.info.user_fills, self.exchange.account_address)[:self.num_grid_intervals] #It might be most recent fills at the START of the array here (im pretty sure it is)  # need to account for if there arent enough entries in the list for this slice
        order_ids = [fill["oid"] for fill in fills]
        active_fill_ids = set(list(set(order_ids) & set(self.order_id_to_gridline.keys())))
        # DEBUGGING
        # print(f"closing to opening order values: {self.closing_order_to_opening_order.values()}") # I don't think this is gonna ever populate with the added check below
        active_fills = [fill for fill in fills if fill["oid"] in active_fill_ids and fill["hash"] not in self.seen_fill_hashes]  # need to have this also filter for orders who don't have active closing orders already. But be careful because in the case of partial fills you'd still want to add to the respective order even if a closing/opening order exists. I might ahve to stop re-upping orders if I can't figure this out better. Cus then i could just have a set of seen fill order ids.actually it's probably creating a new order id when i re-up, right? I'd have to check that. cus then it'd be fine.   
        # DEBUGGING
        # print(fills)
        log.info(f"As of {time()}, these are the fills: {active_fills}")
        # print(f"and just cus, I'm printing the # of fills: {len(fills)} and the active fills: {active_fill_ids}")
        # print(f"and my order_ids: {order_ids} and gridline order ids: {self.order_id_to_gridline.keys()}")
        for fill in active_fills:
            self.seen_fill_hashes.add(fill["hash"])
            log.info(f"Order filled at ${fill['px']} for {fill['sz']} {self.market}!")
            if fill['dir'] == 'Open Long':
                self.gridline_to_order[self.order_id_to_gridline[fill['oid']]][1] += float(fill['sz'])  # but this wouldn't use the order  id of this fill, right? it'd be for the corresponding long fill above? Do I need a new mapping to match closing orders with their corresponding opening orders to handle this? Or do I even need to track that? I guess I do since how else would it know to set a new order where one has been filled but then closed already in the session
                # use gridline + 1 since you're setting the closing order as a sell on the gridline above
                closing_order_id = self.open_limit_order(
                                            self.order_id_to_gridline[fill["oid"]] + 1,
                                            False,
                                            float(fill['sz']),
                                            self.grid.lines[self.order_id_to_gridline[fill["oid"]] + 1]
                                        )
                self.closing_order_to_opening_order[closing_order_id] = fill['oid']
            elif fill['dir'] == 'Close Long':
                corresponding_opening_order_gridline = self.order_id_to_gridline[self.closing_order_to_opening_order[fill["oid"]]]
                self.gridline_to_order[corresponding_opening_order_gridline][1] -= float(fill['sz'])
                # re-up corresponding opening order
                self.open_limit_order(
                    corresponding_opening_order_gridline,
                    True,
                    float(fill["sz"]),
                    self.grid.lines[corresponding_opening_order_gridline]
                )
                # do I need to track the closing order fill? Right now I'm only reducing the tracked fill on the open order
            elif fill['dir'] == 'Open Short':
                self.gridline_to_order[self.order_id_to_gridline[fill['oid']]][3] += float(fill['sz'])
                # use gridline - 1 since you're setting the closing order as a buy on the gridline below
                closing_order_id = self.open_limit_order(
                                            self.order_id_to_gridline[fill["oid"]] - 1,
                                            True,
                                            float(fill['sz']),
                                            self.grid.lines[self.order_id_to_gridline[fill["oid"]] - 1]
                                        )
                self.closing_order_to_opening_order[closing_order_id] = fill['oid']
            elif fill['dir'] == 'Close Short':
                corresponding_opening_order_gridline = self.order_id_to_gridline[self.closing_order_to_opening_order[fill["oid"]]]
                self.gridline_to_order[corresponding_opening_order_gridline][3] -= float(fill['sz'])
                # re-up corresponding opening order
                self.open_limit_order(
                    corresponding_opening_order_gridline,
                    True,
                    float(fill["sz"]),
                    self.grid.lines[corresponding_opening_order_gridline]
                )

    def get_current_price(self) -> float:
        """Returns the midpoint between current bid and ask prices"""
        return float(self.safe_external_call(self.info.all_mids)[self.market])

    @staticmethod
    def get_current_time() -> int:
        """returns current unix timestamp in milliseconds"""
        return int(time()*1000 // 1)

    def run(self):
        """Main function loop"""
        while True:
            # calculate new sma and reset grid hourly
            if (time() - self.start_time)/3600 > self.epochs:
                sma = self.calculate_sma()
                self.reset_grid(sma)
                self.epochs += 1
            # check for fills
            self.check_fills()
            sleep(15)

    def close(self):
        """Ends the bot's current session"""
        log.info("Winding down all open orders and positions...")
        self.cancel_all_orders()
        self.safe_external_call(self.exchange.market_close, self.market)

    def reestablish_connection(self):
        """Reconnect to hyperliquid"""
        log.info("Connection to hyperliquid lost. Re-establishing connection...")
        self.exchange = Exchange(
            self.agent,
            base_url=constants.MAINNET_API_URL if not self.test_run else constants.TESTNET_API_URL,
            account_address=os.getenv("ACCOUNT_ADDRESS"),
        )
        self.info = Info(constants.MAINNET_API_URL if not self.test_run else constants.TESTNET_API_URL, skip_ws=True)
        log.info("Successfully reconnected.")

    def safe_external_call(self, function, *args, **kwargs):
        """Handles connection errors for all Hyperliquid api calls"""

        attempt = 0
        while attempt < 3:
            try:
                return function(*args, **kwargs)  
            except (RemoteDisconnected, ConnectionError) as e:
                attempt += 1
                self.reestablish_connection()
                log.warning(f"Network error: {e}. Retrying ({attempt}/{3}) in 5 seconds...")
                sleep(5)
            except Exception as e:
                log.warning(f"Unexpected error {e}")
                raise
        raise Exception(f"Failed to complete external call after 3 retries.")


if __name__ == "__main__":
    bot = GridBot()
    while True:
        try:
            bot.run()
        except (RemoteDisconnected, ConnectionError):#probably don't even need this except with safe_external call
            bot.reestablish_connection()
        except KeyboardInterrupt:
            # might put a try/except here to avoid that coroutine error can just have pass in the except since it's doing evrerything it needs to do on close
            bot.close()
            break
