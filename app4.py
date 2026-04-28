import math
import random
import re
import time
import logging
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, request, jsonify, render_template_string

# =========================================================
# PRO-QUANT Realistic Edition
# 适用定位：A股技术面观察池 + 风险收益筛选 + 半自动复盘工具
# 说明：本工具不构成投资建议，只用于个人研究和复盘。
# =========================================================

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("proquant")

# ================== 参数区 ==================
KLINE_LIMIT = 320
SCAN_LIMIT = 320
MAX_WORKERS = 16
CACHE_TTL = 1800
ROUND_TRIP_COST = 0.002  # 粗略模拟佣金、印花税、滑点等合计成本，默认0.2%

KDJ_WINDOW = 9
RSI_PERIOD = 14
BOLL_PERIOD = 20
ATR_PERIOD = 14
SUPPORT_SHORT = 20
SUPPORT_MID = 60
VOLUME_PRICE_DAYS = 60

active_cache = {"data": None, "time": 0}
market_cache = {"data": None, "time": 0}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.2 Safari/605.1.15",
]


def create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=80, pool_maxsize=80)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Referer": "https://quote.eastmoney.com/",
    })
    return session


http_session = create_session()


# ================== 通用工具 ==================
def safe_float(val, default=0.0) -> float:
    try:
        v = float(val)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def safe_date(date_str) -> str:
    s = str(date_str).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s.replace("/", "-")


def pct(a, b) -> float:
    return (safe_float(a) - safe_float(b)) / (safe_float(b) + 1e-9)


def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))


def stock_market_id(symbol: str) -> str:
    """东方财富secid市场前缀：沪市1，深市0。"""
    symbol = str(symbol).strip()
    return "1" if symbol.startswith(("5", "6", "9")) else "0"


def secid_for_stock(symbol: str) -> str:
    return f"{stock_market_id(symbol)}.{symbol}"


def secid_for_index(index_code: str) -> str:
    # 常用指数：上证1.000001，沪深300 1.000300，深成指0.399001，创业板0.399006
    if index_code.startswith("399"):
        return f"0.{index_code}"
    return f"1.{index_code}"


# ================== 股票池 ==================
def get_hardcoded_fallback_stocks():
    codes = [
        ("000001", "平安银行"), ("000002", "万科A"), ("000063", "中兴通讯"), ("000100", "TCL科技"),
        ("000538", "云南白药"), ("000725", "京东方A"), ("000858", "五粮液"), ("002230", "科大讯飞"),
        ("002304", "洋河股份"), ("002415", "海康威视"), ("002475", "立讯精密"), ("002594", "比亚迪"),
        ("002714", "牧原股份"), ("300014", "亿纬锂能"), ("300015", "爱尔眼科"), ("300059", "东方财富"),
        ("300124", "汇川技术"), ("300274", "阳光电源"), ("300750", "宁德时代"), ("600000", "浦发银行"),
        ("600030", "中信证券"), ("600036", "招商银行"), ("600104", "上汽集团"), ("600276", "恒瑞医药"),
        ("600519", "贵州茅台"), ("600900", "长江电力"), ("601012", "隆基绿能"), ("601166", "兴业银行"),
        ("601318", "中国平安"), ("601888", "中国中免"), ("603259", "药明康德"), ("688981", "中芯国际"),
    ]
    return [{"code": c, "name": n, "sector": "核心资产", "amount": 0} for c, n in codes]


def get_active_stocks(limit=SCAN_LIMIT):
    """按成交额获取活跃股票池，失败时使用核心资产兜底。"""
    now = time.time()
    if active_cache["data"] is not None and now - active_cache["time"] < CACHE_TTL:
        return active_cache["data"][:limit]

    stocks = []
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": str(limit), "po": "1", "np": "1",
            "fid": "f6",  # 成交额排序
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f14,f6,f100,f2,f3",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2", "invt": "2", "_": str(int(now * 1000)),
        }
        data = http_session.get(url, params=params, timeout=(3, 8)).json()
        for item in data.get("data", {}).get("diff", []) or []:
            code = item.get("f12")
            name = item.get("f14") or ""
            if not code or not re.match(r"^\d{6}$", code):
                continue
            if "ST" in name or "退" in name:
                continue
            stocks.append({
                "code": code,
                "name": name,
                "sector": item.get("f100", ""),
                "amount": safe_float(item.get("f6")),
            })
    except Exception as e:
        logger.warning(f"active stock fetch failed: {e}")

    if len(stocks) < 50:
        stocks = get_hardcoded_fallback_stocks()

    # 去重
    unique = list({s["code"]: s for s in stocks}.values())
    active_cache["data"] = unique
    active_cache["time"] = now
    return unique[:limit]


# ================== 股票搜索 ==================
def resolve_stock_input(keyword):
    keyword = str(keyword or "").strip()
    if re.match(r"^\d{6}$", keyword):
        return keyword, f"A股_{keyword}"

    try:
        resp = http_session.get(
            "https://searchapi.eastmoney.com/api/suggest/get",
            params={
                "input": keyword,
                "type": "14",
                "token": "D43BF722C8E33BDC906FB84D85E326E8",
                "count": "1",
            },
            timeout=(2, 5),
        )
        if resp.status_code == 200:
            data = resp.json()
            rows = data.get("QuotationCodeTable", {}).get("Data", [])
            if rows:
                item = rows[0]
                return item.get("Code"), item.get("Name")
    except Exception as e:
        logger.warning(f"resolve failed: {e}")
    return keyword, keyword


# ================== 数据获取 ==================
def fetch_eastmoney_kline(secid: str, end_date=None, limit=KLINE_LIMIT):
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",  # 前复权
        "end": end_date if end_date else "20500101",
        "lmt": str(limit),
    }
    resp = http_session.get(url, params=params, timeout=(3, 8))
    resp.raise_for_status()
    data = resp.json()
    klines = data.get("data", {}).get("klines", []) or []
    rows = []
    for item in klines:
        p = item.split(",")
        if len(p) < 11:
            continue
        rows.append({
            "date": safe_date(p[0]),
            "open": safe_float(p[1]),
            "close": safe_float(p[2]),
            "high": safe_float(p[3]),
            "low": safe_float(p[4]),
            "volume": safe_float(p[5]),
            "amount": safe_float(p[6]),
            "amplitude": safe_float(p[7]),
            "pct_chg": safe_float(p[8]),
            "turnover": safe_float(p[10]),
        })
    return pd.DataFrame(rows)


