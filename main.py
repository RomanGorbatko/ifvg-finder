import argparse
import asyncio
from pprint import pprint
from typing import Dict, List, Optional
from telegram import Bot
import ccxt
import pandas as pd
import logging
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import hashlib
from pymongo import MongoClient, ASCENDING
import json

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

with open("config.json", "r") as f:
    config = json.load(f)

EXCHANGE = ccxt.binance({"enableRateLimit": True})

mongo_client = MongoClient(config["mongodb_uri"])
db = mongo_client[config["database_name"]]
collection = db[config["collection_name"]]
collection.create_index([("stable_id", ASCENDING)], unique=True)
collection.create_index([("case", ASCENDING)])
collection.create_index([("timeframe", ASCENDING)])
collection.create_index([("status", ASCENDING)])


def parse_arguments() -> argparse.Namespace:
    """Парсить аргументи командного рядка для аналізу IFVG.

    Returns:
        argparse.Namespace: Об'єкт із аргументами --symbol, --timeframe, --plot, --monitor.
    """
    parser = argparse.ArgumentParser(
        description="Аналіз IFVG для криптовалютної пари та таймфрейму."
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default=None,
        help="Базовий символ (наприклад, TRX), якщо не вказано — обробляються всі символи Binance Futures Perpetual",
    )
    parser.add_argument(
        "--timeframe",
        type=str,
        required=True,
        help="Таймфрейм (наприклад, 5m, 4h, 1d)",
        choices=["5m", "15m", "30m", "1h", "4h", "1d"],
    )
    parser.add_argument(
        "--plot",
        action="store_true",
        default=False,
        help="Увімкнути генерацію графіків (за замовчуванням вимкнено)",
    )
    parser.add_argument(
        "--monitor",
        action="store_true",
        default=False,
        help="Увімкнути режим моніторингу тестування непротестованих IFVG (за замовчуванням вимкнено)",
    )
    return parser.parse_args()


async def send_telegram_message(message: str) -> None:
    """Асинхронно відправляє повідомлення через Telegram.

    Args:
        message (str): Текст повідомлення для відправки.
    """
    bot = Bot(token=config["telegram_bot_token"])
    await bot.send_message(chat_id=config["chat_id"], text=message)


def monitor_ifvgs() -> None:
    """Моніторить непротестовані IFVG, перевіряючи їх тестування на хвилинному таймфреймі.

    Виконує перевірку для всіх символів Binance Futures Perpetual, використовуючи лише дві останні свічки (поточну та попередню хвилину).
    Оновлює статус IFVG у MongoDB і відправляє повідомлення через Telegram, якщо IFVG протестований.
    """
    symbols = get_binance_futures_symbols(EXCHANGE)
    for symbol in symbols:
        # Завантажуємо лише дві останні свічки на 1m
        df = fetch_ohlcv(EXCHANGE, symbol, "5m", limit=2)
        if df.empty:
            logging.warning(
                f"Не вдалося завантажити дані для {symbol} на 1m, пропускаємо."
            )
            continue

        # Отримуємо всі непротестовані IFVG для цього символу
        unmitigated_ifvgs = collection.find({"symbol": symbol, "status": "unmitigated"})

        for ifvg in unmitigated_ifvgs:
            ifvg_data = {
                "first_fvg": ifvg["first_fvg"],
                "second_fvg": ifvg["second_fvg"],
                "ifvg_type": ifvg["ifvg_type"] + " IFVG",
                "case": ifvg["case"],
            }
            # Перевіряємо, чи IFVG протестований на основі поточних або попередніх даних
            if is_ifvg_mitigated(df, ifvg_data):
                # Оновлюємо статус на 'mitigated'
                collection.update_one(
                    {"stable_id": ifvg["stable_id"]}, {"$set": {"status": "mitigated"}}
                )
                # Формуємо повідомлення для Telegram
                message = (
                    f"Symbol: ${symbol.split('/')[0]}\n"
                    f"Timeframe: {ifvg['timeframe']}\n"
                    f"Stable ID: {ifvg['stable_id']}\n"
                    f"Type: {ifvg['ifvg_type']} (Case: {ifvg['case']})"
                )
                # Асинхронно відправляємо повідомлення
                asyncio.run(send_telegram_message(message))
                logging.info(
                    f"IFVG з ID {ifvg['stable_id']} оновлено на 'mitigated' для {symbol}"
                )


