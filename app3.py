# -*- coding: utf-8 -*-
"""
PRO-QUANT 终极定稿版 (融合量价、情绪与多维回测)
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
KLINE_LIMIT = 600  # 扩大K线取数，满足长时间回测
ACTIVE_CACHE_TTL = 30 * 60
MARKET_CACHE_TTL = 10 * 60
MAX_SCAN_WORKERS = 16
MAX_BACKTEST_WORKERS = 12
DEFAULT_COST_RATE = 0.0015
MIN_REQUIRED_BARS = 90

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ================== 网络会话 ==================
def create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.35,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=64, pool_maxsize=64)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": random.choice(USER_AGENTS)})
    session.verify = False
    return session

HTTP = create_session()

# ================== 工具函数 ==================
def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except (TypeError, ValueError):
        return default

def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default

def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))

def pct_change(current: float, base: float) -> float:
    if base == 0: return 0.0
    return (current - base) / base * 100

def safe_date(value: Any) -> str:
    text = str(value).strip().replace("/", "-")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text

def market_id_from_code(code: str) -> str:
    return "1" if str(code).startswith(("5", "6", "9")) else "0"

def dedupe_by_code(stocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = {}
    for item in stocks:
        code = str(item.get("code", "")).strip()
        if re.fullmatch(r"\d{6}", code):
            seen[code] = item
    return list(seen.values())

# ================== 活跃股票池（带兜底） ==================
active_stocks_cache = {"data": None, "time": 0.0}

def get_hardcoded_fallback_stocks() -> List[Dict[str, Any]]:
    base = [
        ("600519", "贵州茅台", "白酒"), ("000858", "五粮液", "白酒"),
        ("601318", "中国平安", "保险"), ("600036", "招商银行", "银行"),
        ("300059", "东方财富", "互金"), ("300750", "宁德时代", "电池"),
        ("688981", "中芯国际", "半导体"), ("002475", "立讯精密", "消费电子"),
        ("002594", "比亚迪", "新能源车"), ("600104", "上汽集团", "汽车"),
        ("002230", "科大讯飞", "AI应用"), ("300124", "汇川技术", "工业"),
        ("000063", "中兴通讯", "通信"), ("000001", "平安银行", "银行"),
        ("000002", "万科A", "地产"), ("000333", "美的集团", "家电"),
    ]
    return [{"code": c, "name": n, "sector": s} for c, n, s in base]

def get_active_stocks(limit: int = 300) -> List[Dict[str, Any]]:
    global active_stocks_cache
    now = time.time()
    if active_stocks_cache["data"] is not None and (now - active_stocks_cache["time"]) < ACTIVE_CACHE_TTL:
        return active_stocks_cache["data"][:limit]

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
        resp = HTTP.get(url, params=params, headers={"Referer": "https://quote.eastmoney.com/"}, timeout=(4, 8))
        if resp.status_code == 200:
            diff = resp.json().get("data", {}).get("diff", [])
            for item in diff:
                code, name = str(item.get("f12", "")).strip(), str(item.get("f14", "")).strip()
                if not re.fullmatch(r"\d{6}", code) or not name or "ST" in name.upper() or "退" in name:
                    continue
                stocks.append({"code": code, "name": name, "sector": str(item.get("f100") or "未分类")})
    except Exception as e:
        logger.warning("get_active_stocks failed: %s", e)

    if len(stocks) < 50:
        stocks = get_hardcoded_fallback_stocks()
    stocks = dedupe_by_code(stocks)
    random.shuffle(stocks)
    active_stocks_cache["data"] = stocks
    active_stocks_cache["time"] = now
    return stocks[:limit]

def resolve_stock_input(keyword: str) -> Tuple[Optional[str], str]:
    text = str(keyword).strip()
    if not text: return None, ""
    if re.fullmatch(r"\d{6}", text): return text, text
    try:
        resp = HTTP.get("https://searchapi.eastmoney.com/api/suggest/get", params={
            "input": text, "type": "14", "token": "D43BF722C8E33BDC906FB84D85E326E8", "count": "1"
        }, timeout=(3, 5))
        if resp.status_code == 200:
            rows = resp.json().get("QuotationCodeTable", {}).get("Data", [])
            if rows:
                return str(rows[0].get("Code", "")).strip(), str(rows[0].get("Name") or text)
    except: pass
    for item in get_hardcoded_fallback_stocks():
        if text in item["name"] or text == item["code"]: return item["code"], item["name"]
    return None, text

# ================== 市场环境与情绪风控 ==================
market_index_cache = {"data": None, "time": 0.0}

def fetch_market_index_df(limit: int = 400) -> Optional[pd.DataFrame]:
    global market_index_cache
    now = time.time()
    if market_index_cache["data"] is not None and now - market_index_cache["time"] < MARKET_CACHE_TTL:
        return market_index_cache["data"].copy()
    try:
        params = {
            "secid": "1.000001", "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101", "fqt": "1", "end": "20500101", "lmt": str(limit)
        }
        resp = HTTP.get("https://push2his.eastmoney.com/api/qt/stock/kline/get", params=params, timeout=(4, 8))
        if resp.status_code == 200:
            rows = resp.json().get("data", {}).get("klines", [])
            parsed = []
            for item in rows:
                p = str(item).split(",")
                if len(p) >= 7:
                    parsed.append({"date": safe_date(p[0]), "close": safe_float(p[2]), "high": safe_float(p[3]), "low": safe_float(p[4]), "volume": safe_float(p[5]), "amount": safe_float(p[6])})
            df = pd.DataFrame(parsed)
            df["date"] = pd.to_datetime(df["date"])
            df["MA20"] = df["close"].rolling(20, min_periods=10).mean()
            df["MA60"] = df["close"].rolling(60, min_periods=30).mean()
            
            l9, h9 = df['low'].rolling(9).min(), df['high'].rolling(9).max()
            rsv = np.where((h9 - l9)==0, 50, (df['close']-l9)/(h9 - l9)*100)
            df['K'] = pd.Series(rsv, index=df.index).ewm(com=2).mean()
            df['D'] = df['K'].ewm(com=2).mean()
            df['J'] = 3*df['K'] - 2*df['D']
            
            market_index_cache["data"] = df.copy()
            market_index_cache["time"] = now
            return df
    except: pass
    return None

def get_market_state(at_date: Optional[str] = None) -> Dict[str, Any]:
    df = fetch_market_index_df()
    if df is None or df.empty:
        return {"state": "震荡", "score": 50.0, "multiplier": 1.0, "close": 0.0, "ma20": 0.0, "amount_est": "未知", "j_val": 50.0}
    working = df.copy()
    if at_date:
        try: working = working[working["date"] <= pd.to_datetime(at_date)]
        except: pass
    if working.empty: return {"state": "震荡", "score": 50.0, "multiplier": 1.0, "close": 0.0, "ma20": 0.0, "amount_est": "未知", "j_val": 50.0}
    
    latest = working.iloc[-1]
    close_price, ma20, ma60, j_val = safe_float(latest["close"]), safe_float(latest["MA20"]), safe_float(latest["MA60"]), safe_float(latest.get("J", 50))
    est_amount = (safe_float(latest.get("amount", 0)) / 0.4) / 100000000  # 预估两市成交额
    
    if close_price > ma20 > ma60: state, multiplier = "强势", 1.05
    elif close_price < ma20 < ma60: state, multiplier = "弱势", 0.75
    else: state, multiplier = "震荡", 1.0
    
    return {
        "state": state, "score": 80.0 if state == "强势" else (35.0 if state == "弱势" else 50.0),
        "multiplier": multiplier, "close": round(close_price, 2), "ma20": round(ma20, 2),
        "j_val": round(j_val, 1), "amount_est": f"{est_amount:.0f}亿" if est_amount>0 else "未知"
    }

def fetch_stock_popularity(code: str) -> Dict[str, float]:
    try:
        mkt = market_id_from_code(code)
        resp = HTTP.get("https://push2.eastmoney.com/api/qt/stock/get", params={"secid": f"{mkt}.{code}", "fields": "f136,f137,f139"}, timeout=(3, 5))
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            if data: return {"rank": safe_int(data.get("f136", 0)), "hot": safe_int(data.get("f137", 0)), "posts": safe_int(data.get("f139", 0))}
    except: pass
    return {}

# ================== 技术指标与交易状态 ==================
class StockAnalyzer:
    def __init__(self, code: str, name: str = "", cost_price: float = None, sector: str = ""):
        self.code = code
        self.name = name or code
        self.cost_price = safe_float(cost_price) if cost_price is not None else None
        self.sector = sector
        self.df = pd.DataFrame()
        self.popularity = {}
        self.volume_price_peak = 0.0
        self.weekly_state = "周线数据不足"
        self.last_error = ""

    def fetch_data(self, end_date: Optional[str] = None, limit: int = KLINE_LIMIT) -> bool:
        self.last_error = ""
        if not re.fullmatch(r"\d{6}", self.code): return False
        
        try:
            mkt = market_id_from_code(self.code)
            params = {
                "secid": f"{mkt}.{self.code}", "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101", "fqt": "1", "end": end_date if end_date else "20500101", "lmt": str(limit)
            }
            resp = HTTP.get("https://push2his.eastmoney.com/api/qt/stock/kline/get", params=params, timeout=(4, 8))
            if resp.status_code == 200 and resp.json().get("data"):
                klines = resp.json()["data"]["klines"]
                parsed = []
                for item in klines:
                    p = item.split(",")
                    parsed.append({
                        "date": safe_date(p[0]), "open": safe_float(p[1]), "close": safe_float(p[2]),
                        "high": safe_float(p[3]), "low": safe_float(p[4]), "volume": safe_float(p[5]),
                        "amount": safe_float(p[6]), "turnover": safe_float(p[10])
                    })
                if len(parsed) >= MIN_REQUIRED_BARS:
                    self.df = pd.DataFrame(parsed)
                    self.df["date"] = pd.to_datetime(self.df["date"], errors="coerce")
                    self.df = self.df.dropna(subset=["date"]).reset_index(drop=True)
                    self.popularity = fetch_stock_popularity(self.code)
                    return True
        except: pass
        self.last_error = "行情获取失败或数据不足"
        return False

    def calculate_indicators(self) -> bool:
        if self.df.empty or len(self.df) < MIN_REQUIRED_BARS: return False
        df = self.df.copy()
        for w in [5, 10, 20, 60]:
            df[f"MA{w}"] = df["close"].rolling(w, min_periods=max(3, w//2)).mean()
        df["VMA5"] = df["volume"].rolling(5, min_periods=3).mean()
        df["VMA20"] = df["volume"].rolling(20, min_periods=8).mean()
        
        low_n, high_n = df["low"].rolling(9).min(), df["high"].rolling(9).max()
        spread = (high_n - low_n).replace(0, np.nan)
        df["K"] = (((df["close"] - low_n) / spread * 100).fillna(50)).ewm(com=2).mean()
        df["D"] = df["K"].ewm(com=2).mean()
        df["J"] = 3 * df["K"] - 2 * df["D"]
        
        df["Support"] = df["low"].shift(1).rolling(20, min_periods=10).min()
        df["Resistance"] = df["high"].shift(1).rolling(20, min_periods=10).max()
        
        # 筹码峰
        recent = df.tail(60).copy()
        if not recent.empty and recent["close"].max() > recent["close"].min():
            bins = np.linspace(recent["close"].min(), recent["close"].max(), 18)
            cuts = pd.cut(recent["close"], bins=bins, include_lowest=True)
            dist = recent.groupby(cuts, observed=False)["volume"].sum()
            self.volume_price_peak = safe_float(dist.idxmax().mid) if not dist.empty else recent["close"].iloc[-1]
            
        self.df = df.replace([np.inf, -np.inf], np.nan).ffill().fillna(0.0).reset_index(drop=True)
        return True

    def _ctx(self, idx: int = -1) -> Dict:
        row = self.df.iloc[idx]
        prev = self.df.iloc[idx - 1] if idx > 0 else row
        g, gp = lambda k: safe_float(row.get(k)), lambda k: safe_float(prev.get(k))
        close, high60, low60 = g("close"), self.df["high"].iloc[max(0, idx-59):idx+1].max(), self.df["low"].iloc[max(0, idx-59):idx+1].min()
        return {
            "close": close, "open": g("open"), "high": g("high"), "low": g("low"), "prev_close": gp("close"),
            "ma5": g("MA5"), "ma10": g("MA10"), "ma20": g("MA20"), "ma60": g("MA60"),
            "vma5": g("VMA5"), "vma20": g("VMA20"), "volume": g("volume"), "amount": g("amount"),
            "j": g("J"), "prev_j": gp("J"), "k": g("K"), "prev_k": gp("K"),
            "support": g("Support") if g("Support") > 0 else close * 0.95,
            "resistance": g("Resistance") if g("Resistance") > 0 else close * 1.05,
            "high60": high60, "low60": low60, "rise_60": (high60 / low60 - 1) if low60 > 0 else 0,
            "pullback": (high60 - close) / high60 if high60 > 0 else 0,
            "bullish": close > g("open"),
        }

    def sentiment(self) -> Tuple[float, str]:
        if len(self.df) < 10: return 50.0, "数据不足"
        recent = self.df.tail(10)
        volatility = (recent["high"].max() - recent["low"].min()) / max(recent["low"].min(), 0.01)
        rfi = min(100, recent["volume"].mean() / max(self.df["volume"].mean(), 1) * volatility * 100)
        rank = self.popularity.get("rank", 9999)
        if 0 < rank <= 100: rfi = max(rfi, 90) # 真龙头人气霸榜
        stage = "🧊 冰点/酝酿" if rfi < 30 else ("🔥 高潮/分歧" if rfi > 80 else "📈 加速/升温" if rfi > 60 else "⚖️ 温和修复")
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
        if ctx["rise_60"] > 0.4 and ctx["j"] > 90 and ctx["volume"] > ctx["vma20"] * 1.5: return "高位放量分歧"
        if ctx["close"] > ctx["high60"] * 0.98 and ctx["volume"] > ctx["vma20"] * 1.3 and ctx["close"] > ctx["ma20"]: return "趋势突破"
        if ctx["rise_60"] > 0.15 and ctx["close"] > ctx["ma60"] and 0.05 <= ctx["pullback"] <= 0.18:
            if ctx["volume"] < ctx["vma20"] * 0.7:
                return "回踩确认" if ctx["bullish"] and ctx["volume"] > ctx["vma5"] else "缩量回踩观察"
        return "弱势反抽" if ctx["j"] > ctx["prev_j"] and ctx["prev_j"] < 30 else "震荡观察"

    def generate_playbook(self, ctx) -> str:
        auction_vol = ctx['volume'] * 0.08
        playbook = f"🎯 【次日竞价与盘中剧本推演】\n"
        playbook += f"➤ 弱转强点火：早盘9:25竞价量若超 {auction_vol/10000:.1f}万手 且涨幅 +1.5%~4%，游资抢筹特征明显，可半路跟随。\n"
        playbook += f"➤ 盘中洗盘低吸：若平开回踩 {ctx['ma5']:.2f} (MA5) 附近，分时呈现'缩量下杀、放量拉回'，为日内绝佳低吸点。\n"
        playbook += f"➤ 强转弱诱多：若直接高开 >6% 但竞价无量，极易被砸成诱多长上影，持股者逢高兑现，切忌打板。"
        return playbook

    def generate_report(self, market: Optional[Dict] = None) -> Dict:
        if not self.calculate_indicators(): return {"error": self.last_error or "指标计算失败"}
        market = market or get_market_state()
        ctx = self._ctx()
        state = self.detect_state()
        rfi, sent_stage = self.sentiment()
        dragon, dscore = self.is_dragon_wave()
        
        support, resistance = ctx["support"], ctx["resistance"]
        stop = max(support * 0.98, ctx["close"] * 0.92)
        target = max(resistance, ctx["close"] * 1.05)
        rr = (target - ctx["close"]) / (ctx["close"] - stop) if ctx["close"] > stop else 0
        
        final_score = clamp(0.2 * market["score"] + 40 + (15 if ctx["close"] > ctx["ma20"] else 0) + (10 if ctx["golden_cross"] else 0))
        if dragon: final_score = max(final_score, 90)

        chart = []
        for _, row in self.df.tail(120).iterrows():
            chart.append({
                "date": row["date"].strftime("%Y-%m-%d"),
                "open": safe_float(row["open"]), "high": safe_float(row["high"]),
                "low": safe_float(row["low"]), "close": safe_float(row["close"]),
                "ma20": safe_float(row.get("MA20")), "volume": safe_float(row["volume"])
            })

        playbook_text = self.generate_playbook(ctx)
        
        rank_str = f"第 {self.popularity.get('rank')} 名" if self.popularity.get('rank', 0) > 0 else "百名开外"
        text_report = f"📡 真实情绪诊断：{sent_stage}\n东财股吧人气排位：{rank_str}  |  量价狂热指数(RFI)：{rfi}/100\n\n{playbook_text}"

        return {
            "code": self.code, "name": self.name, "price": round(ctx["close"], 2),
            "change": round(pct_change(ctx["close"], ctx["prev_close"]), 2),
            "state": state, "rfi": rfi, "sent_stage": sent_stage,
            "dragon": dragon, "score": round(final_score, 1),
            "support": round(support, 2), "resistance": round(resistance, 2),
            "stop": round(stop, 2), "target": round(target, 2), "rr": round(rr, 2),
            "market": market["state"], "text_report": text_report, "chart": chart
        }

# ================== 策略扫描与高级回测 ==================
def scan_stocks(strategy: str, top_n: int = 12, pool_size: int = 300) -> Dict[str, Any]:
    market = get_market_state()
    universe = get_active_stocks(pool_size)
    results = []

    def _scan(s):
        if "ST" in s["name"].upper(): return None
        a = StockAnalyzer(s["code"], s["name"])
        if not a.fetch_data() or not a.calculate_indicators(): return None
        ctx = a._ctx()
        if ctx["amount"] < 1e8: return None
        
        passed, score = False, 0
        if strategy == "dragon_wave": passed, score = a.is_dragon_wave()
        elif strategy == "short": 
            if ctx["j"] < 35 and ctx["bullish"] and ctx["volume"] > ctx["vma5"] * 1.1: passed, score = True, 70
        elif strategy == "washout":
            # BUG FIX: changed `between` to standard python syntax
            if ctx["rise_60"] > 0.06 and 0.05 <= ctx["pullback"] <= 0.15 and ctx["volume"] < ctx["vma20"] * 0.8: passed, score = True, 75
            
        if not passed: return None
        rfi, _ = a.sentiment()
        return {"code": a.code, "name": a.name, "close": round(ctx["close"], 2), "score": score, "rfi": rfi, "advice": f"{ctx['close']*0.99:.2f}"}

    with ThreadPoolExecutor(max_workers=MAX_SCAN_WORKERS) as ex:
        futures = {ex.submit(_scan, s): s for s in universe}
        for f in as_completed(futures):
            res = f.result()
            if res: results.append(res)
            
    results.sort(key=lambda x: x["rfi"], reverse=True) # 情绪为主导
    return {"strategy": strategy, "market_state": market["state"], "results": results[:top_n]}

def advanced_backtest(strategy: str, test_date: str, hold_days: int = 5, pool_size: int = 200) -> Dict:
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
        if not a.fetch_data(limit=800) or not a.calculate_indicators(): return None
        df = a.df
        trade_dates = df["date"].dt.strftime("%Y-%m-%d")
        valid_idx = df.index[trade_dates <= test_date].tolist()
        if not valid_idx: return None
        signal_idx = valid_idx[-1]
        if signal_idx < 50: return None
        ctx = a._ctx(signal_idx)
        if ctx["amount"] < 1e8: return None
        
        passed = False
        if strategy == "dragon_wave": passed = a.is_dragon_wave()[0]
        elif strategy == "short": passed = ctx["j"] < 35
        elif strategy == "washout": passed = (ctx["rise_60"] > 0.06 and 0.05 <= ctx["pullback"] <= 0.15 and ctx["volume"] < ctx["vma20"] * 0.8)
        
        if not passed: return None
        buy_idx = signal_idx + 1
        if buy_idx >= len(df): return None
        buy_price = df.iloc[buy_idx]["open"]
        
        res = {}
        for rule, check_fn in exit_rules.items():
            max_p, sell_idx = buy_price, buy_idx + 1
            while sell_idx < len(df):
                row = df.iloc[sell_idx]
                close_p = safe_float(row["close"])
                max_p = max(max_p, close_p)
                temp = {"close": close_p, "ma5": safe_float(row["MA5"]), "ma10": safe_float(row["MA10"])}
                if rule == "fixed":
                    if (sell_idx - buy_idx) >= hold_days: break
                else:
                    if check_fn({"max_price": max_p}, temp): break
                sell_idx += 1
            if sell_idx >= len(df): continue
            sell_price = df.iloc[sell_idx]["close"]
            if sell_price <= 0: continue
            res[rule] = {"ret": (sell_price / buy_price - 1 - DEFAULT_COST_RATE) * 100, "days": sell_idx - buy_idx}
        return res

    with ThreadPoolExecutor(max_workers=MAX_BACKTEST_WORKERS) as ex:
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
        ret_vals = np.array([x["ret"] for x in rets])
        days_vals = np.array([x["days"] for x in rets])
        perf[k] = {
            "count": len(ret_vals),
            "win_rate": round(float((ret_vals > 0).mean()) * 100, 1),
            "avg_return": round(float(ret_vals.mean()), 2),
            "avg_days": round(float(days_vals.mean()), 1)
        }
    
    # 评选最佳：胜率 * 平均收益
    best = max(perf.items(), key=lambda kv: kv[1]["avg_return"] * kv[1]["win_rate"]) if perf else None
    return {
        "strategy": strategy, "test_date": test_date, "total_signals": total,
        "rules": perf, "best_rule": best[0] if best else "N/A",
        "suggestion": f"建议使用「{best[0]}」退出规则" if best else "无数据"
    }

# ================== Flask 路由 ==================
app = Flask(__name__)

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/climate')
def climate():
    state = get_market_state()
    return jsonify(state)

@app.route('/analyze', methods=['POST'])
def analyze():
    data = request.get_json(force=True) or {}
    code, name = resolve_stock_input(data.get("stock", ""))
    if not code: return jsonify({"error": "无法识别股票"})
    a = StockAnalyzer(code, name, data.get("cost"))
    if not a.fetch_data(): return jsonify({"error": a.last_error or "行情获取失败"})
    rep = a.generate_report()
    return jsonify(rep)

@app.route('/scan', methods=['POST'])
def scan():
    data = request.get_json(force=True) or {}
    strategy = data.get("strategy", "dragon_wave")
    top_n = min(safe_int(data.get("top_n"), 12), 30)
    results = scan_stocks(strategy, top_n)
    return jsonify(results)

@app.route('/backtest', methods=['POST'])
def backtest():
    data = request.get_json(force=True) or {}
    strategy = data.get("strategy", "dragon_wave")
    test_date = data.get("date", "")
    hold_days = safe_int(data.get("hold_days"), 5)
    pool_size = safe_int(data.get("pool_size"), 200)
    res = advanced_backtest(strategy, test_date, hold_days, pool_size)
    return jsonify(res)

# ================== 前端 UI 极客终端 ==================
HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PRO-QUANT 旗舰终端</title>
    <link href="https://cdn.bootcdn.net/ajax/libs/twitter-bootstrap/5.3.0/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.bootcdn.net/ajax/libs/bootstrap/5.3.0/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
    <style>
        body{background:#0b1320;color:#f1f5f9;font-family:-apple-system,sans-serif}
        .card{background:#132239;border:1px solid #2d3a52;border-radius:12px;margin-bottom:18px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.5);}
        .card-header{background:#0b1525;border-bottom:1px solid #2d3a52;padding:14px 20px;font-weight:700;border-radius:12px 12px 0 0; color:#3b82f6;}
        input,select,button{border-radius:8px;padding:10px;margin:5px 0;background:#1e293b;border:1px solid #475569;color:white;width:100%}
        input::placeholder { color: #94a3b8 !important; }
        button{background:linear-gradient(90deg,#3b82f6,#2dd4bf);color:#000;font-weight:bold;cursor:pointer;border:none;}
        button:hover{opacity:0.9;}
        .terminal{background:#020617;color:#a7f3d0;padding:16px;border-radius:8px;border:1px solid #334155;font-family:Consolas,monospace;white-space:pre-wrap;font-size:14px;}
        .up{color:#ef4444}.down{color:#10b981}
        .climate-panel{background:linear-gradient(135deg, #0f172a 0%, #1e293b 100%);border-radius:12px;padding:20px;border:1px solid #334155;}
        .grid{display:grid;grid-template-columns:320px 1fr;gap:20px;max-width:1600px;margin:auto;padding:20px}
        .stock-card { cursor: pointer; transition: 0.2s; background:#1e293b; border:1px solid #334155; border-radius:8px; padding:10px;} 
        .stock-card:hover { border-color: #10b981; background-color: #334155; }
    </style>
</head>
<body>
<nav class="navbar navbar-dark px-4" style="background:#020617;border-bottom:1px solid #2d3a52">
    <span style="font-size:22px;font-weight:800;color:#3b82f6">🛰️ PRO-QUANT 旗舰实战版 V20</span>
</nav>
<div class="grid">
    <div>
        <div class="card">
            <div class="card-header">🌍 全局风向标 (上证)</div>
            <div id="climateBox" class="p-3">
                <div class="spinner-border spinner-border-sm text-info" role="status"></div> 评估大盘系统性风险...
            </div>
        </div>
        <div class="card">
            <div class="card-header">🎯 个股深度分析 & 盘中剧本</div>
            <div class="p-3">
                <input id="stockInput" placeholder="代码/名称 (例: 华电能源)" value="华电能源">
                <button onclick="analyze()">生成次日操盘剧本</button>
            </div>
        </div>
        <div class="card">
            <div class="card-header">📡 情绪周期雷达</div>
            <div class="p-3">
                <select id="scanStrategy">
                    <option value="dragon_wave">🐉 龙头二波 (龙回头)</option>
                    <option value="short">⚡ 短线突击 (一日游)</option>
                    <option value="washout">🛁 缩量洗盘 (黄金坑)</option>
                </select>
                <button onclick="scan()">雷达全网扫描</button>
            </div>
        </div>
        <div class="card">
            <div class="card-header">🧠 AI 动态回测寻优</div>
            <div class="p-3">
                <select id="btStrategy">
                    <option value="dragon_wave">龙回头策略</option>
                    <option value="short">短线策略</option>
                    <option value="washout">洗盘策略</option>
                </select>
                <label class="text-secondary small mt-2">指定日期 (默认近期)</label>
                <input type="date" id="btDate">
                <button onclick="runBt()">启动机器二次优化鉴定</button>
            </div>
        </div>
    </div>
    
    <div class="card p-4" style="min-height:800px; background:#0f172a;">
        <div id="loader" style="display:none; color:#3b82f6;" class="mb-3">
            <div class="spinner-border spinner-border-sm mr-2" role="status"></div> 核心算力调度中...
        </div>
        <div id="displayArea">
            <div class="text-center mt-5 pt-5 text-secondary">
                <h2 class="mb-3">系统已就绪</h2>
                <p>请在左侧输入标的，生成盘中交易剧本或启动机器回测优化。</p>
            </div>
        </div>
    </div>
</div>
<script>
    const display = document.getElementById('displayArea');
    const loader = document.getElementById('loader');
    let chartObj = null;

    // 气候初始化
    (async()=>{
        try {
            const c = await fetch('/climate').then(r=>r.json());
            let statusColor = c.state === '强势' ? '#10b981' : (c.state === '弱势' ? '#ef4444' : '#f59e0b');
            document.getElementById('climateBox').innerHTML = `
                <div class="d-flex justify-content-between mb-2">
                    <span class="fs-5 fw-bold">${c.close}</span>
                    <span class="fs-6 fw-bold" style="color:${statusColor}">${c.state}</span>
                </div>
                <div class="small text-secondary mb-2">大盘情绪J值: <b class="text-white">${c.j_val}</b> | 预估两市成交: <b class="text-white">${c.amount_est}</b></div>
            `;
        } catch(e) { document.getElementById('climateBox').innerHTML = "大盘数据获取失败"; }
    })();

    document.getElementById('btDate').value = new Date(Date.now()-7*86400000).toISOString().slice(0,10);

    async function analyze(){
        const stock = document.getElementById('stockInput').value;
        if(!stock) return;
        loader.style.display = 'block';
        display.innerHTML = '';
        if(chartObj) { chartObj.destroy(); chartObj = null; }
        
        try {
            const r = await fetch('/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stock})}).then(r=>r.json());
            loader.style.display = 'none';
            if(r.error){display.innerHTML=`<div class="alert alert-danger">❌ ${r.error}</div>`;return;}
            
            let color = r.change >= 0 ? 'up' : 'down';
            display.innerHTML = `
                <div class="d-flex justify-content-between align-items-end mb-4 border-bottom border-secondary pb-3">
                    <h2 class="text-white mb-0">${r.name} <span class="fs-5 text-secondary">${r.code}</span></h2>
                    <h1 class="${color} mb-0 fw-bold">${r.price.toFixed(2)} <span class="fs-5">${r.change>=0?'+':''}${r.change}%</span></h1>
                </div>
                <div class="row text-center mb-4">
                    <div class="col-3"><div class="p-2" style="background:#1e293b; border-radius:8px;"><div class="small text-secondary">交易状态</div><div class="fw-bold text-info">${r.state}</div></div></div>
                    <div class="col-3"><div class="p-2" style="background:#1e293b; border-radius:8px;"><div class="small text-secondary">狂热指数 (RFI)</div><div class="fw-bold text-warning">${r.rfi}</div></div></div>
                    <div class="col-3"><div class="p-2" style="background:#1e293b; border-radius:8px;"><div class="small text-secondary">综合评分</div><div class="fw-bold text-success">${r.score}</div></div></div>
                    <div class="col-3"><div class="p-2" style="background:#1e293b; border-radius:8px;"><div class="small text-secondary">盈亏比预测</div><div class="fw-bold text-white">${r.rr}</div></div></div>
                </div>
                <div class="terminal mb-4">${r.text_report}</div>
                ${r.dragon ? '<div class="alert alert-warning fw-bold">🐉 龙回头形态共振激活！</div>' : ''}
                <div id="chart" style="height:450px"></div>
            `;
            if(r.chart && r.chart.length>0) setTimeout(()=>drawChart(r.chart), 100);
        } catch(e) { loader.style.display = 'none'; }
    }

    function drawChart(data){
        const options = {
            series: [
                { name: 'K线', type: 'candlestick', data: data.map(i => ({x: new Date(i.date).getTime(), y: [i.open, i.high, i.low, i.close]})) },
                { name: 'MA20', type: 'line', data: data.map(i => ({x: new Date(i.date).getTime(), y: i.ma20})) },
                { name: '成交量', type: 'column', data: data.map(i => ({x: new Date(i.date).getTime(), y: i.volume, fillColor: i.close>=i.open?'#ef4444':'#10b981'})) }
            ],
            chart: { height: 450, background: 'transparent', toolbar: { show: false } },
            theme: { mode: 'dark' }, stroke: { width: [1, 2, 1], curve: 'smooth' }, colors: ['#fff', '#3b82f6', '#fff'],
            plotOptions: { candlestick: { colors: { upward: '#ef4444', downward: '#10b981' } }, bar: { columnWidth: '80%' } },
            xaxis: { type: 'datetime', labels: { style: { colors: '#94a3b8' } } },
            yaxis: [
                { seriesName: 'K线', title: { text: '价格', style: { color: '#64748b' } }, labels: { formatter: val => val?val.toFixed(2):'' } },
                { show: false },
                { seriesName: '成交量', opposite: true, title: { text: '成交量', style: { color: '#64748b' } }, labels: { formatter: val => val?(val/10000).toFixed(0)+'w':'' } }
            ],
            grid: { borderColor: '#334155', strokeDashArray: 3 }, legend: { labels: { colors: '#f8fafc' } }
        };
        chartObj = new ApexCharts(document.getElementById('chart'), options);
        chartObj.render();
    }

    async function scan(){
        const strategy = document.getElementById('scanStrategy').value;
        loader.style.display = 'block'; display.innerHTML = '';
        try {
            const r = await fetch('/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({strategy})}).then(r=>r.json());
            loader.style.display = 'none';
            let html = `<h4 class="mb-4 text-white border-bottom border-secondary pb-2">🎯 雷达扫描结果</h4><div class="row g-3">`;
            if(!r.results || r.results.length === 0) html += "<div class='text-secondary'>未扫描到符合模型要求的标的。</div>";
            r.results.forEach(i => {
                html += `<div class="col-md-6"><div class="stock-card h-100" onclick="document.getElementById('stockInput').value='${i.code}';analyze();">
                    <div class="d-flex justify-content-between mb-1"><strong class="text-white fs-5">${i.name}</strong><span class="text-warning small">RFI: ${i.rfi}</span></div>
                    <div style="font-size:13px;color:#cbd5e1;">现价: ${i.close} | 模型建议: <span style="color:#fcd34d;">${i.advice}</span></div>
                </div></div>`;
            });
            html += '</div>';
            display.innerHTML = html;
        } catch(e) { loader.style.display = 'none'; }
    }

    async function runBt(){
        const strategy = document.getElementById('btStrategy').value;
        const date = document.getElementById('btDate').value;
        loader.style.display = 'block'; display.innerHTML = '';
        try {
            const r = await fetch('/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({strategy,date})}).then(r=>r.json());
            loader.style.display = 'none';
            
            let medals = ["🥇", "🥈", "🥉", "4", "5"];
            let sorted_rules = Object.entries(r.rules).sort((a,b) => (b[1].avg_return * b[1].win_rate) - (a[1].avg_return * a[1].win_rate));
            
            let ruleMap = {"fixed_5d": "固定持仓5天", "ma5_break": "跌破MA5出局", "ma10_break": "跌破MA10出局", "trailing_15": "极值回撤15%止盈", "hybrid": "破MA5或回撤15%综合"};
            
            let txt = `📅 AI动态平仓策略寻优\n共捕捉全市场历史同类买入信号 ${r.total_signals} 次，平行宇宙推演结果：\n`;
            txt += "="*65 + "\n";
            txt += "排名 | 退出策略规则       | 平均收益 | 历史胜率 | 平均持仓\n";
            txt += "-"*65 + "\n";
            
            sorted_rules.forEach((item, idx) => {
                let k = item[0], v = item[1];
                let r_name = ruleMap[k] || k;
                txt += `${medals[idx]||' '} | ${r_name.padEnd(14, ' ')} | ${v.avg_return.toFixed(2).padStart(6, ' ')}% | ${v.win_rate.toFixed(1).padStart(6, ' ')}% | ${v.avg_days.toFixed(1).padStart(4, ' ')} 天\n`;
            });
            txt += "="*65 + "\n";
            txt += `💡 系统寻优结论：建议实盘执行【${ruleMap[r.best_rule]||r.best_rule}】退出机制，可获得最高资金期望效率。`;
            
            display.innerHTML = `<h4 class="text-white border-bottom border-secondary pb-3 mb-4">🧬 AI 回测优化报告</h4><div class="terminal">${txt}</div>`;
        } catch(e) { loader.style.display = 'none'; }
    }
</script>
</body>
</html>
"""

if __name__ == '__main__':
    print("✅ PRO-QUANT 旗舰版 V20 启动成功！")
    print("👉 请在浏览器中打开: http://127.0.0.1:10000")
    app.run(host='0.0.0.0', port=10000, debug=False)