def fetch_tencent_kline(symbol: str, end_date=None, limit=KLINE_LIMIT):
    prefix = "sh" if symbol.startswith(("5", "6", "9")) else "sz"
    tx_url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{symbol},day,,,{limit},qfq"
    resp = http_session.get(tx_url, timeout=(3, 8))
    resp.raise_for_status()
    data = resp.json()
    stock_data = data.get("data", {}).get(f"{prefix}{symbol}") or {}
    kline = stock_data.get("qfqday") or stock_data.get("day") or []
    rows = []
    for i in kline:
        if len(i) < 6:
            continue
        rows.append({
            "date": safe_date(i[0]),
            "open": safe_float(i[1]),
            "close": safe_float(i[2]),
            "high": safe_float(i[3]),
            "low": safe_float(i[4]),
            "volume": safe_float(i[5]),
            "amount": 0.0,
            "amplitude": 0.0,
            "pct_chg": 0.0,
            "turnover": 0.0,
        })
    df = pd.DataFrame(rows)
    if end_date and not df.empty:
        end = safe_date(end_date)
        df = df[df["date"] <= end]
    return df


# ================== 指标计算 ==================
def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy().sort_values("date").reset_index(drop=True)
    for c in ["open", "close", "high", "low", "volume", "turnover"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    # 均线与量能
    for n in [5, 10, 20, 60]:
        df[f"MA{n}"] = df["close"].rolling(n, min_periods=1).mean()
    for n in [5, 20]:
        df[f"VMA{n}"] = df["volume"].rolling(n, min_periods=1).mean()

    # KDJ
    low_n = df["low"].rolling(KDJ_WINDOW, min_periods=1).min()
    high_n = df["high"].rolling(KDJ_WINDOW, min_periods=1).max()
    rsv = np.where(high_n - low_n == 0, 50, (df["close"] - low_n) / (high_n - low_n) * 100)
    df["K"] = pd.Series(rsv).ewm(com=2, adjust=False).mean()
    df["D"] = df["K"].ewm(com=2, adjust=False).mean()
    df["J"] = 3 * df["K"] - 2 * df["D"]

    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["DIF"] = ema12 - ema26
    df["DEA"] = df["DIF"].ewm(span=9, adjust=False).mean()
    df["MACD"] = 2 * (df["DIF"] - df["DEA"])

    # RSI
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(RSI_PERIOD, min_periods=1).mean()
    loss = (-delta.clip(upper=0)).rolling(RSI_PERIOD, min_periods=1).mean()
    df["RSI"] = 100 - 100 / (1 + gain / (loss + 1e-9))

    # BOLL
    df["BOLL_MID"] = df["close"].rolling(BOLL_PERIOD, min_periods=1).mean()
    df["BOLL_STD"] = df["close"].rolling(BOLL_PERIOD, min_periods=1).std().fillna(0)
    df["BOLL_UP"] = df["BOLL_MID"] + 2 * df["BOLL_STD"]
    df["BOLL_LOW"] = df["BOLL_MID"] - 2 * df["BOLL_STD"]

    # ATR
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(ATR_PERIOD, min_periods=1).mean()
    df["ATR_PCT"] = df["ATR"] / (df["close"] + 1e-9)

    # 支撑压力
    df["Support20"] = df["low"].rolling(SUPPORT_SHORT, min_periods=1).min()
    df["Resistance20"] = df["high"].rolling(SUPPORT_SHORT, min_periods=1).max()
    df["Support60"] = df["low"].rolling(SUPPORT_MID, min_periods=1).min()
    df["Resistance60"] = df["high"].rolling(SUPPORT_MID, min_periods=1).max()

    df = df.replace([np.inf, -np.inf], np.nan).fillna(0)
    return df


def volume_price_peak(df: pd.DataFrame, days=VOLUME_PRICE_DAYS) -> float:
    recent = df.tail(days).copy()
    if recent.empty or recent["close"].max() <= recent["close"].min():
        return safe_float(df["close"].iloc[-1]) if not df.empty else 0.0
    # 剔除极端巨量日，防止单日异常成交扭曲量价密集区
    vol_cap = recent["volume"].quantile(0.95)
    recent["volume_adj"] = recent["volume"].clip(upper=vol_cap)
    bins = np.linspace(recent["close"].min(), recent["close"].max(), 12)
    try:
        cuts = pd.cut(recent["close"], bins=bins, include_lowest=True)
        dist = recent.groupby(cuts, observed=False)["volume_adj"].sum()
    except TypeError:
        cuts = pd.cut(recent["close"], bins=bins, include_lowest=True)
        dist = recent.groupby(cuts)["volume_adj"].sum()
    if dist.empty:
        return safe_float(recent["close"].iloc[-1])
    return safe_float(dist.idxmax().mid)


def weekly_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    temp = df.copy()
    temp["date_dt"] = pd.to_datetime(temp["date"])
    w = temp.set_index("date_dt").resample("W-FRI").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()
    if w.empty:
        return pd.DataFrame()
    w = w.reset_index().rename(columns={"date_dt": "date"})
    w["date"] = w["date"].dt.strftime("%Y-%m-%d")
    return calculate_indicators(w)


# ================== 市场环境 ==================
def get_market_environment():
    now = time.time()
    if market_cache["data"] is not None and now - market_cache["time"] < CACHE_TTL:
        return market_cache["data"]

    env = {
        "score": 50,
        "status": "中性",
        "index": "沪深300",
        "details": "指数数据不足，按中性处理。",
        "discount": 1.0,
    }
    try:
        df = fetch_eastmoney_kline(secid_for_index("000300"), limit=160)
        df = calculate_indicators(df)
        if len(df) >= 60:
            latest = df.iloc[-1]
            ma20_up = latest["MA20"] > df["MA20"].iloc[-5]
            score = 40
            if latest["close"] > latest["MA20"]:
                score += 20
            if latest["close"] > latest["MA60"]:
                score += 20
            if latest["MA20"] > latest["MA60"]:
                score += 10
            if ma20_up:
                score += 10
            score = int(clamp(score))
            if score >= 75:
                status, discount = "偏强", 1.05
            elif score >= 55:
                status, discount = "中性偏强", 1.0
            elif score >= 40:
                status, discount = "震荡", 0.9
            else:
                status, discount = "偏弱", 0.75
            env = {
                "score": score,
                "status": status,
                "index": "沪深300",
                "details": f"收盘{latest['close']:.2f}，MA20:{latest['MA20']:.2f}，MA60:{latest['MA60']:.2f}，MA20斜率:{'向上' if ma20_up else '走平/向下'}。",
                "discount": discount,
            }
    except Exception as e:
        logger.warning(f"market env failed: {e}")

    market_cache["data"] = env
    market_cache["time"] = now
    return env


# ================== 信号与评分 ==================
@dataclass
class SignalResult:
    total_score: int
    state: str
    rating: str
    reasons: list
    risks: list
    buy_zone: str
    stop_loss: str
    target: str
    position: str
    risk_reward: float
    components: dict


def classify_rating(score: int):
    if score >= 82:
        return "强观察"
    if score >= 70:
        return "重点观察"
    if score >= 60:
        return "普通观察"
    if score >= 45:
        return "震荡观望"
    return "风险回避"


def position_suggestion(score: int, state: str, market_status: str):
    if score >= 82 and state in ["趋势突破", "回踩确认"] and market_status in ["偏强", "中性偏强"]:
        return "试探仓10%-20%，回踩不破再加；不建议一次性重仓。"
    if score >= 70:
        return "观察仓或小仓位试错，等待放量确认。"
    if score >= 60:
        return "只进观察池，不急于买入。"
    if score >= 45:
        return "保持观望，等待趋势重新清晰。"
    return "回避或降低仓位。"


def detect_state(df: pd.DataFrame) -> str:
    if len(df) < 80:
        return "数据不足"
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    high20_prev = df["high"].rolling(20).max().iloc[-2]
    low60 = df["low"].rolling(60).min().iloc[-1]
    high60 = df["high"].rolling(60).max().iloc[-1]
    vol_ratio = latest["volume"] / (latest["VMA5"] + 1e-9)
    vol_ratio20 = latest["volume"] / (latest["VMA20"] + 1e-9)
    ma20_up = latest["MA20"] > df["MA20"].iloc[-5]

    breakout = latest["close"] > high20_prev and vol_ratio20 > 1.25 and ma20_up and latest["close"] > latest["MA20"]
    rise_from_low = latest["close"] / (low60 + 1e-9) - 1
    near_high = latest["close"] / (high60 + 1e-9) > 0.92
    high_risk = rise_from_low > 0.35 and near_high and latest["J"] > 85 and vol_ratio > 1.15

    pullback = (
        latest["close"] > latest["MA60"]
        and latest["MA20"] >= latest["MA60"]
        and latest["close"] >= latest["MA20"] * 0.97
        and vol_ratio < 0.85
        and pct(df["high"].tail(20).max(), latest["close"]) > 0.04
    )

    reconfirm = pullback and latest["close"] > prev["close"] and vol_ratio > 1.05 and latest["K"] > latest["D"]

    weak_rebound = latest["close"] < latest["MA20"] and latest["close"] < latest["MA60"] and latest["J"] < 25

    if high_risk:
        return "高位风险"
    if breakout:
        return "趋势突破"
    if reconfirm:
        return "回踩确认"
    if pullback:
        return "缩量回踩观察"
    if weak_rebound:
        return "弱势反抽"
    return "震荡观察"


def evaluate_signal(df: pd.DataFrame, market_env=None, cost_price=None) -> SignalResult:
    if df is None or len(df) < 80:
        return SignalResult(0, "数据不足", "风险回避", ["历史数据不足。"], ["无法计算稳定指标。"], "-", "-", "-", "回避", 0.0, {})

    market_env = market_env or get_market_environment()
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    state = detect_state(df)
    close = latest["close"]
    vol_ratio5 = latest["volume"] / (latest["VMA5"] + 1e-9)
    vol_ratio20 = latest["volume"] / (latest["VMA20"] + 1e-9)
    ma20_up = latest["MA20"] > df["MA20"].iloc[-5]
    ma60_up = latest["MA60"] > df["MA60"].iloc[-10]
    macd_up = latest["MACD"] > df["MACD"].iloc[-3]
    kdj_cross = latest["K"] > latest["D"] and prev["K"] <= prev["D"]

    reasons, risks = [], []

    # 1. 市场环境 0-15
    market_score = market_env.get("score", 50) * 0.15
    reasons.append(f"市场环境：{market_env.get('status')}，{market_env.get('details')}")

    # 2. 趋势结构 0-25
    trend = 0
    if close > latest["MA20"]:
        trend += 6; reasons.append("价格站上MA20，短中期趋势不弱。")
    else:
        risks.append("价格仍在MA20下方，短线趋势偏弱。")
    if close > latest["MA60"]:
        trend += 5; reasons.append("价格站上MA60，中期趋势未破坏。")
    else:
        risks.append("价格低于MA60，中期结构需要谨慎。")
    if latest["MA5"] > latest["MA10"] > latest["MA20"]:
        trend += 5; reasons.append("MA5>MA10>MA20，短均线排列较好。")
    if latest["MA20"] > latest["MA60"]:
        trend += 4; reasons.append("MA20位于MA60上方，中期趋势有支撑。")
    if ma20_up:
        trend += 3; reasons.append("MA20斜率向上，趋势延续性较好。")
    if ma60_up:
        trend += 2
    trend = clamp(trend, 0, 25)

    # 3. 动量 0-18
    momentum = 0
    if latest["DIF"] > latest["DEA"]:
        momentum += 4; reasons.append("MACD处于多头状态。")
    if macd_up:
        momentum += 3
    if 45 <= latest["RSI"] <= 70:
        momentum += 4; reasons.append("RSI处于相对健康区间，未明显过热。")
    elif latest["RSI"] > 78:
        risks.append("RSI偏高，短线可能过热。")
    if 20 < latest["J"] < 80:
        momentum += 3
    elif latest["J"] >= 90:
        risks.append("KDJ-J值过高，追高风险上升。")
    elif latest["J"] <= 20:
        momentum += 2; reasons.append("KDJ-J值低位，存在反抽弹性，但需趋势确认。")
    if kdj_cross:
        momentum += 4; reasons.append("KDJ出现低位/中位金叉迹象。")
    momentum = clamp(momentum, 0, 18)

    # 4. 量价结构 0-20
    volume_score = 0
    high20_prev = df["high"].rolling(20).max().iloc[-2]
    breakout = close > high20_prev and vol_ratio20 > 1.25 and ma20_up
    pullback_shrink = close > latest["MA60"] and close >= latest["MA20"] * 0.97 and vol_ratio5 < 0.85
    if breakout:
        volume_score += 8; reasons.append("放量突破20日高点，存在趋势突破信号。")
    if pullback_shrink:
        volume_score += 6; reasons.append("回踩区间缩量，抛压阶段性减弱。")
    if 0.7 <= vol_ratio20 <= 1.8:
        volume_score += 3
    elif vol_ratio20 > 2.5:
        risks.append("成交量异常放大，需警惕放量滞涨或短线分歧。")
    if latest["close"] >= latest["open"] and latest["volume"] > latest["VMA5"]:
        volume_score += 3
    volume_score = clamp(volume_score, 0, 20)

    # 5. 风险收益比 0-22
    support = max(latest["Support20"], latest["MA60"] * 0.98)
    resistance = max(latest["Resistance20"], latest["Resistance60"])
    downside = max((close - support) / (close + 1e-9), 0.005)
    upside = max((resistance - close) / (close + 1e-9), 0.0)
    rr = upside / (downside + 1e-9)
    risk_reward_score = 0
    if downside <= 0.06:
        risk_reward_score += 7; reasons.append("距离关键支撑较近，止损成本相对可控。")
    elif downside > 0.12:
        risks.append("距离有效支撑较远，止损空间偏大。")
    if rr >= 2.0:
        risk_reward_score += 9; reasons.append("上方空间/下方风险比超过2，赔率较好。")
    elif rr >= 1.3:
        risk_reward_score += 5
    else:
        risks.append("风险收益比不足，当前位置不适合追买。")
    if latest["ATR_PCT"] <= 0.045:
        risk_reward_score += 3
    else:
        risks.append("ATR波动偏大，仓位需要降低。")
    if cost_price:
        vp = volume_price_peak(df)
        if cost_price <= vp * 1.02:
            risk_reward_score += 3; reasons.append("持仓成本接近或低于近60日量价密集区。")
        else:
            risks.append("持仓成本高于近60日量价密集区，解套压力需关注。")
    risk_reward_score = clamp(risk_reward_score, 0, 22)

    raw_total = market_score + trend + momentum + volume_score + risk_reward_score
    total = int(clamp(raw_total * market_env.get("discount", 1.0), 0, 100))

    # 高位风险强制扣分
    if state == "高位风险":
        total = min(total, 55)
    if close < latest["MA60"] and latest["MA20"] < latest["MA60"]:
        total = min(total, 50)

    stop = min(support, close - 1.5 * latest["ATR"])
    stop = max(stop, 0)
    buy_low = max(latest["MA20"] * 0.99, support)
    buy_high = close if state in ["趋势突破", "回踩确认"] else min(close, latest["MA20"] * 1.02)
    target = resistance if resistance > close else close * 1.06

    rating = classify_rating(total)
    pos = position_suggestion(total, state, market_env.get("status", "中性"))

    components = {
        "market": round(market_score, 1),
        "trend": round(trend, 1),
        "momentum": round(momentum, 1),
        "volume": round(volume_score, 1),
        "risk_reward": round(risk_reward_score, 1),
    }

    return SignalResult(
        total_score=total,
        state=state,
        rating=rating,
        reasons=reasons[:8],
        risks=risks[:8] if risks else ["暂未发现特别突出的单项风险，但仍需结合大盘和仓位控制。"],
        buy_zone=f"{buy_low:.2f} - {buy_high:.2f}",
        stop_loss=f"{stop:.2f}",
        target=f"{target:.2f}",
        position=pos,
        risk_reward=round(rr, 2),
        components=components,
    )


def strategy_match(df: pd.DataFrame, strategy: str, market_env=None) -> tuple[bool, SignalResult]:
    sig = evaluate_signal(df, market_env=market_env)
    latest = df.iloc[-1]
    state = sig.state

    if strategy == "short":
        ok = (
            sig.total_score >= 68
            and state in ["趋势突破", "回踩确认"]
            and latest["close"] > latest["MA20"]
            and latest["volume"] > latest["VMA5"] * 1.05
            and latest["RSI"] < 78
        )
    elif strategy == "band":
        ok = (
            sig.total_score >= 70
            and latest["close"] > latest["MA20"] > latest["MA60"]
            and 42 <= latest["RSI"] <= 72
            and latest["ATR_PCT"] <= 0.055
            and state not in ["高位风险", "弱势反抽"]
        )
    elif strategy == "washout":
        # 注意：缩量洗盘优先进入观察池，真正买点需要回踩后重新放量确认。
        ok = (
            sig.total_score >= 62
            and state in ["缩量回踩观察", "回踩确认"]
            and latest["close"] > latest["MA60"]
            and latest["ATR_PCT"] <= 0.06
        )
    else:
        ok = sig.total_score >= 70

    return ok, sig


# ================== 分析器 ==================
class StockAnalyzer:
    def __init__(self, symbol, name=None, cost_price=None):
        self.symbol = str(symbol).strip()
        self.name = name or self.symbol
        self.cost_price = safe_float(cost_price) if cost_price not in [None, ""] else None
        self.df = pd.DataFrame()
        self.df_w = pd.DataFrame()
        self.vp_peak = 0.0

    def fetch_data(self, end_date=None, limit=KLINE_LIMIT) -> bool:
        try:
            self.df = fetch_eastmoney_kline(secid_for_stock(self.symbol), end_date=end_date, limit=limit)
            if len(self.df) >= 80:
                return True
        except Exception as e:
            logger.warning(f"EastMoney kline failed {self.symbol}: {e}")
        try:
            self.df = fetch_tencent_kline(self.symbol, end_date=end_date, limit=limit)
            return len(self.df) >= 80
        except Exception as e:
            logger.warning(f"Tencent kline failed {self.symbol}: {e}")
        return False

    def prepare(self) -> bool:
        if self.df.empty or len(self.df) < 80:
            return False
        self.df = calculate_indicators(self.df)
        self.df_w = weekly_indicators(self.df)
        self.vp_peak = volume_price_peak(self.df)
        return not self.df.empty

    def full_report(self):
        if not self.prepare():
            return {"error": "数据不足或指标计算失败，可能是停牌、退市、接口异常或历史数据过短。"}

        market_env = get_market_environment()
        sig = evaluate_signal(self.df, market_env=market_env, cost_price=self.cost_price)
        latest = self.df.iloc[-1]
        prev = self.df.iloc[-2]
        change = pct(latest["close"], prev["close"]) * 100
        vol_ratio = latest["volume"] / (latest["VMA5"] + 1e-9)

        weekly_text = "周线数据不足。"
        if not self.df_w.empty and len(self.df_w) >= 10:
            lw = self.df_w.iloc[-1]
            weekly_text = (
                f"周线收盘:{lw['close']:.2f}；MA5:{lw['MA5']:.2f}；MA10:{lw['MA10']:.2f}；"
                f"KDJ-J:{lw['J']:.1f}；MACD:{lw['MACD']:.3f}。"
            )

        cost_line = "未填写"
        if self.cost_price:
            pnl = pct(latest["close"], self.cost_price) * 100
            cost_line = f"{self.cost_price:.2f}，当前浮盈亏:{pnl:+.2f}%"

        reason_text = "\n  ".join([f"✅ {x}" for x in sig.reasons])
        risk_text = "\n  ".join([f"⚠️ {x}" for x in sig.risks])
        comp = sig.components

        text_report = f"""=======================================================
📊 【{self.name} ({self.symbol})】A股技术面观察报告 Realistic Edition
=======================================================
【基础状态】
最新收盘：{latest['close']:.2f} 元 | 日涨跌：{change:+.2f}% | 持仓成本：{cost_line}
交易状态：{sig.state} | 观察评级：{sig.rating} | 综合分：{sig.total_score}/100
市场环境：{market_env.get('status')} | 风险收益比：{sig.risk_reward}

【关键指标】
MA20:{latest['MA20']:.2f} | MA60:{latest['MA60']:.2f} | ATR波动:{latest['ATR_PCT']*100:.2f}%
KDJ-J:{latest['J']:.1f} | RSI:{latest['RSI']:.1f} | MACD:{latest['MACD']:.3f} | 量比(5日):{vol_ratio:.2f}
20日支撑:{latest['Support20']:.2f} | 20日压力:{latest['Resistance20']:.2f} | 近60日量价密集区:{self.vp_peak:.2f}

【评分拆解】
市场:{comp['market']} | 趋势:{comp['trend']} | 动量:{comp['momentum']} | 量价:{comp['volume']} | 赔率:{comp['risk_reward']}

【支持逻辑】
  {reason_text}

【风险提示】
  {risk_text}

【操作框架】
参考买入区间：{sig.buy_zone}
风控止损参考：{sig.stop_loss}
第一目标/压力：{sig.target}
仓位建议：{sig.position}

【周线观察】
{weekly_text}
=======================================================
说明：本模型用于形成观察池和复盘，不构成投资建议。真正买入需结合基本面、行业催化、公告风险和个人仓位。
"""

        chart_data = []
        for _, row in self.df.tail(100).iterrows():
            chart_data.append({
                "date": safe_date(row["date"]),
                "open": safe_float(row["open"]),
                "high": safe_float(row["high"]),
                "low": safe_float(row["low"]),
                "close": safe_float(row["close"]),
                "ma20": safe_float(row["MA20"]),
                "ma60": safe_float(row["MA60"]),
                "volume": safe_float(row["volume"]),
            })

        return {
            "name": self.name,
            "code": self.symbol,
            "price": safe_float(latest["close"]),
            "change": round(change, 2),
            "state": sig.state,
            "rating": sig.rating,
            "score": sig.total_score,
            "risk_reward": sig.risk_reward,
            "buy_zone": sig.buy_zone,
            "stop_loss": sig.stop_loss,
            "target": sig.target,
            "position": sig.position,
            "kdj": {"K": safe_float(latest["K"]), "D": safe_float(latest["D"]), "J": safe_float(latest["J"])},
            "rsi": safe_float(latest["RSI"]),
            "macd": safe_float(latest["MACD"]),
            "vol_ratio": safe_float(vol_ratio),
            "text_report": text_report,
            "chart_data": chart_data,
        }


# ================== 扫描 ==================
def analyze_single_scan(stock, strategy, market_env):
    az = StockAnalyzer(stock["code"], stock.get("name"))
    if not az.fetch_data() or not az.prepare():
        return None
    ok, sig = strategy_match(az.df, strategy, market_env=market_env)
    if not ok:
        return None
    latest = az.df.iloc[-1]
    return {
        "code": az.symbol,
        "name": az.name,
        "sector": stock.get("sector", ""),
        "score": sig.total_score,
        "state": sig.state,
        "rating": sig.rating,
        "close": f"{latest['close']:.2f}",
        "buy_zone": sig.buy_zone,
        "stop_loss": sig.stop_loss,
        "target": sig.target,
        "risk_reward": sig.risk_reward,
    }


def scan_stocks(strategy, top_n=10, limit=SCAN_LIMIT):
    stocks = get_active_stocks(limit)
    market_env = get_market_environment()
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(analyze_single_scan, s, strategy, market_env) for s in stocks]
        for f in as_completed(futures):
            try:
                res = f.result()
                if res:
                    results.append(res)
            except Exception as e:
                logger.warning(f"scan item failed: {e}")
    results.sort(key=lambda x: (x["score"], x["risk_reward"]), reverse=True)
    return results[:top_n]


