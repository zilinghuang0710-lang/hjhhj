# -*- coding: utf-8 -*-
"""
PRO-QUANT 旗舰实战版 (V21)
核心能力：大盘风控 / RFI情绪共振 / 盘前剧本 / 机器寻优回测 / 极客大屏
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

# ================== 核心配置 ==================
KLINE_LIMIT = 600
ACTIVE_CACHE_TTL = 30 * 60
MARKET_CACHE_TTL = 10 * 60
MAX_SCAN_WORKERS = 16
MAX_BT_WORKERS = 12
DEFAULT_COST_RATE = 0.0015
MIN_REQUIRED_BARS = 90

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ================== 高可用网络池 ==================
def create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=0.35, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=frozenset(["GET", "POST"]))
    adapter = HTTPAdapter(max_retries=retry, pool_connections=64, pool_maxsize=64)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": random.choice(USER_AGENTS), "Referer": "https://quote.eastmoney.com/"})
    session.verify = False
    return session

HTTP = create_session()

# ================== 基础工具集 ==================
def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        return default if math.isnan(v) or math.isinf(v) else v
    except: return default

def safe_int(value: Any, default: int = 0) -> int:
    try: return int(float(value))
    except: return default

def safe_date(value: Any) -> str:
    text = str(value).strip().replace("/", "-")
    if len(text) == 8 and text.isdigit(): return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text

def market_id_from_code(code: str) -> str:
    return "1" if str(code).startswith(("5", "6", "9")) else "0"

def clamp(v, lo=0, hi=100): return max(lo, min(hi, v))

# ================== 活跃资产池 ==================
active_cache = {"data": None, "time": 0.0}

def get_fallback_stocks() -> List[Dict[str, Any]]:
    base = [("600519", "贵州茅台", "白酒"), ("601318", "中国平安", "保险"), ("300059", "东方财富", "互联网金融"), 
            ("300750", "宁德时代", "电池"), ("688981", "中芯国际", "半导体"), ("002594", "比亚迪", "新能源车"),
            ("000001", "平安银行", "银行"), ("600104", "上汽集团", "汽车"), ("002230", "科大讯飞", "AI应用")]
    return [{"code": c, "name": n, "sector": s} for c, n, s in base]

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
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23", "fields": "f12,f14,f100", "fid": "f3",
            "_": str(int(now * 1000))
        }
        resp = HTTP.get(url, params=params, timeout=(4, 8))
        if resp.status_code == 200:
            diff = resp.json().get("data", {}).get("diff", [])
            for item in diff:
                code, name = str(item.get("f12", "")).strip(), str(item.get("f14", "")).strip()
                if not re.fullmatch(r"\d{6}", code) or not name or "ST" in name.upper() or "退" in name: continue
                stocks.append({"code": code, "name": name, "sector": str(item.get("f100") or "未分类")})
    except Exception as e:
        logger.warning(f"获取活跃股票失败: {e}")

    if not stocks: stocks = get_fallback_stocks()
    unique = list({s['code']: s for s in stocks}.values())
    random.shuffle(unique)
    active_cache["data"] = unique
    active_cache["time"] = now
    return unique[:limit]

def resolve_stock_input(keyword: str) -> Tuple[Optional[str], str]:
    text = str(keyword).strip()
    if re.fullmatch(r"\d{6}", text): return text, text
    try:
        resp = HTTP.get("https://searchapi.eastmoney.com/api/suggest/get", params={"input": text, "type": "14", "token": "D43BF722C8E33BDC906FB84D85E326E8", "count": "1"}, timeout=3)
        if resp.status_code == 200 and resp.json().get("QuotationCodeTable", {}).get("Data"):
            return str(resp.json()["QuotationCodeTable"]["Data"][0]["Code"]), str(resp.json()["QuotationCodeTable"]["Data"][0]["Name"])
    except: pass
    for item in get_fallback_stocks():
        if text in item["name"]: return item["code"], item["name"]
    return None, text

def fetch_stock_popularity(code: str) -> Dict[str, Any]:
    """真实东财股吧情绪数据"""
    try:
        mkt = market_id_from_code(code)
        resp = HTTP.get("https://push2.eastmoney.com/api/qt/stock/get", params={"secid": f"{mkt}.{code}", "fields": "f136,f137"}, timeout=3)
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            if data: return {"rank": safe_int(data.get("f136")), "hot": safe_int(data.get("f137"))}
    except: pass
    return {"rank": 0, "hot": 0}

# ================== 大盘风向标 (全局风险控制) ==================
class MarketClimateAnalyzer:
    def get_climate_report(self):
        try:
            params = {"secid": "1.000001", "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61", "klt": "101", "fqt": "1", "end": "20500101", "lmt": "60"}
            resp = HTTP.get("https://push2his.eastmoney.com/api/qt/stock/kline/get", params=params, timeout=4)
            if resp.status_code == 200 and resp.json().get("data"):
                klines = resp.json()["data"]["klines"]
                df = pd.DataFrame([{"close": safe_float(p.split(",")[2]), "high": safe_float(p.split(",")[3]), "low": safe_float(p.split(",")[4]), "amount": safe_float(p.split(",")[6])} for p in klines])
                
                df['MA20'] = df['close'].rolling(20).mean()
                df['MA60'] = df['close'].rolling(60).mean()
                l9, h9 = df['low'].rolling(9).min(), df['high'].rolling(9).max()
                rsv = np.where((h9 - l9)==0, 50, (df['close']-l9)/(h9 - l9)*100)
                df['K'] = pd.Series(rsv, index=df.index).ewm(com=2).mean()
                df['D'] = df['K'].ewm(com=2).mean()
                df['J'] = 3*df['K'] - 2*df['D']
                
                latest, prev = df.iloc[-1], df.iloc[-2]
                is_uptrend = latest['close'] > latest['MA20']
                j_val = latest['J']
                est_amount = (latest['amount'] / 0.4) / 100000000 
                news_sentiment = "🔥 活跃 (资金充沛)" if est_amount > 10000 else ("🧊 萎靡 (存量博弈)" if est_amount < 6000 else "⚖️ 中性 (板块轮动)")

                if is_uptrend and 20 <= j_val <= 80: status, color, pos, adv = "🟢 温和做多区", "success", "7成~满仓", "大盘趋势向上且未过热，逢低积极加仓。"
                elif is_uptrend and j_val > 90: status, color, pos, adv = "🔥 情绪高潮区", "warning", "5成", "指数加速赶顶，短线提防盛极而衰，切忌追高。"
                elif not is_uptrend and j_val < 15: status, color, pos, adv = "🧊 极度恐慌区", "info", "3成", "市场非理性错杀，左侧资金可小仓位尝试抄底。"
                elif not is_uptrend: status, color, pos, adv = "🔴 阴跌绞肉机", "danger", "空仓或1成", "大盘破位阴跌，系统性风险极大，建议空仓休息！"
                else: status, color, pos, adv = "🟡 混沌震荡区", "secondary", "3成~5成", "多空激烈博弈，高抛低吸，控仓位。"
                
                return {"status": status, "color": color, "position": pos, "advice": adv, "index_price": f"{latest['close']:.2f}", "change": f"{(latest['close']-prev['close'])/prev['close']*100:+.2f}%", "j_val": f"{j_val:.1f}", "news_sentiment": news_sentiment, "amount": f"{est_amount:.0f}亿"}
        except: pass
        return {"status": "未知", "color": "secondary", "advice": "数据获取异常", "position": "--", "index_price": "0.00", "news_sentiment": "未知", "amount": "--"}

# ================== 个股深度研判 ==================
class StockAnalyzer:
    def __init__(self, code: str, name: str = ""):
        self.code = code
        self.name = name or code
        self.df = pd.DataFrame()
        self.popularity = {}

    def fetch_data(self) -> bool:
        if not re.fullmatch(r"\d{6}", self.code): return False
        try:
            mkt = market_id_from_code(self.code)
            params = {"secid": f"{mkt}.{self.code}", "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61", "klt": "101", "fqt": "1", "end": "20500101", "lmt": str(KLINE_LIMIT)}
            resp = HTTP.get("https://push2his.eastmoney.com/api/qt/stock/kline/get", params=params, timeout=(4, 8))
            if resp.status_code == 200 and resp.json().get("data"):
                klines = resp.json()["data"]["klines"]
                parsed = []
                for item in klines:
                    p = item.split(",")
                    parsed.append({"date": safe_date(p[0]), "open": safe_float(p[1]), "close": safe_float(p[2]), "high": safe_float(p[3]), "low": safe_float(p[4]), "volume": safe_float(p[5]), "amount": safe_float(p[6]), "turnover": safe_float(p[10])})
                if len(parsed) >= MIN_REQUIRED_BARS:
                    self.df = pd.DataFrame(parsed)
                    self.df["date"] = pd.to_datetime(self.df["date"], errors="coerce")
                    self.df = self.df.dropna(subset=["date"]).reset_index(drop=True)
                    self.popularity = fetch_stock_popularity(self.code)
                    return True
        except: pass
        return False

    def calc_indicators(self):
        df = self.df.copy()
        for w in [5, 10, 20, 60]: df[f"MA{w}"] = df["close"].rolling(w, min_periods=max(3, w//2)).mean()
        df["VMA5"] = df["volume"].rolling(5, min_periods=3).mean()
        df["VMA20"] = df["volume"].rolling(20, min_periods=8).mean()
        
        low_n, high_n = df["low"].rolling(9).min(), df["high"].rolling(9).max()
        rsv = ((df["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100).fillna(50)
        df["K"] = rsv.ewm(com=2).mean()
        df["D"] = df["K"].ewm(com=2).mean()
        df["J"] = 3 * df["K"] - 2 * df["D"]
        
        df["Support"] = df["low"].shift(1).rolling(20, min_periods=10).min()
        df["Resistance"] = df["high"].shift(1).rolling(20, min_periods=10).max()
        self.df = df.replace([np.inf, -np.inf], np.nan).ffill().fillna(0.0).reset_index(drop=True)

    def _ctx(self, idx: int = -1) -> Dict:
        row = self.df.iloc[idx]
        prev = self.df.iloc[idx - 1] if idx > 0 else row
        g, gp = lambda k: safe_float(row.get(k)), lambda k: safe_float(prev.get(k))
        close = g("close")
        high60 = self.df["high"].iloc[max(0, idx-59):idx+1].max()
        low60 = self.df["low"].iloc[max(0, idx-59):idx+1].min()
        return {
            "close": close, "open": g("open"), "high": g("high"), "low": g("low"), "prev_close": gp("close"),
            "ma5": g("MA5"), "ma10": g("MA10"), "ma20": g("MA20"), "ma60": g("MA60"),
            "vma5": g("VMA5"), "vma20": g("VMA20"), "volume": g("volume"), "amount": g("amount"),
            "j": g("J"), "prev_j": gp("J"), "support": g("Support"), "resistance": g("Resistance"),
            "high60": high60, "low60": low60, 
            "rise_60": (high60 / low60 - 1) if low60 > 0 else 0,
            "pullback": (high60 - close) / high60 if high60 > 0 else 0,
            "bullish": close > g("open"),
        }

    def sentiment(self) -> Tuple[float, str]:
        """融合 RFI 与真实人气排名的高阶情感"""
        if len(self.df) < 10: return 50.0, "数据不足"
        recent = self.df.tail(10)
        volatility = (recent["high"].max() - recent["low"].min()) / max(recent["low"].min(), 0.01)
        rfi = min(100, recent["volume"].mean() / max(self.df["volume"].mean(), 1) * volatility * 100)
        
        rank = self.popularity.get("rank", 0)
        if 0 < rank <= 100: rfi = max(rfi, 90) # 真实热榜赋权
        
        latest = self.df.iloc[-1]
        stage = "🧊 冰点/酝酿"
        if rfi > 80 and latest['close'] < latest['MA5']: stage = "⚔️ 衰退/互道傻X"
        elif rfi > 75 and latest['close'] > latest['MA5']: stage = "🔥 高潮/全网沸腾"
        elif rfi < 30 and latest['volume'] > self.df.iloc[-2]['volume'] * 1.5: stage = "🌱 启动/先知先觉"
        return round(rfi, 1), stage

    def detect_state(self, ctx) -> str:
        if ctx["close"] < ctx["ma60"]: return "趋势破坏"
        if ctx["rise_60"] > 0.4 and ctx["j"] > 90 and ctx["volume"] > ctx["vma20"] * 1.5: return "高位放量分歧"
        if ctx["close"] > ctx["high60"] * 0.98 and ctx["volume"] > ctx["vma20"] * 1.3 and ctx["close"] > ctx["ma20"]: return "趋势突破"
        if ctx["rise_60"] > 0.15 and ctx["close"] > ctx["ma60"] and 0.05 <= ctx["pullback"] <= 0.18:
            if ctx["volume"] < ctx["vma20"] * 0.7: return "回踩确认" if ctx["bullish"] and ctx["volume"] > ctx["vma5"] else "缩量回踩观察"
        return "弱势反抽" if ctx["j"] > ctx["prev_j"] and ctx["prev_j"] < 30 else "震荡观察"

    def is_dragon_wave(self) -> Tuple[bool, float]:
        if len(self.df) < 30: return False, 0
        df30 = self.df.tail(30)
        peak_idx = df30["high"].idxmax()
        peak_price = df30.loc[peak_idx]["high"]
        if peak_price / (df30.iloc[0]["close"] + 0.01) < 1.35: return False, 0
        latest = self.df.iloc[-1]
        dd = (peak_price - latest["close"]) / (peak_price + 0.01)
        if not (0.15 <= dd <= 0.35): return False, 0
        if latest["volume"] > df30.loc[peak_idx]["volume"] * 0.45: return False, 0
        if latest["close"] < latest["MA20"]: return False, 0
        return True, 95

    def generate_playbook(self, ctx) -> str:
        auction_vol = ctx['volume'] * 0.08
        playbook = f"🎯 【次日竞价与盘中剧本推演】\n"
        playbook += f"➤ 弱转强点火：早盘9:25竞价量若超 {auction_vol/10000:.1f}万手 且涨幅 +1.5%~4%，游资抢筹特征明显，可跟随。\n"
        playbook += f"➤ 盘中洗盘低吸：若平开回踩 {ctx['ma5']:.2f} (MA5) 附近，分时呈现'缩量下杀、放量拉回'，为日内绝佳低吸点。\n"
        playbook += f"➤ 强转弱诱多：若直接高开 >6% 但竞价无量，极易被砸成诱多长上影，持股者逢高兑现，切忌打板。"
        return playbook

    def generate_report(self) -> Dict:
        if not self.calc_indicators(): return {"error": self.last_error or "计算失败"}
        ctx = self._ctx()
        state = self.detect_state(ctx)
        rfi, sent_stage = self.sentiment()
        dragon, _ = self.is_dragon_wave()
        
        support, resistance = ctx["support"], ctx["resistance"]
        stop = max(support * 0.98, ctx["close"] * 0.92)
        target = max(resistance, ctx["close"] * 1.05)
        rr = (target - ctx["close"]) / (ctx["close"] - stop) if ctx["close"] > stop else 0
        
        # 简单评分模型
        score = clamp(40 + (15 if ctx["close"] > ctx["ma20"] else 0) + (15 if rfi < 30 and ctx["j"] < 30 else 0) + (10 if state == "回踩确认" else 0))
        if dragon: score = max(score, 90)

        playbook = self.generate_playbook(ctx)
        rank_str = f"第 {self.popularity.get('rank')} 名" if self.popularity.get('rank', 0) > 0 else "百名开外"
        text_report = f"📡 真实情绪面：{sent_stage}\n东财人气榜排位：{rank_str}  |  量价狂热代理指数(RFI)：{rfi}/100\n\n{playbook}"

        chart = [{"date": r["date"].strftime("%Y-%m-%d"), "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"], "ma20": r.get("MA20", 0), "volume": r["volume"]} for _, r in self.df.tail(120).iterrows()]

        return {
            "code": self.code, "name": self.name, "price": round(ctx["close"], 2),
            "change": round(pct_change(ctx["close"], ctx["prev_close"]), 2),
            "state": state, "rfi": rfi, "dragon": dragon, "score": round(score, 1),
            "support": round(support, 2), "resistance": round(resistance, 2),
            "stop": round(stop, 2), "target": round(target, 2), "rr": round(rr, 2),
            "text_report": text_report, "chart": chart
        }

# ================== 策略扫描与 AI 机器回测 ==================
def scan_stocks(strategy: str, top_n: int = 12, pool_size: int = 300) -> List[Dict]:
    universe = get_active_stocks(pool_size)
    results = []

    def _scan(s):
        if "ST" in s["name"].upper(): return None
        a = StockAnalyzer(s["code"], s["name"])
        if not a.fetch_data() or not a.calc_indicators(): return None
        ctx = a._ctx()
        if ctx["amount"] < 1e8: return None
        
        passed, score = False, 0
        if strategy == "dragon_wave": passed, score = a.is_dragon_wave()
        elif strategy == "short": 
            if ctx["j"] < 35 and ctx["bullish"] and ctx["volume"] > ctx["vma5"] * 1.1: passed, score = True, 70
        elif strategy == "washout":
            # BUG 彻底修复点：采用标准的 Python 比较运算符
            if ctx["rise_60"] > 0.06 and (0.05 <= ctx["pullback"] <= 0.15) and ctx["volume"] < ctx["vma20"] * 0.8: passed, score = True, 75
            
        if not passed: return None
        rfi, _ = a.sentiment()
        return {"code": a.code, "name": a.name, "close": round(ctx["close"], 2), "score": score, "rfi": rfi, "advice": f"{ctx['close']*0.99:.2f}"}

    with ThreadPoolExecutor(max_workers=MAX_SCAN_WORKERS) as ex:
        futures = {ex.submit(_scan, s): s for s in universe}
        for f in as_completed(futures):
            res = f.result()
            if res: results.append(res)
            
    results.sort(key=lambda x: x["rfi"], reverse=True)
    return results[:top_n]

def advanced_backtest(strategy: str, test_date: str, hold_days=5, pool_size=200) -> Dict:
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

    with ThreadPoolExecutor(max_workers=MAX_BT_WORKERS) as ex:
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
            "name": k,
            "win_rate": round(float((ret_vals > 0).mean()) * 100, 1),
            "avg_return": round(float(ret_vals.mean()), 2),
            "avg_days": round(float(days_vals.mean()), 1)
        }
    
    best = max(perf.items(), key=lambda kv: kv[1]["avg_return"] * kv[1]["win_rate"]) if perf else None
    return {
        "strategy": strategy, "test_date": test_date, "total_signals": total,
        "rules": perf, "best_rule": best[0] if best else "N/A"
    }

# ================== Flask 路由与前端 UI ==================
app = Flask(__name__)

@app.route('/')
def index(): return render_template_string(HTML)

@app.route('/climate')
def climate(): return jsonify(MarketClimateAnalyzer().get_climate_report())

@app.route('/analyze', methods=['POST'])
def analyze():
    code, name = resolve_stock_input(request.get_json(force=True).get("stock", ""))
    if not code: return jsonify({"error": "无法识别股票"})
    a = StockAnalyzer(code, name)
    if not a.fetch_data(): return jsonify({"error": "行情获取失败"})
    return jsonify(a.generate_report())

@app.route('/scan', methods=['POST'])
def scan():
    data = request.get_json(force=True) or {}
    strategy = data.get("strategy", "dragon_wave")
    top_n = min(safe_int(data.get("top_n"), 12), 30)
    return jsonify({"results": scan_stocks(strategy, top_n)})

@app.route('/backtest', methods=['POST'])
def backtest():
    data = request.get_json(force=True) or {}
    strategy = data.get("strategy", "dragon_wave")
    test_date = data.get("date", "")
    return jsonify(advanced_backtest(strategy, test_date, 5, 200))

# 注意：使用标准的 Python 跨行字符串构建 HTML，防止产生注入冲突
HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PRO-QUANT 旗舰终端 V21</title>
    <link href="https://cdn.bootcdn.net/ajax/libs/twitter-bootstrap/5.3.0/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.bootcdn.net/ajax/libs/bootstrap/5.3.0/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
    <style>
        body { background-color: #0b0f19; color: #f1f5f9; font-family: -apple-system, sans-serif; }
        .card { background-color: #1e293b; border: 1px solid #334155; border-radius: 12px; margin-bottom: 20px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.5); }
        .card-header-custom { background-color: #0f172a; border-bottom: 1px solid #334155; padding: 14px 20px; font-weight: 700; border-radius: 12px 12px 0 0; color: #3b82f6; }
        input.form-control, select.form-select { background-color: #334155 !important; color: #ffffff !important; border: 1px solid #475569 !important; border-radius: 8px; }
        input::placeholder { color: #94a3b8 !important; }
        .btn-primary { background: linear-gradient(90deg, #3b82f6, #2dd4bf); color: #000; border: none; border-radius: 8px; font-weight: bold; } 
        .btn-primary:hover { opacity: 0.9; color: #000; }
        .terminal-box { background-color: #020617; color: #a7f3d0; font-family: 'Consolas', monospace; padding: 15px; border-radius: 8px; border: 1px solid #334155; font-size: 15px; white-space: pre-wrap; line-height: 1.6; }
        .climate-panel { border-radius: 12px; padding: 18px 24px; margin-bottom: 24px; border: 1px solid #334155; background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); }
        .text-up { color: #ef4444 !important; } .text-down { color: #10b981 !important; }
        .stock-card { cursor: pointer; transition: 0.2s; background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 12px; } 
        .stock-card:hover { border-color: #10b981; background-color: #334155; }
        #chart-container { height: 480px; width: 100%; margin-top: 20px; }
    </style>
</head>
<body>
<nav class="navbar navbar-dark mb-4 px-4 border-bottom border-secondary" style="background-color: #020617;">
    <span class="navbar-brand fw-bold" style="color:#3b82f6; font-size: 22px;">🛰️ PRO-QUANT 旗舰实战版 V21</span>
</nav>

<div class="container-fluid px-4">
    <div class="climate-panel d-flex justify-content-between align-items-center" id="climatePanel">
        <div>
            <h4 class="mb-2 text-white"><span class="fs-6 text-secondary">上证指数</span> <span id="idxPrice">--</span> <span id="idxChange" class="fs-6">--</span></h4>
            <div class="text-secondary small">情绪J值: <b id="idxJ" class="text-white">--</b> | 预估成交: <b id="idxAmount" class="text-white">--</b> | 游资生态: <b id="idxNews" class="text-warning">--</b></div>
        </div>
        <div class="text-end">
            <h3 class="mb-2 fw-bold" id="climateStatus" style="color: #94a3b8;">系统评估中...</h3>
            <div class="text-white fs-5"><span class="text-secondary">AI风控仓位建议:</span> <b id="climatePos" class="text-warning">--</b></div>
        </div>
    </div>
    <div class="alert alert-dark mb-4 border-secondary text-info" id="climateAdvice" style="background-color: #020617; font-size: 15px;">加载中...</div>

    <div class="row">
        <div class="col-lg-3">
            <div class="card">
                <div class="card-header-custom">🎯 深度分析与盘前剧本</div>
                <div class="card-body">
                    <input type="text" id="s_code" class="form-control mb-3" placeholder="代码/名称 (例: 华电能源)" value="华电能源">
                    <button class="btn btn-primary w-100 py-2" onclick="analyze()">⚡ 生成次日操盘剧本</button>
                </div>
            </div>
            
            <div class="card">
                <div class="card-header-custom">📡 情绪量价共振雷达</div>
                <div class="card-body">
                    <select id="scanStrategy" class="form-select mb-3">
                        <option value="dragon_wave">🐉 龙头二波 (龙回头)</option>
                        <option value="short">⚡ 短线突击 (一日游)</option>
                        <option value="washout">🛁 缩量洗盘 (黄金坑)</option>
                    </select>
                    <button class="btn btn-primary w-100 py-2" onclick="scan()">全网扫描侦测</button>
                </div>
            </div>

            <div class="card">
                <div class="card-header-custom">🧠 AI 退出规则寻优</div>
                <div class="card-body">
                    <label class="text-secondary small mb-1">测试策略在全市场的最佳退出方式</label>
                    <select id="btStrategy" class="form-select mb-2">
                        <option value="dragon_wave">龙回头策略</option>
                        <option value="short">短线脉冲策略</option>
                        <option value="washout">洗盘策略</option>
                    </select>
                    <input type="date" id="btDate" class="form-control mb-3">
                    <button class="btn btn-primary w-100 py-2" onclick="runBt()">启动机器动态鉴定</button>
                </div>
            </div>
        </div>

        <div class="col-lg-9">
            <div class="card" style="min-height: 800px;">
                <div class="card-header-custom d-flex justify-content-between align-items-center">
                    <span>📺 可视化情报看板</span>
                    <span id="loader" style="display:none; color:#3b82f6;"><div class="spinner-border spinner-border-sm mr-2" role="status"></div>核心算力调度中...</span>
                </div>
                <div class="card-body" id="displayArea">
                    <div class="text-center mt-5 pt-5 text-secondary">
                        <h2 class="mb-3">系统已就绪</h2>
                        <p>请在左侧输入标的，生成盘中交易剧本或启动全网扫描优化。</p>
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>

<script>
    let kChart = null;

    // 大盘初始化
    window.onload = async function() {
        document.getElementById('btDate').value = new Date(Date.now() - 7*86400000).toISOString().slice(0, 10);
        try {
            const data = await fetch('/climate').then(r=>r.json());
            document.getElementById('idxPrice').innerText = data.index_price;
            document.getElementById('idxChange').innerText = data.change;
            document.getElementById('idxChange').className = data.change.includes('+') ? 'text-up fs-5 ms-2' : 'text-down fs-5 ms-2';
            document.getElementById('idxJ').innerText = data.j_val;
            document.getElementById('idxAmount').innerText = data.amount;
            document.getElementById('idxNews').innerText = data.news_sentiment;
            document.getElementById('climateStatus').innerText = data.status;
            document.getElementById('climateStatus').style.color = {"success":"#10b981", "warning":"#f59e0b", "danger":"#ef4444", "info":"#38bdf8"}[data.color] || "#fff";
            document.getElementById('climatePos').innerText = data.position;
            document.getElementById('climateAdvice').innerHTML = `<b>💡 大局观风控体系：</b> ${data.advice}`;
        } catch(e) {}
    };

    // 核心个股分析
    async function analyze() {
        const stock = document.getElementById('s_code').value;
        if(!stock) return;
        if(kChart) { kChart.destroy(); kChart = null; } 
        document.getElementById('loader').style.display = 'block';
        try {
            const r = await fetch('/analyze', { method: 'POST', body: JSON.stringify({stock}) }).then(r=>r.json());
            document.getElementById('loader').style.display = 'none';
            if(r.error) { document.getElementById('displayArea').innerHTML = `<div class="alert alert-danger mx-3 mt-3">❌ ${r.error}</div>`; return; }
            
            const color = r.change >= 0 ? 'text-up' : 'text-down';
            document.getElementById('displayArea').innerHTML = `
            <div class="d-flex justify-content-between align-items-end mb-4 px-2">
                <h2 class="text-white mb-0 fw-bold">${r.name} <span class="fs-5 text-secondary">${r.code}</span></h2>
                <h1 class="${color} mb-0 fw-bold">${r.price.toFixed(2)} <span class="fs-4">${r.change>=0?'+':''}${r.change}%</span></h1>
            </div>
            <div class="row text-center mb-4">
                <div class="col-3"><div class="p-2" style="background:#1e293b; border-radius:8px;"><div class="small text-secondary">形态评级</div><div class="fw-bold text-info">${r.state}</div></div></div>
                <div class="col-3"><div class="p-2" style="background:#1e293b; border-radius:8px;"><div class="small text-secondary">狂热指数(RFI)</div><div class="fw-bold text-warning">${r.rfi}</div></div></div>
                <div class="col-3"><div class="p-2" style="background:#1e293b; border-radius:8px;"><div class="small text-secondary">综合评分</div><div class="fw-bold text-success">${r.score}</div></div></div>
                <div class="col-3"><div class="p-2" style="background:#1e293b; border-radius:8px;"><div class="small text-secondary">盈亏比测算</div><div class="fw-bold text-white">${r.rr}</div></div></div>
            </div>
            ${r.dragon ? '<div class="alert alert-warning fw-bold border-0" style="background:#9a3412; color:#fff;">🐉 发现「龙回头」信号！二波预期强烈，注意依托MA20低吸。</div>' : ''}
            <pre class="terminal-box">${r.text_report}</pre>
            <div id="chart-container"></div>`;
            setTimeout(() => { renderChart(r.chart); }, 100);
        } catch(e) { document.getElementById('loader').style.display = 'none'; }
    }

    // 绘制图表
    function renderChart(data) {
        if(!data || !data.length) return;
        const options = {
            series: [
                { name: 'K线', type: 'candlestick', data: data.map(i => ({x: new Date(i.date).getTime(), y: [i.open, i.high, i.low, i.close]})) },
                { name: 'MA20', type: 'line', data: data.map(i => ({x: new Date(i.date).getTime(), y: i.ma20})) },
                { name: '成交量', type: 'column', data: data.map(i => ({x: new Date(i.date).getTime(), y: i.volume, fillColor: i.close>=i.open?'#ef4444':'#10b981'})) }
            ],
            chart: { height: 480, type: 'line', background: 'transparent', toolbar: { show: false } },
            theme: { mode: 'dark' }, stroke: { width: [1, 2, 1], curve: 'smooth' }, colors: ['#fff', '#3b82f6', '#fff'],
            plotOptions: { candlestick: { colors: { upward: '#ef4444', downward: '#10b981' } }, bar: { columnWidth: '80%' } },
            xaxis: { type: 'datetime', labels: { style: { colors: '#94a3b8' } } },
            yaxis: [
                { seriesName: 'K线', title: { text: '价格', style:{color:'#64748b'} }, labels: { formatter: val => val?val.toFixed(2):'' } },
                { show: false },
                { seriesName: '成交量', opposite: true, title: { text: '成交量', style:{color:'#64748b'} }, labels: { formatter: val => val?(val/10000).toFixed(0)+'w':'' } }
            ],
            grid: { borderColor: '#334155', strokeDashArray: 3 }
        };
        kChart = new ApexCharts(document.querySelector("#chart-container"), options);
        kChart.render();
    }

    // 扫描功能
    async function scan() {
        const strategy = document.getElementById('scanStrategy').value;
        document.getElementById('loader').style.display = 'block';
        document.getElementById('displayArea').innerHTML = '';
        try {
            const r = await fetch('/scan', { method: 'POST', body: JSON.stringify({strategy}) }).then(r=>r.json());
            document.getElementById('loader').style.display = 'none';
            let html = `<h4 class="mb-4 text-white border-bottom border-secondary pb-2">🎯 情绪共振雷达扫描结果</h4><div class="row g-3">`;
            if(!r.results || r.results.length === 0) html += "<div class='text-secondary'>当前未扫描到高度匹配的模型标的。</div>";
            r.results.forEach(i => {
                html += `<div class="col-md-6"><div class="stock-card h-100" onclick="document.getElementById('s_code').value='${i.code}';analyze();">
                    <div class="d-flex justify-content-between mb-2"><strong class="text-white fs-5">${i.name}</strong><span class="text-warning small fw-bold">RFI: ${i.rfi}</span></div>
                    <div style="font-size:13px;color:#cbd5e1;">现价: ${i.close} | 建议低吸点位: <span style="color:#fcd34d;">${i.advice}</span></div>
                </div></div>`;
            });
            html += '</div>';
            document.getElementById('displayArea').innerHTML = html;
        } catch(e) { document.getElementById('loader').style.display = 'none'; }
    }

    // 回测寻优功能
    async function runBt() {
        const strategy = document.getElementById('btStrategy').value;
        const date = document.getElementById('btDate').value;
        document.getElementById('loader').style.display = 'block';
        document.getElementById('displayArea').innerHTML = '';
        try {
            const r = await fetch('/backtest', { method: 'POST', body: JSON.stringify({strategy, date}) }).then(r=>r.json());
            document.getElementById('loader').style.display = 'none';
            
            let ruleMap = {"fixed_5d": "固定持仓5天", "ma5_break": "跌破MA5出局", "ma10_break": "跌破MA10出局", "trailing_15": "最大回撤15%止盈", "hybrid": "破MA5或回撤15%"};
            let sorted_rules = Object.entries(r.rules).sort((a,b) => (b[1].avg_return * b[1].win_rate) - (a[1].avg_return * a[1].win_rate));
            let medals = ["🥇", "🥈", "🥉", "4", "5"];
            
            let txt = `📅 AI 全市场动态平仓策略寻优报告\n共捕捉历史同类买入信号 ${r.total_signals} 次，平行宇宙推演结果：\n`;
            txt += "="*70 + "\\n";
            txt += "排名 | 退出策略规则       | 平均收益 | 历史胜率 | 平均持仓\\n";
            txt += "-"*70 + "\\n";
            sorted_rules.forEach((item, idx) => {
                let r_name = ruleMap[item[0]] || item[0]; let v = item[1];
                txt += `${medals[idx]||'  '} | ${r_name.padEnd(14, ' ')} | ${v.avg_return.toFixed(2).padStart(6, ' ')}% | ${v.win_rate.toFixed(1).padStart(6, ' ')}% | ${v.avg_days.toFixed(1).padStart(4, ' ')} 天\\n`;
            });
            txt += "="*70 + "\\n";
            txt += `💡 系统寻优结论：实盘建议执行【${ruleMap[r.best_rule]||r.best_rule}】策略，可获得最高综合资金期望效率。`;
            
            document.getElementById('displayArea').innerHTML = `<h4 class="text-white border-bottom border-secondary pb-3 mb-4">🧬 策略机器优化诊断报告</h4><pre class="terminal-box" style="font-size:15px; color:#60a5fa;">${txt}</pre>`;
        } catch(e) { document.getElementById('loader').style.display = 'none'; }
    }
</script>
</body>
</html>
"""

if __name__ == '__main__':
    print("✅ PRO-QUANT 旗舰版 V21 启动成功！")
    print("👉 浏览器访问: http://127.0.0.1:10000")
    app.run(host='0.0.0.0', port=10000, debug=False)
