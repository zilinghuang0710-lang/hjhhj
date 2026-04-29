# -*- coding: utf-8 -*-
"""
PRO-QUANT 终极融合版（修复数据获取 + 多策略回测 + 真实情绪）
修复要点：
1. 所有请求统一添加 Referer，解决“数据获取失败”
2. 指数改用沪深300，市场环境判断合理化
3. 双数据源切换（东财/腾讯），兜底股票池扩大
4. 情绪因子融合真实人气排名与量价RFI代理
5. 龙回头识别严格化
6. 回测支持5种退出规则自动对比，输出最优
7. 前端集成大盘气候仪表盘
"""

import logging
import math
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import urllib3
from flask import Flask, jsonify, render_template_string, request
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================== 基本配置 ==================
KLINE_LIMIT = 500
ACTIVE_CACHE_TTL = 30 * 60
MARKET_CACHE_TTL = 10 * 60
MAX_WORKERS_SCAN = 16
MAX_WORKERS_BT = 12
DEFAULT_COST_RATE = 0.0015

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ================== 网络会话（统一加防爬头） ==================
def create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=0.2, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=100, pool_maxsize=100)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": random.choice(USER_AGENTS)})
    session.verify = False
    return session


HTTP = create_session()


def api_get(url, params=None, timeout=8):
    """统一GET请求，自动添加Referer"""
    return HTTP.get(url, params=params,
                    headers={"Referer": "https://quote.eastmoney.com/"},
                    timeout=timeout)


# ================== 工具函数 ==================
def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        return default if math.isnan(v) or math.isinf(v) else v
    except:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except:
        return default


def safe_date(value: Any) -> str:
    text = str(value).strip().replace("/", "-")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def market_id_from_code(code: str) -> str:
    return "1" if str(code).startswith(("5", "6", "9")) else "0"


def dedupe_by_code(stocks):
    seen = {}
    for s in stocks:
        code = str(s.get("code", "")).strip()
        if re.fullmatch(r"\d{6}", code):
            seen[code] = s
    return list(seen.values())


# ================== 活跃股票池（双源+大兜底） ==================
active_cache = {"data": None, "time": 0.0}


def get_hardcoded_fallback_stocks():
    return [
        {"code": "600519", "name": "贵州茅台", "sector": "白酒"},
        {"code": "000858", "name": "五粮液", "sector": "白酒"},
        {"code": "601318", "name": "中国平安", "sector": "保险"},
        {"code": "600036", "name": "招商银行", "sector": "银行"},
        {"code": "601166", "name": "兴业银行", "sector": "银行"},
        {"code": "600030", "name": "中信证券", "sector": "券商"},
        {"code": "300059", "name": "东方财富", "sector": "互联网金融"},
        {"code": "300750", "name": "宁德时代", "sector": "电池"},
        {"code": "688981", "name": "中芯国际", "sector": "半导体"},
        {"code": "002475", "name": "立讯精密", "sector": "消费电子"},
        {"code": "000725", "name": "京东方A", "sector": "面板"},
        {"code": "601012", "name": "隆基绿能", "sector": "光伏"},
        {"code": "600276", "name": "恒瑞医药", "sector": "创新药"},
        {"code": "300015", "name": "爱尔眼科", "sector": "医疗服务"},
        {"code": "002594", "name": "比亚迪", "sector": "新能源车"},
        {"code": "601888", "name": "中国中免", "sector": "消费"},
        {"code": "600900", "name": "长江电力", "sector": "公用事业"},
        {"code": "000063", "name": "中兴通讯", "sector": "通信"},
        {"code": "000100", "name": "TCL科技", "sector": "电子"},
        {"code": "000001", "name": "平安银行", "sector": "银行"},
        {"code": "000002", "name": "万科A", "sector": "地产"},
        {"code": "000333", "name": "美的集团", "sector": "家电"},
        {"code": "000651", "name": "格力电器", "sector": "家电"},
        {"code": "600104", "name": "上汽集团", "sector": "汽车"},
        {"code": "002230", "name": "科大讯飞", "sector": "AI应用"},
        {"code": "300124", "name": "汇川技术", "sector": "工业自动化"},
        {"code": "300274", "name": "阳光电源", "sector": "光伏"},
        {"code": "601398", "name": "工商银行", "sector": "银行"},
        {"code": "601939", "name": "建设银行", "sector": "银行"},
        {"code": "600050", "name": "中国联通", "sector": "通信"},
        {"code": "601728", "name": "中国电信", "sector": "通信"},
    ]