# ================== 回测 ==================
def get_backtest_pool(limit=120):
    stocks = get_active_stocks(limit)
    return stocks[:limit]


def run_backtest(strategy, test_date_str, hold_days=5, pool_limit=120):
    """
    单日截面回测：
    1. 使用test_date当天及以前数据生成信号，避免未来函数；
    2. 下一交易日开盘买入；
    3. 持有hold_days个交易日，按收盘卖出；
    4. 扣除ROUND_TRIP_COST。
    """
    if not test_date_str:
        return None
    test_date = safe_date(test_date_str)
    market_env = get_market_environment()
    picks = []

    for stock in get_backtest_pool(pool_limit):
        code, name = stock["code"], stock.get("name", stock["code"])
        az = StockAnalyzer(code, name)
        if not az.fetch_data(limit=KLINE_LIMIT):
            continue
        full = calculate_indicators(az.df)
        if full.empty or len(full) < 100:
            continue

        eligible = full[full["date"] <= test_date]
        if len(eligible) < 90:
            continue
        idx = eligible.index[-1]
        if idx + hold_days >= len(full) or idx + 1 >= len(full):
            continue

        hist = calculate_indicators(full.iloc[:idx + 1].copy())
        ok, sig = strategy_match(hist, strategy, market_env=market_env)
        if not ok:
            continue

        buy_row = full.iloc[idx + 1]
        sell_row = full.iloc[idx + hold_days]
        buy_price = safe_float(buy_row["open"])
        sell_price = safe_float(sell_row["close"])
        if buy_price <= 0 or sell_price <= 0:
            continue
        ret = sell_price / buy_price - 1 - ROUND_TRIP_COST
        picks.append({
            "code": code,
            "name": name,
            "buy_date": buy_row["date"],
            "sell_date": sell_row["date"],
            "buy": buy_price,
            "sell": sell_price,
            "profit": ret * 100,
            "success": ret > 0,
            "score": sig.total_score,
            "state": sig.state,
        })

    if not picks:
        return None

    profits = np.array([p["profit"] for p in picks], dtype=float)
    wins = profits[profits > 0]
    losses = profits[profits <= 0]
    profit_factor = abs(wins.sum() / losses.sum()) if losses.sum() != 0 else float("inf")

    return {
        "date": test_date,
        "strategy": strategy,
        "hold": hold_days,
        "total": len(picks),
        "accuracy": float((profits > 0).mean() * 100),
        "avg_return": float(profits.mean()),
        "median_return": float(np.median(profits)),
        "max_gain": float(profits.max()),
        "max_loss": float(profits.min()),
        "profit_factor": float(profit_factor) if np.isfinite(profit_factor) else 999.0,
        "picks": sorted(picks, key=lambda x: x["score"], reverse=True)[:30],
    }


