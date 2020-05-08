# This file is part of Maker Keeper Framework.
#
# Copyright (C) 2018 ith-harvey, grandizzy
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import logging
import sys
from typing import List
from math import log10

from market_maker_keeper.band import Bands, NewOrder
from market_maker_keeper.control_feed import create_control_feed
from market_maker_keeper.limit import History
from market_maker_keeper.order_book import OrderBookManager
from market_maker_keeper.order_history_reporter import create_order_history_reporter
from market_maker_keeper.price_feed import PriceFeedFactory
from market_maker_keeper.reloadable_config import ReloadableConfig
from market_maker_keeper.spread_feed import create_spread_feed
from market_maker_keeper.util import setup_logging
from pymaker.lifecycle import Lifecycle
from pymaker.numeric import Wad
from pyexchange.coinbase import CoinbaseApi, Order


class CoinbaseMarketMakerKeeper:
    """Keeper acting as a market maker on coinbase."""

    logger = logging.getLogger()

    def __init__(self, args: list):
        parser = argparse.ArgumentParser(prog='coinbase-market-maker-keeper')

        parser.add_argument("--coinbase-api-server", type=str, default="https://api.pro.coinbase.com",
                            help="Address of the coinbase API server (default: 'https://api.pro.coinbase.com')")

        parser.add_argument("--coinbase-api-key", type=str, required=True,
                            help="API key for the coinbase API")

        parser.add_argument("--coinbase-secret-key", type=str, required=True,
                            help="Secret key for the coinbase API")

        parser.add_argument("--coinbase-password", type=str, required=True,
                            help="Secret key for the coinbase API")

        parser.add_argument("--coinbase-timeout", type=float, default=9.5,
                            help="Timeout for accessing the coinbase API (in seconds, default: 9.5)")
        parser.add_argument("--pair", type=str, required=True,
                            help="Token pair (sell/buy) on which the keeper will operate")

        parser.add_argument("--config", type=str, required=True,
                            help="Bands configuration file")

        parser.add_argument("--price-feed", type=str, required=True,
                            help="Source of price feed")

        parser.add_argument("--price-feed-expiry", type=int, default=120,
                            help="Maximum age of the price feed (in seconds, default: 120)")

        parser.add_argument("--spread-feed", type=str,
                            help="Source of spread feed")

        parser.add_argument("--spread-feed-expiry", type=int, default=3600,
                            help="Maximum age of the spread feed (in seconds, default: 3600)")

        parser.add_argument("--control-feed", type=str,
                            help="Source of control feed")

        parser.add_argument("--control-feed-expiry", type=int, default=86400,
                            help="Maximum age of the control feed (in seconds, default: 86400)")

        parser.add_argument("--order-history", type=str,
                            help="Endpoint to report active orders to")

        parser.add_argument("--order-history-every", type=int, default=30,
                            help="Frequency of reporting active orders (in seconds, default: 30)")

        parser.add_argument("--refresh-frequency", type=int, default=3,
                            help="Order book refresh frequency (in seconds, default: 3)")

        parser.add_argument("--debug", dest='debug', action='store_true',
                            help="Enable debug output")

        parser.add_argument("--dry-run", dest='dry_run', action='store_true',
                            help="Enable dry run")

        self.arguments = parser.parse_args(args)
        setup_logging(self.arguments)

        self.dry_run = self.arguments.dry_run
        self.bands_config = ReloadableConfig(self.arguments.config)
        self.price_feed = PriceFeedFactory().create_price_feed(self.arguments)
        self.spread_feed = create_spread_feed(self.arguments)
        self.control_feed = create_control_feed(self.arguments)
        self.order_history_reporter = create_order_history_reporter(self.arguments)

        self.history = History()

        self.coinbase_api = CoinbaseApi(api_server=self.arguments.coinbase_api_server,
                                        api_key=self.arguments.coinbase_api_key,
                                        secret_key=self.arguments.coinbase_secret_key,
                                        password=self.arguments.coinbase_password,
                                        timeout=self.arguments.coinbase_timeout)

        self.order_book_manager = OrderBookManager(refresh_frequency=self.arguments.refresh_frequency)
        self.order_book_manager.get_orders_with(lambda: self.coinbase_api.get_orders(self.pair()))
        self.order_book_manager.get_balances_with(lambda: self.coinbase_api.get_balances())
        self.order_book_manager.cancel_orders_with(lambda order: self.coinbase_api.cancel_order(order.order_id))
        self.order_book_manager.enable_history_reporting(self.order_history_reporter, self.our_buy_orders,
                                                         self.our_sell_orders)
        self.order_book_manager.start()

    def main(self):
        with Lifecycle() as lifecycle:
            lifecycle.initial_delay(10)
            lifecycle.on_startup(self.startup)
            lifecycle.every(1, self.synchronize_orders)
            lifecycle.on_shutdown(self.shutdown)

    def startup(self):
        # Get maximum number of decimals for prices and amounts.
        quote_increment = self.coinbase_api.get_product(self.arguments.pair)["quote_increment"]
        self.logger.debug(f"Quote increment: {quote_increment}")
        self.quote_increment = max(2, -(int(log10(float(quote_increment)))))
        self.logger.debug(f"Quote precision: {self.quote_increment}")

        currencies = self.coinbase_api._http_unauthenticated("GET", f"/currencies", {})
        b = self.arguments.pair.split("-")[0]
        q = self.arguments.pair.split("-")[1]
        for c in currencies:
            if c["id"] == b:
                self.buy_precision = max(0, -(int(log10(float(c["min_size"])))))
                self.logger.debug(f"Buy precision: {self.buy_precision}")
                self.logger.debug(f"Buy precision: {c}")
            if c["id"] == q:
                self.sell_precision = max(0, -(int(log10(float(c["min_size"])))))
                self.logger.debug(f"Sell precision: {self.sell_precision}")
                self.logger.debug(f"Sell precision: {c}")

    def shutdown(self):
        self.order_book_manager.cancel_all_orders()

    def pair(self):
        return self.arguments.pair.upper()

    def token_sell(self) -> str:
        return self.arguments.pair.split('-')[0].upper()

    def token_buy(self) -> str:
        return self.arguments.pair.split('-')[1].upper()

    def our_available_balance(self, our_balances: dict, token: str) -> Wad:
        token_balances = list(filter(lambda coin: coin['currency'].upper() == token, our_balances))
        if token_balances:
            return Wad.from_number(token_balances[0]['available'])
        else:
            return Wad(0)

    def our_sell_orders(self, our_orders: list) -> list:
        return list(filter(lambda order: order.is_sell, our_orders))

    def our_buy_orders(self, our_orders: list) -> list:
        return list(filter(lambda order: not order.is_sell, our_orders))

    def synchronize_orders(self):
        bands = Bands.read(self.bands_config, self.spread_feed, self.control_feed, self.history)

        order_book = self.order_book_manager.get_order_book()
        target_price = self.price_feed.get_price()
        # Cancel orders
        cancellable_orders = bands.cancellable_orders(our_buy_orders=self.our_buy_orders(order_book.orders),
                                                      our_sell_orders=self.our_sell_orders(order_book.orders),
                                                      target_price=target_price)
        if len(cancellable_orders) > 0:
            self.order_book_manager.cancel_orders(cancellable_orders)
            return

        # Do not place new orders if order book state is not confirmed
        if order_book.orders_being_placed or order_book.orders_being_cancelled:
            self.logger.debug("Order book is in progress, not placing new orders")
            return

        # Place new orders
        new_orders = bands.new_orders(our_buy_orders=self.our_buy_orders(order_book.orders),
                                      our_sell_orders=self.our_sell_orders(order_book.orders),
                                      our_buy_balance=self.our_available_balance(order_book.balances, self.token_buy()),
                                      our_sell_balance=self.our_available_balance(order_book.balances, self.token_sell()),
                                      target_price=target_price)[0]

        self.place_orders(new_orders)

    def place_orders(self, new_orders: List[NewOrder]):
        def place_order_function(new_order_to_be_placed):
            price = round(new_order_to_be_placed.price, self.quote_increment)
            amount = round(new_order_to_be_placed.pay_amount, min(self.quote_increment, self.sell_precision)) if new_order_to_be_placed.is_sell else round(new_order_to_be_placed.buy_amount, min(self.quote_increment, self.buy_precision))

            if not self.dry_run:
                order_id = self.coinbase_api.place_order(self.pair(), new_order_to_be_placed.is_sell, price, amount)
            else:
                self.logger.info(f"Dry run order amount: {amount} price: {price}")

            return Order(order_id=order_id,
                         pair=self.pair(),
                         is_sell=new_order_to_be_placed.is_sell,
                         price=price,
                         amount=amount)

        for new_order in new_orders:
            self.order_book_manager.place_order(lambda new_order=new_order: place_order_function(new_order))


if __name__ == '__main__':
    CoinbaseMarketMakerKeeper(sys.argv[1:]).main()