def get_active_stocks(limit: int = 400) -> List[Dict[str, Any]]:
    global active_cache
    now = time.time()
    if active_cache["data"] is not None and (now - active_cache["time"]) < ACTIVE_CACHE_TTL:
        return active_cache["data"][:limit]

    stocks = []
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": str(max(limit, 1000)), "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f14,f100", "fid": "f3",
            "_": str(int(now * 1000))
        }
        resp = api_get(url, params)
        if resp.status_code == 200:
            payload = resp.json()
            diff = payload.get("data", {}).get("diff", []) if payload else []
            for item in diff:
                code = str(item.get("f12", "")).strip()
                name = str(item.get("f14", "")).strip()
                if not re.fullmatch(r"\d{6}", code) or not name:
                    continue
                if "ST" in name.upper() or "退" in name:
                    continue
                stocks.append({"code": code, "name": name, "sector": str(item.get("f100") or "未分类")})
    except Exception as e:
        logger.warning("get_active_stocks failed: %s", e)

    if len(stocks) < 50:
        stocks = get_hardcoded_fallback_stocks()

    unique = dedupe_by_code(stocks)
    random.shuffle(unique)
    active_cache["data"] = unique
    active_cache["time"] = now
    return unique[:limit]


def resolve_stock_input(keyword: str) -> Tuple[Optional[str], str]:
    text = str(keyword).strip()
    if re.fullmatch(r"\d{6}", text):
        return text, text
    try:
        resp = api_get("https://searchapi.eastmoney.com/api/suggest/get",
                       params={"input": text, "type": "14",
                               "token": "D43BF722C8E33BDC906FB84D85E326E8", "count": "5"})
        if resp.status_code == 200:
            rows = resp.json().get("QuotationCodeTable", {}).get("Data", [])
            for item in rows:
                code = str(item.get("Code", "")).strip()
                if re.fullmatch(r"\d{6}", code):
                    return code, str(item.get("Name") or text)
    except:
        pass
    return None, text


# ================== 市场环境（沪深300） ==================
market_index_cache = {"data": None, "time": 0.0}


def fetch_market_index() -> Optional[pd.DataFrame]:
    global market_index_cache
    now = time.time()
    if market_index_cache["data"] is not None and (now - market_index_cache["time"]) < MARKET_CACHE_TTL:
        return market_index_cache["data"].copy()
    try:
        params = {
            "secid": "1.000300", "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101", "fqt": "1", "end": "20500101", "lmt": "300"
        }
        resp = api_get("https://push2his.eastmoney.com/api/qt/stock/kline/get", params)
        if resp.status_code != 200:
            return None
        payload = resp.json()
        rows = payload.get("data", {}).get("klines", [])
        if not rows:
            return None
        parsed = []
        for item in rows:
            parts = str(item).split(",")
            if len(parts) < 6: continue
            parsed.append({
                "date": safe_date(parts[0]), "open": safe_float(parts[1]),
                "close": safe_float(parts[2]), "high": safe_float(parts[3]),
                "low": safe_float(parts[4]), "volume": safe_float(parts[5])
            })
        df = pd.DataFrame(parsed)
        if len(df) < 60: return None
        df["date"] = pd.to_datetime(df["date"])
        df = df.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
        df["MA20"] = df["close"].rolling(20, min_periods=10).mean()
        df["MA60"] = df["close"].rolling(60, min_periods=30).mean()
        market_index_cache["data"] = df.copy()
        market_index_cache["time"] = now
        return df
    except Exception as e:
        logger.warning("fetch_market_index failed: %s", e)
        return None


def get_market_state(at_date: Optional[str] = None) -> Dict[str, Any]:
    df = fetch_market_index()
    if df is None or df.empty:
        return {"state": "震荡", "score": 50.0, "multiplier": 1.0, "close": 0.0, "ma20": 0.0, "ma60": 0.0}
    working = df.copy()
    if at_date:
        try:
            target = pd.to_datetime(at_date)
            working = working[working["date"] <= target]
        except:
            pass
    if len(working) < 60:
        return {"state": "震荡", "score": 50.0, "multiplier": 1.0, "close": 0.0, "ma20": 0.0, "ma60": 0.0}
    latest = working.iloc[-1]
    close_p = safe_float(latest["close"])
    ma20 = safe_float(latest["MA20"])
    ma60 = safe_float(latest["MA60"])
    if close_p > ma20 > ma60:
        state, multiplier = "强势", 1.05
    elif close_p < ma20 < ma60:
        state, multiplier = "弱势", 0.75
    else:
        state, multiplier = "震荡", 1.0
    return {
        "state": state, "score": 80.0 if state == "强势" else (35.0 if state == "弱势" else 50.0),
        "multiplier": multiplier, "close": round(close_p, 2),
        "ma20": round(ma20, 2), "ma60": round(ma60, 2)
    }


# ================== 真实情绪人气数据 ==================
def fetch_stock_popularity(code: str) -> Dict[str, Any]:
    try:
        mkt = market_id_from_code(code)
        params = {"secid": f"{mkt}.{code}", "fields": "f136,f137"}
        resp = api_get("https://push2.eastmoney.com/api/qt/stock/get", params)
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            return {
                "rank": safe_int(data.get("f136")),
                "hot": safe_int(data.get("f137"))
            }
    except:
        pass
    return {"rank": 0, "hot": 0}