# ================== Flask 路由 ==================
app = Flask(__name__)


@app.route("/")
def home():
    return render_template_string(HTML)


@app.route("/analyze", methods=["POST"])
def analyze():
    d = request.get_json() or {}
    code, name = resolve_stock_input(d.get("stock"))
    if not code or not re.match(r"^\d{6}$", str(code)):
        return jsonify({"error": "无法识别股票，请输入6位A股代码或准确名称。"})
    az = StockAnalyzer(code, name, d.get("cost"))
    if not az.fetch_data():
        return jsonify({"error": "网络连接异常或该股票近期无足够数据。"})
    rep = az.full_report()
    if "error" in rep:
        return jsonify(rep)
    return jsonify({"report": rep})


@app.route("/scan", methods=["POST"])
def scan():
    d = request.get_json() or {}
    strategy = d.get("strategy", "washout")
    top_n = int(d.get("top_n", 10) or 10)
    limit = int(d.get("limit", SCAN_LIMIT) or SCAN_LIMIT)
    return jsonify({"results": scan_stocks(strategy, top_n=top_n, limit=limit)})


@app.route("/portfolio", methods=["POST"])
def portfolio():
    holdings = (request.get_json() or {}).get("holdings", [])
    if not holdings:
        return jsonify({"error": "请输入有效仓位。"})

    total_weight = sum(safe_float(h.get("weight")) for h in holdings)
    rows = []
    weighted_score = 0.0

    with ThreadPoolExecutor(max_workers=min(10, len(holdings))) as ex:
        futures = {}
        for h in holdings:
            code, name = resolve_stock_input(h.get("code"))
            if not code or not re.match(r"^\d{6}$", str(code)):
                continue
            cost = safe_float(h.get("cost"))
            weight = safe_float(h.get("weight"))
            az = StockAnalyzer(code, name, cost)
            futures[ex.submit(az.fetch_data)] = (az, cost, weight)

        for f in as_completed(futures):
            az, cost, weight = futures[f]
            try:
                if not f.result() or not az.prepare():
                    continue
                sig = evaluate_signal(az.df, cost_price=cost)
                latest = az.df.iloc[-1]
                pnl = pct(latest["close"], cost) * 100 if cost > 0 else 0
                weighted_score += sig.total_score * (weight / 100)
                rows.append({
                    "code": az.symbol,
                    "name": az.name,
                    "price": latest["close"],
                    "cost": cost,
                    "pnl": pnl,
                    "weight": weight,
                    "score": sig.total_score,
                    "state": sig.state,
                    "rating": sig.rating,
                    "stop": sig.stop_loss,
                    "position": sig.position,
                })
            except Exception as e:
                logger.warning(f"portfolio item failed: {e}")

    if not rows:
        return jsonify({"error": "组合数据分析失败，请检查代码或网络。"})

    concentration = max([r["weight"] for r in rows]) if rows else 0
    risk_notes = []
    if total_weight > 100:
        risk_notes.append("总仓位超过100%，请检查输入。")
    if concentration >= 40:
        risk_notes.append("单一股票仓位过高，组合波动会被单票主导。")
    if weighted_score < 55:
        risk_notes.append("组合加权技术评分偏低，建议降低弱势标的占比。")
    if not risk_notes:
        risk_notes.append("组合暂无明显集中风险，但仍需结合行业相关性与基本面。")

    report = {
        "total_weight": total_weight,
        "weighted_score": round(weighted_score, 1),
        "concentration": concentration,
        "risk_notes": risk_notes,
        "rows": rows,
    }
    return jsonify({"report": report})