def normalize_symbol(symbol: str) -> str:
    """Нормалізує символ, видаляючи суфікси :USDT і залишаючи лише формат BASE/USDT.

    Args:
        symbol (str): Вхідний символ (наприклад, TRX/USDT:USDT).

    Returns:
        str: Нормалізований символ (наприклад, TRX/USDT).
    """
    if ":" in symbol:
        return symbol.split(":")[0]
    return symbol


def get_binance_futures_symbols(exchange: ccxt.Exchange) -> List[str]:
    """Отримує список символів для Binance Futures Perpetual.

    Args:
        exchange (ccxt.Exchange): Об'єкт біржі для запиту.

    Returns:
        List[str]: Список нормалізованих символів (наприклад, ['BTC/USDT', 'TRX/USDT']).

    Raises:
        Exception: Якщо сталася помилка при отриманні даних.
    """
    try:
        markets = exchange.fetch_markets()
        futures_symbols = [
            normalize_symbol(market["symbol"])
            for market in markets
            if market["type"] == "swap" and market["settle"] == "USDT"
        ]
        return list(set(futures_symbols))
    except Exception as e:
        logging.error(f"Помилка отримання списку символів: {e}")
        return []


def fetch_ohlcv(
    exchange: ccxt.Exchange, symbol: str, timeframe: str, limit: int
) -> pd.DataFrame:
    """Завантажує OHLCV дані для вказаного символу та таймфрейму.

    Args:
        exchange (ccxt.Exchange): Об'єкт біржі для запиту.
        symbol (str): Нормалізований символ (наприклад, TRX/USDT).
        timeframe (str): Таймфрейм (наприклад, '4h').
        limit (int): Кількість свічок для завантаження.

    Returns:
        pd.DataFrame: DataFrame з OHLCV даними або порожній DataFrame у разі помилки.
    """
    try:
        normalized_symbol = normalize_symbol(symbol)
        ohlcv = exchange.fetch_ohlcv(normalized_symbol, timeframe, limit=limit)
        df = pd.DataFrame(
            ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(
            df["timestamp"], unit="ms", utc=True
        ).dt.tz_convert("Europe/Kyiv")
        logging.info(
            f"Завантажено {len(df)} свічок для {normalized_symbol} на {timeframe}"
        )
        return df
    except Exception as e:
        logging.error(f"Помилка завантаження даних для {normalized_symbol}: {e}")
        return pd.DataFrame()


def is_fvg_large_enough(fvg: Dict[str, float], base_price: float) -> bool:
    """Перевіряє, чи достатньо великий розрив FVG.

    Args:
        fvg (Dict[str, float]): Словник з полями 'high' і 'low' FVG.
        base_price (float): Базова ціна для розрахунку відсотка розриву.

    Returns:
        bool: True, якщо розрив більший або дорівнює min_gap_percentage, інакше False.
    """
    if base_price <= 0:
        return False
    gap_size = fvg["high"] - fvg["low"]
    gap_percentage = (gap_size / base_price) * 100
    return gap_percentage >= config["min_gap_percentage"]


def create_fvg(
    fvg_type: str,
    start_candle: pd.Series,
    end_candle: pd.Series,
    high_value: float,
    low_value: float,
) -> Dict[str, str | float]:
    """Створює словник FVG з вказаними параметрами.

    Args:
        fvg_type (str): Тип FVG ('bearish' або 'bullish').
        start_candle (pd.Series): Початкова свіча з полем 'timestamp'.
        end_candle (pd.Series): Кінцева свіча з полем 'timestamp'.
        high_value (float): Максимальна ціна FVG.
        low_value (float): Мінімальна ціна FVG.

    Returns:
        Dict[str, str | float]: Словник з даними FVG.
    """
    return {
        "type": fvg_type,
        "start": start_candle["timestamp"].isoformat(),
        "end": end_candle["timestamp"].isoformat(),
        "high": high_value,
        "low": low_value,
    }


def generate_stable_id(ifvg: Dict) -> str:
    """Генерує унікальний ID для IFVG на основі його даних.

    Args:
        ifvg (Dict): Словник з даними IFVG, що містить 'first_fvg' і 'second_fvg'.

    Returns:
        str: Унікальний хеш (MD5) для IFVG.
    """
    start_time = pd.Timestamp(ifvg["first_fvg"]["start"]).timestamp()
    high = max(ifvg["first_fvg"]["high"], ifvg["second_fvg"]["high"])
    low = min(ifvg["first_fvg"]["low"], ifvg["second_fvg"]["low"])
    data = f"{start_time}_{high}_{low}"
    return hashlib.md5(data.encode()).hexdigest()


def save_ifvg_to_mongodb(ifvg: Dict, symbol: str, timeframe: str) -> None:
    """Зберігає IFVG у MongoDB, якщо він ще не існує.

    Args:
        ifvg (Dict): Словник із даними про IFVG (first_fvg, second_fvg, ifvg_type, case, тощо).
        symbol (str): Нормалізований символ у форматі BASE/USDT (наприклад, TRX/USDT).
        timeframe (str): Таймфрейм аналізу (наприклад, '4h', '1d').
    """
    stable_id = ifvg["stable_id"]
    if collection.find_one({"stable_id": stable_id}):
        logging.info(
            f"IFVG з ID {stable_id} вже існує у MongoDB, пропускаємо збереження."
        )
        return

    ifvg_type = ifvg["ifvg_type"].replace(" IFVG", "")
    normalized_symbol = normalize_symbol(symbol)
    ifvg_data = {
        "stable_id": stable_id,
        "ifvg_type": ifvg_type,
        "case": ifvg["case"],
        "status": "unmitigated",
        "first_fvg": {
            "type": ifvg["first_fvg"]["type"],
            "start": ifvg["first_fvg"]["start"],
            "end": ifvg["first_fvg"]["end"],
            "high": ifvg["first_fvg"]["high"],
            "low": ifvg["first_fvg"]["low"],
        },
        "second_fvg": {
            "type": ifvg["second_fvg"]["type"],
            "start": ifvg["second_fvg"]["start"],
            "end": ifvg["second_fvg"]["end"],
            "high": ifvg["second_fvg"]["high"],
            "low": ifvg["second_fvg"]["low"],
        },
        "distance_to_high": ifvg["distance_to_high"],
        "distance_to_low": ifvg["distance_to_low"],
        "symbol": normalized_symbol,
        "timeframe": timeframe,
        "timestamp": pd.Timestamp.now().isoformat(),
    }
    result = collection.insert_one(ifvg_data)
    logging.info(
        f"IFVG з ID {stable_id} збережено у MongoDB із ID {result.inserted_id}"
    )


def zones_overlap(fvg1: Dict, fvg2: Dict) -> bool:
    """Перевіряє, чи перетинаються зони двох FVG.

    Args:
        fvg1 (Dict): Перший FVG з полями 'start', 'end', 'low', 'high'.
        fvg2 (Dict): Другий FVG з полями 'start', 'end', 'low', 'high'.

    Returns:
        bool: True, якщо зони перетинаються, інакше False.
    """
    return fvg1["low"] <= fvg2["high"] and fvg1["high"] >= fvg2["low"]


def is_ifvg_mitigated(df: pd.DataFrame, ifvg: Dict) -> bool:
    """Перевіряє, чи IFVG вже протестований (mitigated) на основі даних.

    Args:
        df (pd.DataFrame): DataFrame з OHLCV даними.
        ifvg (Dict): Словник з даними IFVG, що містить 'first_fvg' і 'second_fvg'.

    Returns:
        bool: True, якщо IFVG протестований, інакше False.
    """
    ifvg_end = pd.Timestamp(ifvg["second_fvg"]["end"])
    ifvg_high = max(ifvg["first_fvg"]["high"], ifvg["second_fvg"]["high"])
    ifvg_low = min(ifvg["first_fvg"]["low"], ifvg["second_fvg"]["low"])

    # Перевіряємо, чи є свічка з точним timestamp у df
    matching_indices = df.index[df["timestamp"] == ifvg_end]
    if len(matching_indices) == 0:
        # Якщо точного збігу немає, шукаємо найближчу свічку після ifvg_end
        closest_idx = df.index[df["timestamp"] >= ifvg_end].min()
        if pd.isna(closest_idx):
            return False  # Якщо немає свічок після ifvg_end, вважаємо IFVG не протестованим
        end_idx = closest_idx
    else:
        end_idx = matching_indices[0]

    if end_idx >= len(df) - 1:
        return False

    # Перевіряємо лише свічки після ifvg_end
    for i in range(int(end_idx) + 1, len(df)):
        candle = df.iloc[i]
        if (
            ifvg_low <= candle["high"] <= ifvg_high
            or ifvg_low <= candle["low"] <= ifvg_high
        ):
            return True
    return False


def calculate_distance_to_ifvg(ifvg: Dict, current_price: float) -> tuple[float, float]:
    """Розраховує відстань від поточної ціни до High і Low IFVG.

    Args:
        ifvg (Dict): Словник з даними IFVG, що містить 'first_fvg' і 'second_fvg'.
        current_price (float): Поточна ціна.

    Returns:
        tuple[float, float]: Відстань до High і Low IFVG у відсотках.
    """
    ifvg_high = max(ifvg["first_fvg"]["high"], ifvg["second_fvg"]["high"])
    ifvg_low = min(ifvg["first_fvg"]["low"], ifvg["second_fvg"]["low"])
    distance_to_high = (
        abs(current_price - ifvg_high) / current_price * 100 if current_price > 0 else 0
    )
    distance_to_low = (
        abs(current_price - ifvg_low) / current_price * 100 if current_price > 0 else 0
    )
    return distance_to_high, distance_to_low


def update_mitigated_ifvg(ifvg: Dict, symbol: str, timeframe: str) -> None:
    """Оновлює статус на 'mitigated' для протестованого IFVG у MongoDB, якщо він існує.

    Args:
        ifvg (Dict): Словник з даними про IFVG.
        symbol (str): Нормалізований символ у форматі BASE/USDT (наприклад, TRX/USDT).
        timeframe (str): Таймфрейм аналізу (наприклад, '4h', '1d').
    """
    stable_id = generate_stable_id(ifvg)
    existing_ifvg = collection.find_one({"stable_id": stable_id})
    if existing_ifvg:
        collection.update_one(
            {"stable_id": existing_ifvg["stable_id"]}, {"$set": {"status": "mitigated"}}
        )
        logging.info(
            f"Оновлено статус IFVG з ID {existing_ifvg['stable_id']} на 'mitigated' для {symbol} на {timeframe}"
        )


def process_ifvg_type(
    df: pd.DataFrame,
    case: str,
    symbol: str,
    timeframe: str,
    candles: Dict,
    is_bullish: bool,
) -> List[Dict]:
    """Обробляє тип IFVG (Bullish або Bearish) для заданого набору свічок.

    Args:
        df (pd.DataFrame): DataFrame з OHLCV даними.
        case (str): Сценарій аналізу (наприклад, 'first_case').
        symbol (str): Нормалізований символ у форматі BASE/USDT (наприклад, TRX/USDT).
        timeframe (str): Таймфрейм аналізу (наприклад, '4h', '1d').
        candles (Dict): Словник свічок ('first', 'third', 'fourth', 'sixth').
        is_bullish (bool): True для Bullish IFVG, False для Bearish IFVG.

    Returns:
        List[Dict]: Список знайдених пар FVG для IFVG.
    """
    fvg_pairs = []
    if is_bullish:
        if candles["first"]["low"] > candles["third"]["high"]:
            bearish_fvg = create_fvg(
                "bearish",
                candles["first"],
                candles["third"],
                candles["first"]["low"],
                candles["third"]["high"],
            )
            if is_fvg_large_enough(bearish_fvg, candles["first"]["close"]):
                bullish_fvg = None
                if candles["third"]["high"] < candles["fourth"]["low"]:
                    bullish_fvg = create_fvg(
                        "bullish",
                        candles["third"],
                        candles["fourth"],
                        candles["fourth"]["low"],
                        candles["third"]["high"],
                    )
                elif candles["fourth"]["high"] < candles["sixth"]["low"]:
                    bullish_fvg = create_fvg(
                        "bullish",
                        candles["fourth"],
                        candles["sixth"],
                        candles["sixth"]["low"],
                        candles["fourth"]["high"],
                    )
                if (
                    bullish_fvg
                    and is_fvg_large_enough(bullish_fvg, candles["third"]["close"])
                    and zones_overlap(bearish_fvg, bullish_fvg)
                ):
                    ifvg = {
                        "first_fvg": bearish_fvg,
                        "second_fvg": bullish_fvg,
                        "ifvg_type": "bullish IFVG",
                        "case": case,
                    }
                    handle_ifvg(df, ifvg, symbol, timeframe, fvg_pairs)
    else:
        if candles["first"]["high"] < candles["third"]["low"]:
            bullish_fvg = create_fvg(
                "bullish",
                candles["first"],
                candles["third"],
                candles["third"]["low"],
                candles["first"]["high"],
            )
            if is_fvg_large_enough(bullish_fvg, candles["first"]["close"]):
                bearish_fvg = None
                if candles["third"]["low"] > candles["fourth"]["high"]:
                    bearish_fvg = create_fvg(
                        "bearish",
                        candles["third"],
                        candles["fourth"],
                        candles["third"]["low"],
                        candles["fourth"]["high"],
                    )
                elif candles["fourth"]["low"] > candles["sixth"]["high"]:
                    bearish_fvg = create_fvg(
                        "bearish",
                        candles["fourth"],
                        candles["sixth"],
                        candles["fourth"]["low"],
                        candles["sixth"]["high"],
                    )
                if (
                    bearish_fvg
                    and is_fvg_large_enough(bearish_fvg, candles["third"]["close"])
                    and zones_overlap(bullish_fvg, bearish_fvg)
                ):
                    ifvg = {
                        "first_fvg": bullish_fvg,
                        "second_fvg": bearish_fvg,
                        "ifvg_type": "bearish IFVG",
                        "case": case,
                    }
                    handle_ifvg(df, ifvg, symbol, timeframe, fvg_pairs)
    return fvg_pairs


def handle_ifvg(
    df: pd.DataFrame, ifvg: Dict, symbol: str, timeframe: str, fvg_pairs: List[Dict]
) -> None:
    """Обробляє IFVG, додаючи його до списку, якщо не протестований, або оновлюючи статус, якщо протестований.

    Args:
        df (pd.DataFrame): DataFrame з OHLCV даними.
        ifvg (Dict): Словник з даними IFVG.
        symbol (str): Нормалізований символ у форматі BASE/USDT (наприклад, TRX/USDT).
        timeframe (str): Таймфрейм аналізу (наприклад, '4h', '1d').
        fvg_pairs (List[Dict]): Список пар FVG для додавання непротестованих IFVG.
    """
    if not is_ifvg_mitigated(df, ifvg):
        current_price = df["close"].iloc[-1]
        distance_to_high, distance_to_low = calculate_distance_to_ifvg(
            ifvg, current_price
        )
        stable_id = generate_stable_id(ifvg)
        ifvg_data = {
            "first_fvg": ifvg["first_fvg"],
            "second_fvg": ifvg["second_fvg"],
            "ifvg_type": ifvg["ifvg_type"],
            "case": ifvg["case"],
            "distance_to_high": distance_to_high,
            "distance_to_low": distance_to_low,
            "stable_id": stable_id,
        }
        fvg_pairs.append(ifvg_data)
        save_ifvg_to_mongodb(ifvg_data, symbol, timeframe)
    else:
        update_mitigated_ifvg(ifvg, symbol, timeframe)


def find_fvg_pairs_for_case(
    df: pd.DataFrame, case: str, symbol: str, timeframe: str
) -> List[Dict]:
    """Знаходить пари FVG для вказаного сценарію та даних.

    Args:
        df (pd.DataFrame): DataFrame з OHLCV даними.
        case (str): Сценарій аналізу (наприклад, 'first_case').
        symbol (str): Нормалізований символ у форматі BASE/USDT (наприклад, TRX/USDT).
        timeframe (str): Таймфрейм аналізу (наприклад, '4h', '1d').

    Returns:
        List[Dict]: Список знайдених пар FVG для IFVG.
    """
    if case not in config["candle_offsets"]:
        return []
    offsets = config["candle_offsets"][case]
    fvg_pairs = []
    min_candles = (
        max(abs(offsets["first_fvg_start"]), abs(offsets["second_fvg_end"])) + 1
    )

    for i in range(min_candles - 1, len(df)):
        try:
            candles = {
                "first": df.iloc[i + offsets["first_fvg_start"]],
                "third": df.iloc[i + offsets["first_fvg_end"]],
                "fourth": df.iloc[i + offsets["second_fvg_start"]],
                "sixth": df.iloc[i + offsets["second_fvg_end"]],
            }
            fvg_pairs.extend(
                process_ifvg_type(df, case, symbol, timeframe, candles, True)
            )
            fvg_pairs.extend(
                process_ifvg_type(df, case, symbol, timeframe, candles, False)
            )
        except (IndexError, Exception) as e:
            logging.error(f"Помилка для i={i} (case {case}): {e}")
    return fvg_pairs


def find_all_fvg_pairs(df: pd.DataFrame, symbol: str, timeframe: str) -> List[Dict]:
    """Знаходить усі пари FVG для вказаного символу та таймфрейму.

    Args:
        df (pd.DataFrame): DataFrame з OHLCV даними.
        symbol (str): Нормалізований символ у форматі BASE/USDT (наприклад, TRX/USDT).
        timeframe (str): Таймфрейм аналізу (наприклад, '4h', '1d').

    Returns:
        List[Dict]: Список усіх знайдених пар FVG для IFVG.
    """
    fvg_pairs = []
    for case in config["candle_offsets"]:
        pairs = find_fvg_pairs_for_case(df, case, symbol, timeframe)
        fvg_pairs.extend(pairs)
    return fvg_pairs


def plot_ifvg(
    symbol: str,
    timeframe: str,
    df: pd.DataFrame,
    fvg_pairs: List[Dict],
    filename: str = "ifvg_chart.png",
) -> None:
    """Генерує графік IFVG для вказаного символу та даних.

    Args:
        symbol (str): Базовий символ (наприклад, TRX).
        timeframe (str): Таймфрейм аналізу (наприклад, '4h', '1d').
        df (pd.DataFrame): DataFrame з OHLCV даними.
        fvg_pairs (List[Dict]): Список пар FVG для відображення.
        filename (str, optional): Ім'я файлу для збереження графіка. Defaults to "ifvg_chart.png".
    """
    if not fvg_pairs:
        return

    current_price = df["close"].iloc[-1]
    current_time = df["timestamp"].iloc[-1]
    closest_ifvg, min_distance = None, float("inf")

    for pair in fvg_pairs:
        ifvg_high = max(pair["first_fvg"]["high"], pair["second_fvg"]["high"])
        ifvg_low = min(pair["first_fvg"]["low"], pair["second_fvg"]["low"])
        mid_price = (ifvg_high + ifvg_low) / 2
        distance = abs(current_price - mid_price)
        if distance < min_distance:
            min_distance = distance
            closest_ifvg = pair

    if not closest_ifvg:
        return

    closest_start = pd.Timestamp(closest_ifvg["first_fvg"]["start"]).timestamp() * 1000
    last_time = current_time.timestamp() * 1000

    closest_high = max(
        closest_ifvg["first_fvg"]["high"], closest_ifvg["second_fvg"]["high"]
    )
    closest_low = min(
        closest_ifvg["first_fvg"]["low"], closest_ifvg["second_fvg"]["low"]
    )
    last_candle_high, last_candle_low = df["high"].iloc[-1], df["low"].iloc[-1]

    fig = make_subplots(rows=1, cols=1, shared_xaxes=True, vertical_spacing=0.1)
    fig.add_trace(
        go.Candlestick(
            x=df["timestamp"],
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name="OHLC",
        )
    )

    for pair in fvg_pairs:
        first_fvg, second_fvg = pair["first_fvg"], pair["second_fvg"]
        ifvg_high, ifvg_low = max(first_fvg["high"], second_fvg["high"]), min(
            first_fvg["low"], second_fvg["low"]
        )
        fillcolor = (
            "rgba(255, 0, 0, 0.2)"
            if pair["ifvg_type"] == "bearish IFVG"
            else "rgba(0, 255, 0, 0.2)"
        )
        line_color = (
            "rgba(255, 0, 0, 0.5)"
            if pair["ifvg_type"] == "bearish IFVG"
            else "rgba(0, 255, 0, 0.5)"
        )

        fig.add_shape(
            type="rect",
            x0=pd.Timestamp(first_fvg["start"]).timestamp() * 1000,
            x1=pd.Timestamp(second_fvg["end"]).timestamp() * 1000,
            y0=ifvg_low,
            y1=ifvg_high,
            fillcolor=fillcolor,
            line=dict(color=line_color, width=1),
            layer="below",
        )

        mid_time = (
            (
                pd.Timestamp(first_fvg["start"]).timestamp()
                + pd.Timestamp(second_fvg["end"]).timestamp()
            )
            / 2
            * 1000
        )
        fig.add_annotation(
            x=mid_time,
            y=(ifvg_high + ifvg_low) / 2,
            xref="x",
            yref="y",
            text=f"{pair['ifvg_type']} (ID: {pair['stable_id'][:8]})",
            showarrow=False,
            font=dict(size=10, color="black"),
        )

    fig.add_hline(
        y=current_price,
        line_dash="dash",
        line_color="blue",
        line_width=1,
        name="Поточна ціна",
    )
    fig.update_layout(
        title=f"IFVG для {symbol} на {timeframe}",
        yaxis_title="Ціна",
        xaxis_title="Час",
        template="plotly_dark",
        showlegend=True,
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        height=600,
        width=1200,
        xaxis=dict(range=[closest_start, last_time]),
        yaxis=dict(
            range=[
                min(closest_low, last_candle_low),
                max(closest_high, last_candle_high),
            ]
        ),
    )
    fig.write_image(filename, scale=2)
    logging.info(f"Графік збережено як {filename}")


def print_fvg_pairs(fvg_pairs: List[Dict]) -> None:
    """Виводить інформацію про знайдені пари FVG у консоль.

    Args:
        fvg_pairs (List[Dict]): Список словників з даними про пари FVG.
    """
    print(f"Знайдено {len(fvg_pairs)} потенційних IFVG (непротестованих)")
    for pair in fvg_pairs:
        first, second = pair["first_fvg"], pair["second_fvg"]
        print(
            f"\n{pair['ifvg_type']} (ID: {pair['stable_id']}, Сценарій: {pair['case']})"
        )
        print(
            f"  Перший FVG ({first['type']}): {first['start']} - {first['end']}, High: {first['high']:.6f}, Low: {first['low']:.6f}, Gap: {first['high'] - first['low']:.6f}"
        )
        print(
            f"  Другий FVG ({second['type']}): {second['start']} - {second['end']}, High: {second['high']:.6f}, Low: {second['low']:.6f}, Gap: {second['high'] - second['low']:.6f}"
        )
        print(
            f"  Відстань від поточної ціни: До High IFVG = {pair['distance_to_high']:.4f}%, До Low IFVG = {pair['distance_to_low']:.4f}%"
        )


def main() -> None:
    """Основна функція для аналізу IFVG та моніторингу тестування.

    Виконує аналіз для вказаного символу або всіх символів Binance Futures Perpetual,
    або моніторинг тестування непротестованих IFVG, залежно від аргументів командного рядка.
    """
    args = parse_arguments()
    symbol: Optional[str] = args.symbol
    timeframe: str = args.timeframe
    plot_enabled: bool = args.plot
    monitor: bool = args.monitor

    if monitor:
        if timeframe != "5m":
            logging.error("Режим моніторингу підтримує лише таймфрейм '5m'")
            return
        monitor_ifvgs()
    else:
        if timeframe == "5m":
            logging.warning(
                "Таймфрейм '5m' підтримується лише в режимі моніторингу. Використовуйте 15m, 30m, 1h, 4h або 1d для аналізу."
            )
            return

        if symbol is None:
            symbols = get_binance_futures_symbols(EXCHANGE)
            for symbol in symbols:
                base_symbol = symbol.split("/")[0]
                print(f"\nОбробка символу {base_symbol} на {timeframe}...")
                df = fetch_ohlcv(EXCHANGE, symbol, timeframe, config["limit"])
                if df.empty:
                    print(
                        f"Не вдалося завантажити дані для {base_symbol}, пропускаємо."
                    )
                    continue

                print(
                    f"Пошук пар FVG для IFVG з фільтром >{config['min_gap_percentage']}% для {base_symbol}..."
                )
                fvg_pairs = find_all_fvg_pairs(df, symbol, timeframe)

                print_fvg_pairs(fvg_pairs)
                if plot_enabled:
                    plot_ifvg(
                        base_symbol,
                        timeframe,
                        df,
                        fvg_pairs,
                        filename=f"ifvg_chart_{base_symbol}_{timeframe}.png",
                    )
        else:
            base_symbol = symbol
            full_symbol = f"{base_symbol}/USDT"
            print(f"Завантаження даних для {base_symbol} на {timeframe}...")
            df = fetch_ohlcv(EXCHANGE, full_symbol, timeframe, config["limit"])
            if df.empty:
                print("Не вдалося завантажити дані, перевірте з'єднання або параметри.")
                return

            print(
                f"Пошук пар FVG для IFVG з фільтром >{config['min_gap_percentage']}% для {base_symbol}..."
            )
            fvg_pairs = find_all_fvg_pairs(df, full_symbol, timeframe)

            print_fvg_pairs(fvg_pairs)
            if plot_enabled:
                plot_ifvg(base_symbol, timeframe, df, fvg_pairs)


if __name__ == "__main__":
    main()