# ================== 个股分析引擎 ==================
class StockAnalyzer:
    def __init__(self, code: str, name: str = "", cost: float = None, sector: str = ""):
        self.code = code
        self.name = name or code
        self.cost = safe_float(cost) if cost is not None else None
        self.sector = sector
        self.df = pd.DataFrame()
        self.popularity = {}
        self.volume_price_peak = 0.0
        self.weekly_state = "周线数据不足"
        self.last_error = ""

    def fetch_data(self, end_date: Optional[str] = None, limit: int = KLINE_LIMIT) -> bool:
        self.last_error = ""
        if not re.fullmatch(r"\d{6}", self.code):
            self.last_error = "代码错误"; return False
        success = self._fetch_eastmoney(end_date, limit)
        if not success:
            success = self._fetch_tencent(limit)
        if not success:
            if not self.last_error: self.last_error = "行情获取失败"
            return False
        self.df["date"] = pd.to_datetime(self.df["date"], errors="coerce")
        self.df = self.df.dropna(subset=["date"]).drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
        if len(self.df) < 30:
            self.last_error = "数据不足"; return False
        self.popularity = fetch_stock_popularity(self.code)
        return True

    def _fetch_eastmoney(self, end_date, limit):
        try:
            params = {
                "secid": f"{market_id_from_code(self.code)}.{self.code}",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101", "fqt": "1",
                "end": end_date if end_date else "20500101",
                "lmt": str(limit)
            }
            resp = api_get("https://push2his.eastmoney.com/api/qt/stock/kline/get", params)
            if resp.status_code != 200: return False
            payload = resp.json()
            data = payload.get("data")
            if not data or not data.get("klines"): return False
            self.name = str(data.get("name", self.name))
            rows = data["klines"]
            parsed = []
            for item in rows:
                parts = str(item).split(",")
                if len(parts) < 11: continue
                parsed.append({
                    "date": safe_date(parts[0]), "open": safe_float(parts[1]),
                    "close": safe_float(parts[2]), "high": safe_float(parts[3]),
                    "low": safe_float(parts[4]), "volume": safe_float(parts[5]),
                    "amount": safe_float(parts[6]), "turnover": safe_float(parts[10])
                })
            if len(parsed) < 30: return False
            self.df = pd.DataFrame(parsed)
            return True
        except Exception as e:
            logger.warning("eastmoney fetch %s: %s", self.code, e)
            return False

    def _fetch_tencent(self, limit):
        try:
            prefix = "sh" if self.code.startswith(("5", "6", "9")) else "sz"
            params = {"param": f"{prefix}{self.code},day,,,{limit},qfq"}
            resp = api_get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get", params)
            if resp.status_code != 200: return False
            payload = resp.json()
            if payload.get("code") != 0: return False
            data = payload["data"].get(f"{prefix}{self.code}", {})
            if data.get("qt", {}).get(f"{prefix}{self.code}", [None])[1]:
                self.name = data["qt"][f"{prefix}{self.code}"][1]
            rows = data.get("qfqday") or data.get("day") or []
            parsed = []
            for item in rows:
                if len(item) < 6: continue
                close_p = safe_float(item[2])
                parsed.append({
                    "date": safe_date(item[0]), "open": safe_float(item[1]),
                    "close": close_p, "high": safe_float(item[3]),
                    "low": safe_float(item[4]), "volume": safe_float(item[5]),
                    "amount": max(safe_float(item[5]), 0) * close_p * 100,
                    "turnover": 0.0
                })
            if len(parsed) < 30: return False
            self.df = pd.DataFrame(parsed)
            return True
        except:
            return False

    def calc_indicators(self) -> bool:
        if self.df.empty: return False
        df = self.df.copy()
        for w in [5, 10, 20, 60]:
            df[f"MA{w}"] = df["close"].rolling(w, min_periods=max(3, w // 2)).mean()
        df["VMA5"] = df["volume"].rolling(5, min_periods=3).mean()
        df["VMA20"] = df["volume"].rolling(20, min_periods=8).mean()
        low_n = df["low"].rolling(9, min_periods=1).min()
        high_n = df["high"].rolling(9, min_periods=1).max()
        spread = (high_n - low_n).replace(0, np.nan)
        rsv = ((df["close"] - low_n) / spread * 100).fillna(50)
        df["K"] = rsv.ewm(com=2, adjust=False).mean()
        df["D"] = df["K"].ewm(com=2, adjust=False).mean()
        df["J"] = 3 * df["K"] - 2 * df["D"]
        ema12 = df["close"].ewm(span=12, adjust=False).mean()
        ema26 = df["close"].ewm(span=26, adjust=False).mean()
        df["DIF"] = ema12 - ema26
        df["DEA"] = df["DIF"].ewm(span=9, adjust=False).mean()
        df["MACD"] = 2 * (df["DIF"] - df["DEA"])
        delta = df["close"].diff()
        gain = delta.clip(lower=0).rolling(14, min_periods=6).mean()
        loss = (-delta.clip(upper=0)).rolling(14, min_periods=6).mean()
        rs = gain / (loss + 1e-9)
        df["RSI14"] = 100 - 100 / (1 + rs)
        prev_close = df["close"].shift(1)
        tr = pd.concat([df["high"] - df["low"],
                        (df["high"] - prev_close).abs(),
                        (df["low"] - prev_close).abs()], axis=1).max(axis=1)
        df["ATR14"] = tr.rolling(14, min_periods=6).mean()
        df["Support"] = df["low"].shift(1).rolling(20, min_periods=10).min()
        df["Resistance"] = df["high"].shift(1).rolling(20, min_periods=10).max()
        # 筹码峰
        recent = df.tail(60)
        if not recent.empty and recent["close"].max() > recent["close"].min():
            bins = np.linspace(recent["close"].min(), recent["close"].max(), 18)
            cuts = pd.cut(recent["close"], bins=bins, include_lowest=True)
            dist = recent.groupby(cuts, observed=False)["volume"].sum()
            if not dist.empty:
                self.volume_price_peak = safe_float(dist.idxmax().mid)
        df = df.replace([np.inf, -np.inf], np.nan).ffill().fillna(0.0)
        self.df = df.reset_index(drop=True)
        # 周线简判
        try:
            w = df.set_index("date").resample("W-FRI").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
            if len(w) >= 8:
                w["WMA5"] = w["close"].rolling(5, min_periods=3).mean()
                w["WMA10"] = w["close"].rolling(10, min_periods=5).mean()
                lw = w.iloc[-1]
                if safe_float(lw["close"]) > safe_float(lw["WMA5"]) > safe_float(lw["WMA10"]):
                    self.weekly_state = "周线偏强"
                elif safe_float(lw["close"]) < safe_float(lw["WMA5"]) < safe_float(lw["WMA10"]):
                    self.weekly_state = "周线偏弱"
                else:
                    self.weekly_state = "周线震荡"
        except:
            pass
        return True

    def _ctx(self, idx=-1) -> Dict:
        row = self.df.iloc[idx]
        prev = self.df.iloc[idx - 1] if idx > 0 else row

        def g(k): return safe_float(row.get(k))

        def gp(k): return safe_float(prev.get(k))

        close_p = g("close")
        amount = g("amount")
        if amount <= 0: amount = max(g("volume") * close_p * 100, 0)
        high60 = self.df["high"].iloc[max(0, idx - 59):idx + 1].max()
        low60 = self.df["low"].iloc[max(0, idx - 59):idx + 1].min()
        return {
            "close": close_p, "open": g("open"), "high": g("high"), "low": g("low"),
            "ma5": g("MA5"), "ma10": g("MA10"), "ma20": g("MA20"), "ma60": g("MA60"),
            "vma5": g("VMA5"), "vma20": g("VMA20"), "volume": g("volume"), "amount": amount,
            "j": g("J"), "prev_j": gp("J"), "k": g("K"), "prev_k": gp("K"),
            "diff": g("DIF"), "dea": g("DEA"), "macd": g("MACD"), "prev_macd": gp("MACD"),
            "rsi": g("RSI14"), "atr": g("ATR14"),
            "support": g("Support") if g("Support") > 0 else close_p * 0.95,
            "resistance": g("Resistance") if g("Resistance") > 0 else close_p * 1.05,
            "high60": high60, "low60": low60,
            "rise_60": (high60 / low60 - 1) if low60 > 0 else 0,
            "pullback": (high60 - close_p) / high60 if high60 > 0 else 0,
            "bullish": close_p > g("open"),
            "golden_cross": gp("K") <= gp("D") and g("K") > g("D"),
        }

    def sentiment_index(self) -> Tuple[float, str]:
        if len(self.df) < 10: return 50.0, "数据不足"
        recent = self.df.tail(10)
        volatility = (recent["high"].max() - recent["low"].min()) / max(recent["low"].min(), 0.01)
        rfi = min(100, recent["volume"].mean() / max(self.df["volume"].mean(), 1) * volatility * 100)
        if self.popularity.get("rank", 0) > 0 and self.popularity["rank"] <= 100:
            rfi = max(rfi, 90)
        stage = "冰点/酝酿"
        if rfi > 80: stage = "高潮/分歧"
        elif rfi > 60: stage = "加速/升温"
        elif rfi < 20: stage = "冰点/启动"
        return round(rfi, 1), stage

    def is_dragon_wave(self) -> Tuple[bool, float]:
        if len(self.df) < 30: return False, 0
        df30 = self.df.tail(30)
        peak_idx = df30["high"].idxmax()
        peak_price = df30.loc[peak_idx]["high"]
        if peak_price / (df30.iloc[0]["close"] + 0.01) < 1.40: return False, 0
        latest = self.df.iloc[-1]
        dd = (peak_price - latest["close"]) / (peak_price + 0.01)
        if not (0.15 <= dd <= 0.35): return False, 0
        if latest["volume"] > df30.loc[peak_idx]["volume"] * 0.45: return False, 0
        if latest["close"] < latest["MA20"]: return False, 0
        return True, 95

    def detect_state(self) -> str:
        ctx = self._ctx()
        if ctx["close"] < ctx["ma60"]: return "趋势破坏"
        if ctx["rise_60"] > 0.4 and ctx["j"] > 90 and ctx["volume"] > ctx["vma20"] * 1.5:
            return "高位放量分歧"
        if ctx["close"] > ctx["high60"] * 0.98 and ctx["volume"] > ctx["vma20"] * 1.3 and ctx["close"] > ctx["ma20"]:
            return "趋势突破"
        if ctx["rise_60"] > 0.15 and ctx["close"] > ctx["ma60"] and 0.05 <= ctx["pullback"] <= 0.18:
            if ctx["volume"] < ctx["vma20"] * 0.7:
                if ctx["bullish"] and ctx["volume"] > ctx["vma5"]:
                    return "回踩确认"
                return "缩量回踩观察"
        if ctx["j"] > ctx["prev_j"] and ctx["prev_j"] < 30:
            return "弱势反抽"
        return "震荡观察"

    def generate_report(self, market: Optional[Dict] = None) -> Dict:
        if not self.calc_indicators():
            return {"error": self.last_error or "指标计算失败"}
        market = market or get_market_state()
        ctx = self._ctx()
        state = self.detect_state()
        rfi, sent_stage = self.sentiment_index()
        dragon, dscore = self.is_dragon_wave()
        # 风控点位
        support = ctx["support"]
        resistance = ctx["resistance"]
        atr = ctx["atr"] if ctx["atr"] > 0 else ctx["close"] * 0.03
        stop = max(support * 0.98, ctx["close"] - 1.5 * atr, ctx["close"] * 0.92)
        target = max(resistance, ctx["close"] + 2 * atr, ctx["close"] * 1.05)
        risk = (ctx["close"] - stop) / ctx["close"] if ctx["close"] > 0 else 0
        reward = (target - ctx["close"]) / ctx["close"] if ctx["close"] > 0 else 0
        rr = reward / risk if risk > 0 else 0
        # 评分
        trend_s = 40 + (15 if ctx["close"] > ctx["ma20"] else 0) + (18 if ctx["close"] > ctx["ma60"] else 0)
        mom_s = 40 + (20 if ctx["golden_cross"] else 0) + (10 if ctx["macd"] > ctx["prev_macd"] else 0)
        vol_s = 40 + (28 if state == "趋势突破" else 26 if state == "回踩确认" else 14 if state == "缩量回踩观察" else -35 if state == "高位放量分歧" else 0)
        rr_s = 40 if rr < 1.2 else (60 if rr < 1.8 else 80)
        final = clamp(0.2 * market["score"] + 0.25 * trend_s + 0.15 * mom_s + 0.2 * vol_s + 0.2 * rr_s * market["multiplier"])
        if dragon: final = max(final, 90)

        chart = []
        for _, r in self.df.tail(120).iterrows():
            chart.append({
                "date": r["date"].strftime("%Y-%m-%d"),
                "open": safe_float(r["open"]), "high": safe_float(r["high"]),
                "low": safe_float(r["low"]), "close": safe_float(r["close"]),
                "ma20": safe_float(r.get("MA20")), "ma60": safe_float(r.get("MA60")),
                "volume": safe_float(r["volume"])
            })

        return {
            "code": self.code, "name": self.name, "price": round(ctx["close"], 2),
            "change": round((ctx["close"] - safe_float(self.df.iloc[-2]["close"])) / max(safe_float(self.df.iloc[-2]["close"]), 0.01) * 100, 2),
            "state": state, "rfi": rfi, "sent_stage": sent_stage, "dragon": dragon, "score": round(final, 1),
            "support": round(support, 2), "resistance": round(resistance, 2),
            "stop": round(stop, 2), "target": round(target, 2), "rr": round(rr, 2),
            "weekly": self.weekly_state, "market": market["state"],
            "popularity": self.popularity, "chart": chart
        }


def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


# ================== 扫描 ==================
def scan_stocks(strategy: str, top_n=12):
    universe = get_active_stocks(300)
    results = []

    def _scan(s):
        if "ST" in s["name"].upper(): return None
        a = StockAnalyzer(s["code"], s["name"], sector=s["sector"])
        if not a.fetch_data() or not a.calc_indicators(): return None
        ctx = a._ctx()
        if ctx["amount"] < 1e8: return None
        if strategy == "dragon_wave":
            passed, score = a.is_dragon_wave()
            if not passed: return None
        elif strategy == "short":
            if not (ctx["j"] < 35 and ctx["bullish"] and ctx["volume"] > ctx["vma5"] * 1.1): return None
            score = 70
        elif strategy == "washout":
            if not (ctx["rise_60"] > 0.06 and 0.05 <= ctx["pullback"] <= 0.15 and ctx["volume"] < ctx["vma20"] * 0.8): return None
            score = 75
        else:
            return None
        return {"code": a.code, "name": a.name, "close": round(ctx["close"], 2), "score": score}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS_SCAN) as ex:
        futures = {ex.submit(_scan, s): s for s in universe}
        for f in as_completed(futures):
            res = f.result()
            if res: results.append(res)
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_n]