@app.route("/backtest", methods=["POST"])
def run_bt():
    d = request.get_json() or {}
    strategy = d.get("strategy", "washout")
    date = d.get("date")
    hold_days = int(d.get("hold_days", 5) or 5)
    pool_limit = int(d.get("pool_limit", 120) or 120)
    res = run_backtest(strategy, date, hold_days, pool_limit=pool_limit)
    if not res:
        return jsonify({"error": "当日无信号、历史数据不足，或该日期后没有足够交易日。"})

    txt = (
        f"📅 截面回测: {res['date']} | 策略: {res['strategy']} | 持有{res['hold']}个交易日\n"
        f"说明：信号日收盘后生成，下一交易日开盘买入，卖出按持有期末收盘，已粗略扣除{ROUND_TRIP_COST*100:.2f}%交易摩擦。\n"
        f"信号数:{res['total']} | 胜率:{res['accuracy']:.1f}% | 平均收益:{res['avg_return']:+.2f}% | 中位数:{res['median_return']:+.2f}%\n"
        f"最大盈利:{res['max_gain']:+.2f}% | 最大亏损:{res['max_loss']:+.2f}% | 盈亏因子:{res['profit_factor']:.2f}\n"
        + "-" * 50 + "\n"
    )
    for p in res["picks"]:
        txt += (
            f"{p['name']}({p['code']}) 分数:{p['score']} 状态:{p['state']} "
            f"买:{p['buy_date']}@{p['buy']:.2f} 卖:{p['sell_date']}@{p['sell']:.2f} "
            f"收益:{p['profit']:+.2f}% {'✅' if p['success'] else '❌'}\n"
        )
    return jsonify({"report": txt, "raw": res})


