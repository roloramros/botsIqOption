#!/usr/bin/env python3
"""Seguimiento simple de portafolio cripto con costo promedio y profit en tiempo real.

Monedas soportadas: BTC, LTC, ETH, BNB, SOL.
Usa la API gratuita de CoinGecko para leer precios de mercado.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List
from urllib.error import URLError
from urllib.request import urlopen

SUPPORTED_ASSETS = {
    "BTC": "bitcoin",
    "LTC": "litecoin",
    "ETH": "ethereum",
    "BNB": "binancecoin",
    "SOL": "solana",
}

def parse_number(value: str) -> float:
    """Convierte números con punto o coma decimal a float."""
    normalized = value.strip().replace(",", ".")
    try:
        return float(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Valor numérico inválido: {value}") from exc



@dataclass
class Purchase:
    asset: str
    amount: float
    usdt_spent: float
    price_usd: float
    timestamp: str

    @classmethod
    def from_dict(cls, item: Dict[str, float | str]) -> "Purchase":
        amount = float(item["amount"])

        if "usdt_spent" in item:
            usdt_spent = float(item["usdt_spent"])
            price_usd = float(item["price_usd"])
        else:
            # Compatibilidad con registros previos (sin campo usdt_spent)
            price_usd = float(item["price_usd"])
            usdt_spent = amount * price_usd

        return cls(
            asset=str(item["asset"]),
            amount=amount,
            usdt_spent=usdt_spent,
            price_usd=price_usd,
            timestamp=str(item["timestamp"]),
        )


class Portfolio:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.purchases: List[Purchase] = []
        self._load()

    def _load(self) -> None:
        if not self.db_path.exists():
            return

        content = self.db_path.read_text(encoding="utf-8").strip()
        if not content:
            self.purchases = []
            return

        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            backup_path = self.db_path.with_suffix(self.db_path.suffix + ".bak")
            backup_path.write_text(content, encoding="utf-8")
            self.purchases = []
            self._save()
            return

        self.purchases = [Purchase.from_dict(item) for item in raw]

    def _save(self) -> None:
        serializable = [asdict(p) for p in self.purchases]
        self.db_path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")

    def add_purchase(
        self,
        asset: str,
        amount: float,
        usdt_spent: float,
    ) -> Purchase:
        total_cost_usd = usdt_spent
        price_usd = total_cost_usd / amount

        purchase = Purchase(
            asset=asset,
            amount=amount,
            usdt_spent=usdt_spent,
            price_usd=price_usd,
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        )
        self.purchases.append(purchase)
        self._save()
        return purchase

    def grouped_positions(self) -> Dict[str, Dict[str, float]]:
        grouped: Dict[str, Dict[str, float]] = {
            symbol: {"amount": 0.0, "cost": 0.0} for symbol in SUPPORTED_ASSETS
        }
        for purchase in self.purchases:
            bucket = grouped[purchase.asset]
            bucket["amount"] += purchase.amount
            bucket["cost"] += purchase.amount * purchase.price_usd

        for data in grouped.values():
            data["avg_buy_price"] = data["cost"] / data["amount"] if data["amount"] > 0 else 0.0
        return grouped


def fetch_market_prices_usd() -> Dict[str, float]:
    ids = ",".join(SUPPORTED_ASSETS.values())
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"
    try:
        with urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except URLError as exc:
        raise RuntimeError(f"No se pudo consultar CoinGecko: {exc}") from exc

    result: Dict[str, float] = {}
    for symbol, coin_id in SUPPORTED_ASSETS.items():
        price = payload.get(coin_id, {}).get("usd")
        if price is None:
            raise RuntimeError(f"La API no devolvió precio para {symbol}.")
        result[symbol] = float(price)
    return result


def load_prices_from_file(path: Path) -> Dict[str, float]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    result: Dict[str, float] = {}
    for symbol in SUPPORTED_ASSETS:
        if symbol not in payload:
            raise RuntimeError(f"Falta el precio de {symbol} en {path}.")
        result[symbol] = float(payload[symbol])
    return result


def print_summary(portfolio: Portfolio, prices_override: Dict[str, float] | None = None) -> None:
    grouped = portfolio.grouped_positions()
    market = prices_override or fetch_market_prices_usd()

    print("\n=== Resumen del portafolio (USD) ===")
    header = (
        f"{'Asset':<6} {'Cantidad':>12} {'Promedio Compra':>18} "
        f"{'Precio Mercado':>16} {'Valor Actual':>14} {'Profit':>14} {'ROI %':>10}"
    )
    print(header)
    print("-" * len(header))

    total_cost = 0.0
    total_value = 0.0

    for symbol in SUPPORTED_ASSETS:
        data = grouped[symbol]
        amount = data["amount"]
        if amount <= 0:
            continue

        cost = data["cost"]
        avg_buy = data["avg_buy_price"]
        market_price = market[symbol]
        value = amount * market_price
        profit = value - cost
        roi = (profit / cost * 100.0) if cost else 0.0

        total_cost += cost
        total_value += value

        print(
            f"{symbol:<6} {amount:>12.6f} {avg_buy:>18.2f} {market_price:>16.2f} "
            f"{value:>14.2f} {profit:>14.2f} {roi:>9.2f}%"
        )

    total_profit = total_value - total_cost
    total_roi = (total_profit / total_cost * 100.0) if total_cost else 0.0
    print("-" * len(header))
    print(
        f"{'TOTAL':<6} {'':>12} {'':>18} {'':>16} {total_value:>14.2f} "
        f"{total_profit:>14.2f} {total_roi:>9.2f}%"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Registro de compras cripto con promedio de compra y profit en tiempo real."
    )
    parser.add_argument(
        "--db",
        default="portfolio_db.json",
        help="Ruta del archivo JSON para guardar el portafolio.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    add_cmd = subparsers.add_parser("add", help="Agregar una compra")
    add_cmd.add_argument("asset", choices=SUPPORTED_ASSETS.keys(), help="Moneda")
    add_cmd.add_argument("crypto_received", type=parse_number, help="Cuánta cripto obtuviste (acepta punto o coma decimal)")
    add_cmd.add_argument("usdt_spent", type=parse_number, help="Cuántos USDT compraste/gastaste en esa operación")

    summary_cmd = subparsers.add_parser("summary", help="Mostrar resumen con profit en vivo")
    summary_cmd.add_argument(
        "--prices-file",
        help="JSON local con precios USD por símbolo (BTC, LTC, ETH, BNB, SOL).",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    portfolio = Portfolio(Path(args.db))

    if args.command == "add":
        if args.crypto_received <= 0 or args.usdt_spent <= 0:
            raise SystemExit("La cripto obtenida y los USDT deben ser mayores que 0.")
        purchase = portfolio.add_purchase(
            args.asset,
            args.crypto_received,
            args.usdt_spent,
        )
        total_cost = purchase.usdt_spent
        print(
            f"Compra guardada: {purchase.asset} {purchase.amount} obtenidos con {purchase.usdt_spent:.4f} USDT "
            f"(costo=${total_cost:.2f}, precio promedio=${purchase.price_usd:.2f}) ({purchase.timestamp})"
        )
    elif args.command == "summary":
        prices_override = None
        if args.prices_file:
            prices_override = load_prices_from_file(Path(args.prices_file))
        print_summary(portfolio, prices_override)


if __name__ == "__main__":
    main()