# ================== 回测五大退出规则 ==================
def advanced_backtest(strategy: str, test_date: str, hold_days=5, pool_size=200):
    universe = get_active_stocks(pool_size)
    exit_rules = {
        "fixed": lambda _, tc: False,
        "ma5_break": lambda ctx, tc: tc["close"] < tc["ma5"],
        "ma10_break": lambda ctx, tc: tc["close"] < tc["ma10"],
        "trailing_15": lambda ctx, tc: (ctx.get("max_price", 0) - tc["close"]) / max(ctx.get("max_price", 1), 0.01) > 0.15,
        "hybrid": lambda ctx, tc: (tc["close"] < tc["ma5"]) or ((ctx.get("max_price", 0) - tc["close"]) / max(ctx.get("max_price", 1), 0.01) > 0.15)
    }
    rule_returns = {k: [] for k in exit_rules}
    total = 0

    def _bt(s):
        a = StockAnalyzer(s["code"], s["name"])
        if not a.fetch_data(limit=800) or not a.calc_indicators(): return None
        df = a.df
        trade_dates = df["date"].dt.strftime("%Y-%m-%d")
        valid_idx = df.index[trade_dates <= test_date].tolist()
        if not valid_idx: return None
        signal_idx = valid_idx[-1]
        if signal_idx < 50: return None
        ctx = a._ctx(signal_idx)
        if ctx["amount"] < 1e8: return None
        passed = False
        if strategy == "dragon_wave":
            passed = a.is_dragon_wave()[0]
        elif strategy == "short":
            passed = ctx["j"] < 35
        elif strategy == "washout":
            passed = (ctx["rise_60"] > 0.06 and 0.05 <= ctx["pullback"] <= 0.15 and ctx["volume"] < ctx["vma20"] * 0.8)
        if not passed: return None
        buy_idx = signal_idx + 1
        if buy_idx >= len(df): return None
        buy_price = df.iloc[buy_idx]["open"]
        res = {}
        for rule, check_fn in exit_rules.items():
            max_p = buy_price
            sell_idx = buy_idx + 1
            while sell_idx < len(df):
                row = df.iloc[sell_idx]
                close_p = safe_float(row["close"])
                max_p = max(max_p, close_p)
                temp = {"close": close_p, "ma5": safe_float(row["MA5"]), "ma10": safe_float(row["MA10"])}
                if rule == "fixed":
                    if (sell_idx - buy_idx) >= hold_days:
                        break
                else:
                    if check_fn({"max_price": max_p}, temp):
                        break
                sell_idx += 1
            if sell_idx >= len(df): continue
            sell_price = df.iloc[sell_idx]["close"]
            if sell_price <= 0: continue
            net = sell_price / buy_price - 1 - DEFAULT_COST_RATE
            res[rule] = net * 100
        return res

    with ThreadPoolExecutor(max_workers=MAX_WORKERS_BT) as ex:
        futures = {ex.submit(_bt, s): s for s in universe}
        for f in as_completed(futures):
            r = f.result()
            if not r: continue
            for k in exit_rules:
                if k in r: rule_returns[k].append(r[k])
            total += 1

    perf = {}
    for k, rets in rule_returns.items():
        if not rets: continue
        arr = np.array(rets)
        perf[k] = {
            "count": len(arr),
            "win_rate": round((arr > 0).mean() * 100, 1),
            "avg_return": round(arr.mean(), 2),
            "max_return": round(arr.max(), 2),
            "min_return": round(arr.min(), 2)
        }
    best = max(perf.items(), key=lambda kv: kv[1]["avg_return"] * kv[1]["win_rate"] / 100) if perf else None
    return {
        "strategy": strategy, "test_date": test_date, "total_signals": total,
        "rules": perf,
        "best_rule": best[0] if best else "N/A",
        "suggestion": f"建议使用「{best[0]}」退出规则" if best else "无数据"
    }