# ================== 前端 ==================
HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PRO-QUANT Realistic Edition</title>
    <link href="https://cdn.bootcdn.net/ajax/libs/twitter-bootstrap/5.3.0/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.bootcdn.net/ajax/libs/bootstrap/5.3.0/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
    <style>
        body { background-color: #0f172a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
        .card { background-color: #1e293b; border: 1px solid #334155; border-radius: 14px; margin-bottom: 15px; }
        .card-header-custom { background-color: #020617; border-bottom: 1px solid #334155; padding: 12px 15px; font-weight: 700; border-radius: 14px 14px 0 0; color: #f8fafc; }
        input.form-control, select.form-select { background-color: #334155 !important; color: #ffffff !important; border: 1px solid #475569 !important; }
        input::placeholder { color: #94a3b8 !important; }
        .btn-primary { background-color: #10b981; border: none; color: #fff; }
        .btn-primary:hover { background-color: #059669; }
        .terminal-box { background-color: #020617; color: #d1fae5; font-family: Consolas, monospace; padding: 15px; border-radius: 10px; border: 1px solid #334155; font-size: 14px; white-space: pre-wrap; line-height: 1.65; }
        .metric-box { background-color: #020617; border: 1px solid #334155; border-radius: 10px; padding: 11px; text-align: center; color: #ffffff; height: 100%; }
        .metric-title { color: #94a3b8; font-size: 13px; margin-bottom: 4px; }
        .stock-card { cursor: pointer; transition: 0.2s; }
        .stock-card:hover { border-color: #10b981; background-color: #334155; }
        .text-up { color: #ef4444 !important; }
        .text-down { color: #10b981 !important; }
        #chart-container { height: 460px; width: 100%; margin-top: 20px; display: block; position: relative; }
        .badge-soft { background:#0f172a; border:1px solid #334155; color:#cbd5e1; }
    </style>
</head>
<body>
<nav class="navbar navbar-dark mb-3 border-bottom border-secondary" style="background-color:#020617;">
    <div class="container-fluid"><span class="navbar-brand fw-bold" style="color:#34d399;">🛰️ PRO-QUANT Realistic Edition</span></div>
</nav>

<div class="container-fluid px-4">
    <div class="row">
        <div class="col-lg-3">
            <div class="card">
                <div class="card-header-custom">🎯 个股观察</div>
                <div class="card-body">
                    <input type="text" id="s_code" class="form-control mb-2" placeholder="代码/名称/拼音" value="北方稀土">
                    <input type="number" step="0.01" id="s_cost" class="form-control mb-3" placeholder="持仓成本(可选)">
                    <button class="btn btn-primary w-100 fw-bold" onclick="analyze()">执行分析</button>
                </div>
            </div>

            <div class="card">
                <div class="card-header-custom">📡 策略扫描</div>
                <div class="card-body">
                    <button class="btn btn-outline-info w-100 mb-2 text-white" onclick="scan('short')">短线突破/确认</button>
                    <button class="btn btn-outline-success w-100 mb-2 text-white" onclick="scan('band')">中线波段</button>
                    <button class="btn btn-outline-warning w-100 text-white" onclick="scan('washout')">缩量回踩观察</button>
                    <div class="small mt-2" style="color:#94a3b8;">扫描逻辑：先完整计算，再按分数与赔率排序，不再按网络返回速度截断。</div>
                </div>
            </div>

            <div class="card">
                <div class="card-header-custom">🛠️ 组合与回测</div>
                <div class="card-body">
                    <div id="ports" class="mb-2">
                        <div class="d-flex gap-1 mb-1">
                            <input class="form-control form-control-sm p_code" placeholder="代码">
                            <input class="form-control form-control-sm p_cost" placeholder="成本">
                            <input class="form-control form-control-sm p_weight" placeholder="仓位%">
                        </div>
                    </div>
                    <div class="d-flex justify-content-between mb-3">
                        <button class="btn btn-sm btn-outline-secondary text-white" onclick="addPort()">+加一行</button>
                        <button class="btn btn-sm btn-success" onclick="calcPort()">组合分析</button>
                    </div>
                    <hr style="border-color:#475569;">
                    <select id="bt_strat" class="form-select form-select-sm mb-1">
                        <option value="washout">缩量回踩观察</option>
                        <option value="short">短线突破/确认</option>
                        <option value="band">中线波段</option>
                    </select>
                    <input type="date" id="bt_date" class="form-control form-control-sm mb-1">
                    <select id="bt_hold" class="form-select form-select-sm mb-2">
                        <option value="3">持有3天</option>
                        <option value="5">持有5天</option>
                        <option value="10">持有10天</option>
                    </select>
                    <button class="btn btn-sm btn-outline-danger w-100 text-white" onclick="runBt()">截面回测</button>
                </div>
            </div>
        </div>

        <div class="col-lg-9">
            <div class="card" style="min-height:800px;">
                <div class="card-header-custom d-flex justify-content-between align-items-center">
                    <span>📺 观察面板</span>
                    <span id="loader" style="display:none;color:#34d399;"><div class="spinner-border spinner-border-sm" role="status"></div> 计算中...</span>
                </div>
                <div class="card-body" id="displayArea">
                    <div class="text-center mt-5" style="color:#64748b;"><h3>等待指令...</h3><p>该版本更偏向“观察池+风控框架”，不直接输出无条件买入。</p></div>
                </div>
            </div>
        </div>
    </div>
</div>

<script>
    document.getElementById('bt_date').value = new Date().toISOString().slice(0,10);
    let kChart = null;

    async function analyze() {
        const stock = document.getElementById('s_code').value;
        const cost = document.getElementById('s_cost').value;
        if(!stock) return;
        if(kChart) { kChart.destroy(); kChart = null; }
        document.getElementById('loader').style.display = 'block';
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 25000);
        try {
            const res = await fetch('/analyze', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({stock,cost}), signal:controller.signal });
            clearTimeout(timeout);
            const data = await res.json();
            document.getElementById('loader').style.display = 'none';
            if(data.error) { document.getElementById('displayArea').innerHTML = `<div class="alert" style="background:#7f1d1d;color:#fff;">❌ ${data.error}</div>`; return; }
            const r = data.report;
            const color = r.change >= 0 ? 'text-up' : 'text-down';
            document.getElementById('displayArea').innerHTML = `
            <div class="d-flex justify-content-between align-items-end mb-3">
                <div><h3 class="text-white mb-1 fw-bold">${r.name} <span class="fs-6" style="color:#94a3b8;">${r.code}</span></h3><span class="badge badge-soft">${r.state}</span> <span class="badge badge-soft">${r.rating}</span></div>
                <h2 class="${color} mb-0 fw-bold">${r.price.toFixed(2)} <span class="fs-6">${r.change >= 0 ? '+'+r.change : r.change}%</span></h2>
            </div>
            <div class="row g-2 mb-3">
                <div class="col-3"><div class="metric-box"><div class="metric-title">综合分</div><div class="fw-bold" style="color:#34d399;">${r.score}/100</div></div></div>
                <div class="col-3"><div class="metric-box"><div class="metric-title">风险收益比</div><div class="fw-bold text-white">${r.risk_reward}</div></div></div>
                <div class="col-3"><div class="metric-box"><div class="metric-title">RSI / J</div><div class="fw-bold text-white">${r.rsi.toFixed(1)} / ${r.kdj.J.toFixed(1)}</div></div></div>
                <div class="col-3"><div class="metric-box"><div class="metric-title">量比</div><div class="fw-bold text-white">${r.vol_ratio.toFixed(2)}</div></div></div>
            </div>
            <div class="row g-2 mb-3">
                <div class="col-md-4"><div class="metric-box"><div class="metric-title">参考买入区间</div><div class="fw-bold" style="color:#fcd34d;">${r.buy_zone}</div></div></div>
                <div class="col-md-4"><div class="metric-box"><div class="metric-title">止损参考</div><div class="fw-bold" style="color:#fb7185;">${r.stop_loss}</div></div></div>
                <div class="col-md-4"><div class="metric-box"><div class="metric-title">目标/压力</div><div class="fw-bold" style="color:#38bdf8;">${r.target}</div></div></div>
            </div>
            <div id="chart-container"></div>
            <div class="mt-4"><button class="btn w-100 text-start" type="button" data-bs-toggle="collapse" data-bs-target="#textReport" style="background:#1e293b;border:1px solid #334155;color:#f8fafc;">📄 展开/折叠 完整报告</button><div class="collapse show mt-2" id="textReport"><pre class="terminal-box">${r.text_report}</pre></div></div>`;
            setTimeout(() => renderChart(r.chart_data), 100);
        } catch(e) {
            clearTimeout(timeout);
            document.getElementById('loader').style.display = 'none';
            document.getElementById('displayArea').innerHTML = `<div class="alert" style="background:#991b1b;color:#fff;">请求超时或渲染失败：${e.message}</div>`;
        }
    }

    function renderChart(data) {
        if(!data || data.length === 0) return;
        const kLineData = data.map(i => ({ x:new Date(i.date).getTime(), y:[i.open,i.high,i.low,i.close] }));
        const ma20Data = data.map(i => ({ x:new Date(i.date).getTime(), y:i.ma20 }));
        const ma60Data = data.map(i => ({ x:new Date(i.date).getTime(), y:i.ma60 }));
        const volData = data.map(i => ({ x:new Date(i.date).getTime(), y:i.volume, fillColor:i.close >= i.open ? 'rgba(239,68,68,0.45)' : 'rgba(16,185,129,0.45)' }));
        const options = {
            series:[
                { name:'K线', type:'candlestick', data:kLineData },
                { name:'MA20', type:'line', data:ma20Data },
                { name:'MA60', type:'line', data:ma60Data },
                { name:'成交量', type:'column', data:volData }
            ],
            chart:{ height:460, type:'line', background:'transparent', toolbar:{show:false} },
            theme:{ mode:'dark' },
            stroke:{ width:[1,2,2,1], curve:'smooth' },
            colors:['#fff','#fbbf24','#38bdf8','#fff'],
            plotOptions:{ candlestick:{ colors:{ upward:'#ef4444', downward:'#10b981' }, wick:{ useFillColor:true } }, bar:{ columnWidth:'80%' } },
            xaxis:{ type:'datetime', labels:{ style:{ colors:'#94a3b8' } } },
            yaxis:[
                { seriesName:'K线', title:{ text:'价格', style:{ color:'#64748b' } }, labels:{ style:{ colors:'#94a3b8' }, formatter:val => val ? val.toFixed(2) : '' } },
                { seriesName:'K线', show:false },
                { seriesName:'K线', show:false },
                { seriesName:'成交量', opposite:true, title:{ text:'成交量', style:{ color:'#64748b' } }, labels:{ style:{ colors:'#94a3b8' }, formatter:val => val ? (val/10000).toFixed(0)+'w' : '' } }
            ],
            grid:{ borderColor:'#334155', strokeDashArray:3 },
            legend:{ position:'top', horizontalAlign:'left', labels:{ colors:'#f8fafc' } }
        };
        kChart = new ApexCharts(document.querySelector('#chart-container'), options);
        kChart.render();
    }

    async function scan(strategy) {
        document.getElementById('loader').style.display = 'block';
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 45000);
        try {
            const res = await fetch('/scan', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({strategy, top_n:10, limit:280}), signal:controller.signal });
            clearTimeout(timeout);
            const data = await res.json();
            document.getElementById('loader').style.display = 'none';
            let html = `<h5 class="mb-3 text-white border-bottom border-secondary pb-2">🎯 扫描结果</h5><div class="row g-2">`;
            if(!data.results || data.results.length === 0) html += `<div style="color:#94a3b8;">没有扫描到符合条件的标的。市场弱时，这是正常结果。</div>`;
            data.results.forEach(r => {
                html += `<div class="col-md-6"><div class="card p-2 stock-card h-100" onclick="quickAnalyze('${r.code}')"><div class="d-flex justify-content-between mb-1"><strong class="text-white">${r.name}</strong><span class="small fw-bold" style="color:#34d399;">${r.score}分</span></div><div style="font-size:12px;color:#cbd5e1;">${r.state} | ${r.rating} | 现价:${r.close}</div><div style="font-size:12px;color:#cbd5e1;">买入区间:<span style="color:#fcd34d;">${r.buy_zone}</span> | 止损:${r.stop_loss} | 赔率:${r.risk_reward}</div></div></div>`;
            });
            html += '</div>';
            document.getElementById('displayArea').innerHTML = html;
        } catch(e) {
            clearTimeout(timeout);
            document.getElementById('loader').style.display = 'none';
            document.getElementById('displayArea').innerHTML = `<div class="alert" style="background:#991b1b;color:#fff;">扫描超时或网络中断。</div>`;
        }
    }

    function quickAnalyze(code) { document.getElementById('s_code').value = code; analyze(); }

    function addPort() {
        const div = document.createElement('div'); div.className = 'd-flex gap-1 mb-1';
        div.innerHTML = '<input class="form-control form-control-sm p_code" placeholder="代码"><input class="form-control form-control-sm p_cost" placeholder="成本"><input class="form-control form-control-sm p_weight" placeholder="仓位%">';
        document.getElementById('ports').appendChild(div);
    }

    async function calcPort() {
        const rows = document.querySelectorAll('#ports .d-flex');
        const holdings = [];
        rows.forEach(r => {
            const code = r.querySelector('.p_code').value.trim();
            const cost = r.querySelector('.p_cost').value;
            const weight = r.querySelector('.p_weight').value;
            if(code) holdings.push({code, cost:cost||0, weight:weight||0});
        });
        document.getElementById('loader').style.display = 'block';
        const res = await fetch('/portfolio', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({holdings}) });
        const data = await res.json();
        document.getElementById('loader').style.display = 'none';
        if(data.error) { document.getElementById('displayArea').innerHTML = `<div class="alert" style="background:#991b1b;color:#fff;">${data.error}</div>`; return; }
        const r = data.report;
        let html = `<h5 class="text-white">🛠️ 组合分析</h5><div class="row g-2 mb-3"><div class="col-md-4"><div class="metric-box"><div class="metric-title">总仓位</div><div class="fw-bold">${r.total_weight.toFixed(1)}%</div></div></div><div class="col-md-4"><div class="metric-box"><div class="metric-title">加权评分</div><div class="fw-bold" style="color:#34d399;">${r.weighted_score}</div></div></div><div class="col-md-4"><div class="metric-box"><div class="metric-title">最大单票仓位</div><div class="fw-bold">${r.concentration.toFixed(1)}%</div></div></div></div>`;
        html += `<pre class="terminal-box">${r.risk_notes.map(x => '⚠️ ' + x).join('\n')}</pre>`;
        html += `<div class="table-responsive"><table class="table table-dark table-sm align-middle"><thead><tr><th>名称</th><th>现价</th><th>盈亏</th><th>仓位</th><th>评分</th><th>状态</th><th>止损</th></tr></thead><tbody>`;
        r.rows.forEach(x => { html += `<tr><td>${x.name}(${x.code})</td><td>${x.price.toFixed(2)}</td><td class="${x.pnl>=0?'text-up':'text-down'}">${x.pnl.toFixed(2)}%</td><td>${x.weight}%</td><td>${x.score}</td><td>${x.state}</td><td>${x.stop}</td></tr>`; });
        html += `</tbody></table></div>`;
        document.getElementById('displayArea').innerHTML = html;
    }

    async function runBt() {
        const strategy = document.getElementById('bt_strat').value;
        const date = document.getElementById('bt_date').value;
        const hold_days = document.getElementById('bt_hold').value;
        document.getElementById('loader').style.display = 'block';
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 60000);
        try {
            const res = await fetch('/backtest', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({strategy,date,hold_days,pool_limit:100}), signal:controller.signal });
            clearTimeout(timeout);
            const data = await res.json();
            document.getElementById('loader').style.display = 'none';
            document.getElementById('displayArea').innerHTML = `<h5 class="text-white">⏳ 回测报告</h5><pre class="terminal-box mt-3">${data.report || data.error}</pre>`;
        } catch(e) {
            clearTimeout(timeout);
            document.getElementById('loader').style.display = 'none';
            document.getElementById('displayArea').innerHTML = `<div class="alert" style="background:#991b1b;color:#fff;">回测超时。可以降低股票池数量或换一个历史日期。</div>`;
        }
    }
</script>
</body>
</html>'''


if __name__ == "__main__":
    print("✅ PRO-QUANT Realistic Edition 启动成功")
    print("👉 访问 http://127.0.0.1:10000")
    app.run(host="0.0.0.0", port=10000, debug=False)