# ================== Flask ==================
app = Flask(__name__)


@app.route('/')
def index():
    return render_template_string(HTML)


@app.route('/climate')
def climate():
    idx = fetch_market_index()
    if idx is None or idx.empty:
        return jsonify({"status": "未知", "change": "0.00%", "j_val": "0", "amount": "0", "news": "未知"})
    latest = idx.iloc[-1]
    prev = idx.iloc[-2]
    change = (latest["close"] - prev["close"]) / prev["close"] * 100
    j_val = "N/A"
    try:
        l9 = idx["low"].rolling(9).min().iloc[-1]
        h9 = idx["high"].rolling(9).max().iloc[-1]
        rsv = (latest["close"] - l9) / (h9 - l9) * 100 if h9 != l9 else 50
        k = pd.Series([rsv]).ewm(com=2).mean().iloc[-1]
        d = pd.Series([k]).ewm(com=2).mean().iloc[-1]
        j_val = f"{3 * k - 2 * d:.1f}"
    except:
        pass
    close_p = latest["close"]
    ma20 = latest["MA20"]
    ma60 = latest["MA60"]
    if close_p > ma20 > ma60:
        state, color, pos = "🟢 温和做多区", "success", "7成~满仓"
    elif close_p < ma20 < ma60:
        state, color, pos = "🔴 阴跌绞肉机", "danger", "空仓或1成"
    elif close_p > ma20 and close_p < ma60:
        state, color, pos = "🟡 震荡蓄势", "warning", "3~5成"
    else:
        state, color, pos = "🟡 混沌震荡", "warning", "3~5成"
    return jsonify({
        "close": round(close_p, 2), "change": f"{change:+.2f}%",
        "j_val": j_val, "amount": f"{safe_float(latest['volume']) / 1e8:.0f}亿",
        "news": "活跃" if safe_float(latest["volume"]) > safe_float(idx["volume"].mean()) else "萎靡",
        "status": state, "color": color, "position": pos
    })


@app.route('/analyze', methods=['POST'])
def analyze():
    data = request.get_json(force=True) or {}
    code, name = resolve_stock_input(data.get("stock", ""))
    if not code:
        return jsonify({"error": "无法识别股票"})
    a = StockAnalyzer(code, name, data.get("cost"))
    if not a.fetch_data():
        return jsonify({"error": a.last_error or "行情获取失败"})
    rep = a.generate_report()
    if "error" in rep:
        return jsonify({"error": rep["error"]})
    return jsonify(rep)


@app.route('/scan', methods=['POST'])
def scan():
    data = request.get_json(force=True) or {}
    strategy = data.get("strategy", "dragon_wave")
    top_n = min(safe_int(data.get("top_n"), 12), 30)
    results = scan_stocks(strategy, top_n)
    return jsonify({"results": results, "strategy": strategy})


@app.route('/portfolio', methods=['POST'])
def portfolio():
    holdings = request.get_json(force=True).get("holdings", [])
    items = []
    for h in holdings:
        code, _ = resolve_stock_input(h.get("code", ""))
        if not code: continue
        a = StockAnalyzer(code, h.get("name", code), h.get("cost"))
        if not a.fetch_data() or not a.calc_indicators(): continue
        rep = a.generate_report()
        items.append({
            "code": rep["code"], "name": rep["name"], "price": rep["price"],
            "pnl": round((rep["price"] - safe_float(h.get("cost", 0))) / max(safe_float(h.get("cost", 0)), 0.01) * 100, 2),
            "weight": h.get("weight", 0), "state": rep["state"], "score": rep["score"],
            "stop": rep["stop"]
        })
    return jsonify({"items": items, "advice": "关注高位分歧个股" if any(i["state"] == "高位放量分歧" for i in items) else "组合稳健"})


@app.route('/backtest', methods=['POST'])
def backtest():
    data = request.get_json(force=True) or {}
    strategy = data.get("strategy", "dragon_wave")
    test_date = data.get("date", "")
    hold_days = safe_int(data.get("hold_days"), 5)
    pool = safe_int(data.get("pool_size"), 200)
    res = advanced_backtest(strategy, test_date, hold_days, pool)
    return jsonify(res)


# ================== 前端 ==================
HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PRO-QUANT 终极版</title>
    <link href="https://cdn.bootcdn.net/ajax/libs/twitter-bootstrap/5.3.0/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.bootcdn.net/ajax/libs/bootstrap/5.3.0/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
    <style>
        body{background:#0b1320;color:#f1f5f9;font-family:-apple-system,sans-serif}
        .card{background:#132239;border:1px solid #2d3a52;border-radius:16px;margin-bottom:18px}
        .card-header{background:#0b1525;border-bottom:1px solid #2d3a52;padding:14px 20px;font-weight:700;border-radius:16px 16px 0 0}
        input,select,button{border-radius:10px;padding:10px;margin:5px 0;background:#0a1525;border:1px solid #334155;color:white;width:100%}
        button{background:linear-gradient(90deg,#3b82f6,#2dd4bf);color:#000;font-weight:bold;cursor:pointer}
        .terminal{background:#020617;color:#a7f3d0;padding:16px;border-radius:12px;font-family:Consolas,monospace;white-space:pre-wrap;font-size:14px}
        .up{color:#ef4444}.down{color:#10b981}
        .grid{display:grid;grid-template-columns:300px 1fr;gap:20px;max-width:1600px;margin:auto;padding:20px}
    </style>
</head>
<body>
<nav class="navbar navbar-dark px-4" style="background:#020617;border-bottom:1px solid #2d3a52">
    <span style="font-size:22px;font-weight:800;color:#3b82f6">🛰️ PRO-QUANT 真实情绪 + 智能回测</span>
</nav>
<div class="grid">
    <div>
        <div class="card">
            <div class="card-header">🌍 大盘气候</div>
            <div id="climateBox" class="p-3">加载中...</div>
        </div>
        <div class="card">
            <div class="card-header">🎯 个股分析</div>
            <div class="p-3">
                <input id="stockInput" placeholder="代码/名称" value="600519">
                <button onclick="analyze()">全面诊断</button>
            </div>
        </div>
        <div class="card">
            <div class="card-header">📡 策略扫描</div>
            <div class="p-3">
                <select id="scanStrategy">
                    <option value="dragon_wave">🐉 龙回头</option>
                    <option value="short">⚡ 短线突击</option>
                    <option value="washout">🛁 缩量洗盘</option>
                </select>
                <button onclick="scan()">扫描</button>
            </div>
        </div>
        <div class="card">
            <div class="card-header">🧠 多策略回测对比</div>
            <div class="p-3">
                <select id="btStrategy">
                    <option value="dragon_wave">龙回头</option>
                    <option value="short">短线</option>
                    <option value="washout">洗盘</option>
                </select>
                <input type="date" id="btDate">
                <button onclick="runBt()">运行对比</button>
            </div>
        </div>
    </div>
    <div id="display" class="card p-4" style="min-height:700px">
        <p class="text-secondary">等待操作...</p>
    </div>
</div>
<script>
    const display = document.getElementById('display');
    async function analyze(){
        const stock = document.getElementById('stockInput').value;
        const r = await fetch('/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stock})}).then(r=>r.json());
        if(r.error){display.innerHTML=`<div class="text-danger">${r.error}</div>`;return;}
        display.innerHTML = `
            <h3>${r.name} <small>${r.code}</small></h3>
            <span class="${r.change>=0?'up':'down'}" style="font-size:28px">${r.price}  ${r.change>=0?'+':''}${r.change}%</span>
            <table class="mt-3"><tr><td>状态</td><td>${r.state}</td></tr><tr><td>RFI</td><td>${r.rfi}</td></tr><tr><td>评分</td><td>${r.score}</td></tr></table>
            ${r.dragon?'<div class="text-warning mt-2">🐉 龙回头信号激活</div>':''}
            <div id="chart" style="height:420px"></div>`;
        if(r.chart) drawChart(r.chart);
    }
    function drawChart(data){
        const options = {
            series: [
                {name:'K线',type:'candlestick',data:data.map(i=>({x:new Date(i.date).getTime(),y:[i.open,i.high,i.low,i.close]}))},
                {name:'MA20',type:'line',data:data.map(i=>({x:new Date(i.date).getTime(),y:i.ma20}))},
                {name:'VOL',type:'column',data:data.map(i=>({x:new Date(i.date).getTime(),y:i.volume,fillColor:i.close>=i.open?'#ef4444':'#10b981'}))}
            ],
            chart:{height:420,background:'transparent'},theme:{mode:'dark'},
            xaxis:{type:'datetime'},yaxis:[{},{show:false},{opposite:true}]
        };
        new ApexCharts(document.getElementById('chart'),options).render();
    }
    async function scan(){
        const s = document.getElementById('scanStrategy').value;
        const r = await fetch('/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({strategy:s})}).then(r=>r.json());
        let html = '<h4>扫描结果</h4>';
        r.results.forEach(i=>html+=`<div class="p-2 border-bottom">${i.name}(${i.code})  ${i.close}  评分${i.score}</div>`);
        display.innerHTML = html;
    }
    async function runBt(){
        const strategy = document.getElementById('btStrategy').value;
        const date = document.getElementById('btDate').value;
        const r = await fetch('/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({strategy,date})}).then(r=>r.json());
        display.innerHTML = `<pre class="terminal">${JSON.stringify(r,null,2)}</pre>`;
    }
    // 气候加载
    (async()=>{
        const c = await fetch('/climate').then(r=>r.json());
        document.getElementById('climateBox').innerHTML = `
            <div>沪深300：${c.close} <span class="${c.change.includes('+')?'up':'down'}">${c.change}</span></div>
            <div>J值：${c.j_val} | 成交：${c.amount}</div>
            <div class="fw-bold">${c.status}</div><div>仓位：${c.position}</div>`;
    })();
    document.getElementById('btDate').value = new Date(Date.now()-7*86400000).toISOString().slice(0,10);
</script>
</body>
</html>
"""

if __name__ == '__main__':
    print("✅ PRO-QUANT 终极版 启动成功")
    print("📍 访问 http://127.0.0.1:10000")
    app.run(host='0.0.0.0', port=10000, debug=False)
