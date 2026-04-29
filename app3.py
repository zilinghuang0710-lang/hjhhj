# -*- coding: utf-8 -*-
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
KLINE_LIMIT = 400
ACTIVE_CACHE_TTL = 30 * 60
MARKET_CACHE_TTL = 10 * 60
MAX_WORKERS = 30

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ================== 网络会话 ==================
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

# ================== 工具函数 ==================
def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        return default if math.isnan(v) or math.isinf(v) else v
    except: return default

def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except: return default

def safe_date(value: Any) -> str:
    text = str(value).strip().replace("/", "-")
    if len(text) == 8 and text.isdigit(): return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text

def market_id_from_code(code: str) -> str:
    return "1" if str(code).startswith(("5", "6", "9")) else "0"

# ================== 活跃股票池 ==================
active_cache = {"data": None, "time": 0.0}

def get_active_stocks(limit: int = 400) -> List[Dict[str, Any]]:
    global active_cache
    now = time.time()
    if active_cache["data"] is not None and (now - active_cache["time"]) < ACTIVE_CACHE_TTL:
        return active_cache["data"][:limit]

    stocks = []
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": str(max(limit, 800)), "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23", "fields": "f12,f14,f100", "fid": "f3",
            "_": str(int(now * 1000))
        }
        resp = HTTP.get(url, params=params, timeout=4)
        if resp.status_code == 200:
            diff = resp.json().get("data", {}).get("diff", [])
            for item in diff:
                code, name = item.get("f12", ""), item.get("f14", "")
                if not re.fullmatch(r"\d{6}", code) or not name or "ST" in name.upper() or "退" in name: continue
                stocks.append({"code": code, "name": name, "sector": item.get("f100", "未分类")})
    except: pass

    if not stocks: # 兜底
        stocks = [{"code":"600519","name":"贵州茅台","sector":"白酒"}]
        
    unique_stocks = list({s['code']: s for s in stocks}.values())
    random.shuffle(unique_stocks)
    active_cache["data"] = unique_stocks
    active_cache["time"] = now
    return unique_stocks[:limit]

def resolve_stock_input(keyword: str) -> Tuple[Optional[str], str]:
    text = str(keyword).strip()
    if re.fullmatch(r"\d{6}", text): return text, text
    try:
        resp = HTTP.get("https://searchapi.eastmoney.com/api/suggest/get", params={"input": text, "type": "14", "token": "D43BF722C8E33BDC906FB84D85E326E8", "count": "1"}, timeout=3)
        if resp.status_code == 200 and resp.json().get("QuotationCodeTable", {}).get("Data"):
            item = resp.json()["QuotationCodeTable"]["Data"][0]
            return item["Code"], item["Name"]
    except: pass
    return text, text

# ================== 核心：市场气候与情绪风向标 ==================
class MarketClimateAnalyzer:
    def get_climate_report(self):
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {"secid": "1.000001", "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61", "klt": "101", "fqt": "1", "end": "20500101", "lmt": "60"}
        try:
            resp = HTTP.get(url, params=params, timeout=4)
            if resp.status_code == 200 and resp.json().get("data"):
                klines = resp.json()["data"]["klines"]
                df = pd.DataFrame([{"close": safe_float(p.split(",")[2]), "high": safe_float(p.split(",")[3]), "low": safe_float(p.split(",")[4]), "volume": safe_float(p.split(",")[5]), "amount": safe_float(p.split(",")[6])} for p in klines])
                
                df['MA20'] = df['close'].rolling(20).mean()
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

                if is_uptrend and 20 <= j_val <= 80: status, color, pos, adv = "🟢 温和做多区", "success", "7成~满仓", "大盘量价齐升，趋势向上，可重仓参与主线龙头或回踩低吸。"
                elif is_uptrend and j_val > 90: status, color, pos, adv = "🔥 情绪高潮区", "warning", "5成", "指数加速赶顶，短线情绪极度亢奋，提防盛极而衰，切忌追高。"
                elif not is_uptrend and j_val < 15: status, color, pos, adv = "🧊 极度恐慌区", "info", "3成", "市场非理性错杀，J值触底，左侧资金可小仓位尝试抄底错杀龙头。"
                elif not is_uptrend: status, color, pos, adv = "🔴 阴跌绞肉机", "danger", "空仓或1成", "大盘破位，系统性风险极大，主力离场，建议空仓休息！"
                else: status, color, pos, adv = "🟡 混沌震荡区", "secondary", "3成~5成", "多空激烈博弈，盘面热点散乱。高抛低吸，不格局，控仓位。"
                
                return {"status": status, "color": color, "position": pos, "advice": adv, "index_price": f"{latest['close']:.2f}", "change": f"{(latest['close']-prev['close'])/prev['close']*100:+.2f}%", "j_val": f"{j_val:.1f}", "news_sentiment": news_sentiment, "amount": f"{est_amount:.0f}亿"}
        except: pass
        return {"status": "未知", "color": "secondary", "advice": "数据获取异常", "position": "--", "index_price": "0.00", "news_sentiment": "未知"}

def fetch_real_sentiment(code: str) -> Dict:
    """真实东财股吧人气接口提取"""
    try:
        mkt = market_id_from_code(code)
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {"secid": f"{mkt}.{code}", "fields": "f136,f137,f139"}
        resp = HTTP.get(url, params=params, timeout=3)
        if resp.status_code == 200:
            d = resp.json().get("data", {})
            return {"rank": safe_int(d.get("f136")), "hot": safe_int(d.get("f137")), "posts": safe_int(d.get("f139"))}
    except: pass
    return {}

# ================== 个股研判引擎 ==================
class StockAnalyzer:
    def __init__(self, symbol, name):
        self.symbol = symbol
        self.name = name
        self.df = pd.DataFrame()
        self.sentiment = {}

    def fetch_data(self):
        mkt = market_id_from_code(self.symbol)
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {"secid": f"{mkt}.{self.symbol}", "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61", "klt": "101", "fqt": "1", "end": "20500101", "lmt": str(KLINE_LIMIT)}
        try:
            resp = HTTP.get(url, params=params, timeout=4)
            if resp.status_code == 200 and resp.json().get("data"):
                klines = resp.json()["data"]["klines"]
                self.df = pd.DataFrame([{"date": safe_date(p.split(",")[0]), "open": safe_float(p.split(",")[1]), "close": safe_float(p.split(",")[2]), "high": safe_float(p.split(",")[3]), "low": safe_float(p.split(",")[4]), "volume": safe_float(p.split(",")[5])} for p in klines])
                self.sentiment = fetch_real_sentiment(self.symbol)
                return len(self.df) >= 30
        except: pass
        return False

    def calc_indicators(self):
        df = self.df
        df['MA5'] = df['close'].rolling(5).mean()
        df['MA10'] = df['close'].rolling(10).mean()
        df['MA20'] = df['close'].rolling(20).mean()
        df['VMA5'] = df['volume'].rolling(5).mean()
        df['VMA20'] = df['volume'].rolling(20).mean()
        
        # KDJ
        l9, h9 = df['low'].rolling(9).min(), df['high'].rolling(9).max()
        rsv = np.where((h9 - l9)==0, 50, (df['close']-l9)/(h9 - l9)*100)
        df['K'] = pd.Series(rsv, index=df.index).ewm(com=2).mean()
        df['D'] = df['K'].ewm(com=2).mean()
        df['J'] = 3*df['K'] - 2*df['D']
        self.df = df.fillna(0)

    def get_sentiment_state(self):
        """融合量价 RFI 与真实人气排名的高阶情感分析"""
        latest = self.df.iloc[-1]
        recent = self.df.tail(10)
        volatility = (recent["high"].max() - recent["low"].min()) / (recent["low"].min()+0.01)
        rfi_proxy = min(100, (recent["volume"].mean() / (self.df["volume"].mean()+1)) * volatility * 100)
        
        # 真假情绪融合逻辑
        rank = self.sentiment.get("rank", 9999)
        if 0 < rank <= 100: rfi_proxy = max(rfi_proxy, 90) # 真龙头霸榜
        
        stage = "🧊 冰点/酝酿期 (无人问津)"
        if rfi_proxy > 80 and latest['close'] < latest['MA5']: stage = "⚔️ 分歧/衰退期 (多空互道傻X)"
        elif rfi_proxy > 75 and latest['close'] > latest['MA5']: stage = "🔥 高潮/加速期 (全网沸腾)"
        elif rfi_proxy < 30 and latest['volume'] > self.df.iloc[-2]['volume'] * 1.5: stage = "🌱 发酵/启动期 (先知先觉)"
        return round(rfi_proxy, 1), stage

    def analyze_strategy(self):
        ctx = self.df.iloc[-1]
        df30 = self.df.tail(30)
        
        # 1. 龙回头
        peak_idx = df30["high"].idxmax()
        peak_price = df30.loc[peak_idx]["high"]
        drawdown = (peak_price - ctx["close"]) / (peak_price + 0.01)
        is_dragon = (peak_price / (df30.iloc[0]["close"] + 0.01) > 1.35) and (0.15 <= drawdown <= 0.35) and (ctx["volume"] < df30.loc[peak_idx]["volume"] * 0.45) and (ctx["close"] >= ctx["MA20"])
        
        # 2. 洗盘
        is_wash = (df30["high"].max() / (df30["close"].iloc[0]+0.01) > 1.05) and (0.05 <= drawdown <= 0.15) and (ctx["volume"] < ctx["VMA20"] * 0.8)
        
        # 3. 短线突击
        is_short = ctx["J"] < 35 and ctx["close"] > self.df.iloc[-2]["close"] and ctx["volume"] > ctx["VMA5"] * 1.2
        
        return "dragon_wave" if is_dragon else ("washout" if is_wash else ("short" if is_short else "none"))

    # 生成盘中剧本
    def generate_playbook(self):
        latest = self.df.iloc[-1]
        auction_vol = latest['volume'] * 0.08
        script = f"🎯 【次日操盘剧本推演】\n"
        script += f"➤ 弱转强点火：早盘9:25竞价量若超 {auction_vol/10000:.1f}万手 且涨幅 +1.5%~4%，游资抢筹特征明显，可半路跟随。\n"
        script += f"➤ 盘中洗盘低吸：若平开回踩 {latest['MA5']:.2f} (MA5) 附近，分时呈现'缩量下杀、放量拉回'，为日内绝佳低吸点。\n"
        script += f"➤ 强转弱诱多：若直接高开 >6% 但竞价无量，极易被砸成诱多长上影，持股者逢高兑现，切忌打板。\n"
        return script

# ================== AI 动态回测寻优引擎 ==================
def dynamic_backtest(df, buy_idx, hold_days=5):
    """运用动态退出的平行推演"""
    buy_price = df.iloc[buy_idx]['open']
    if buy_price <= 0: return None
    
    rules = {
        "fixed_5d": {"ret": 0, "days": 0, "name": "固定持仓5天"},
        "ma5_break": {"ret": 0, "days": 0, "name": "跌破MA5出局"},
        "ma10_break": {"ret": 0, "days": 0, "name": "跌破MA10出局"},
        "trailing_15": {"ret": 0, "days": 0, "name": "最高回撤15%止盈"},
        "hybrid": {"ret": 0, "days": 0, "name": "破MA5或回撤15%"}
    }
    
    max_p = buy_price
    for i in range(1, len(df) - buy_idx):
        row = df.iloc[buy_idx + i]
        c, m5, m10 = row['close'], row['MA5'], row['MA10']
        max_p = max(max_p, row['high'])
        dd = (max_p - c) / max_p
        
        if rules["ma5_break"]["days"] == 0 and c < m5: rules["ma5_break"].update({"ret": c/buy_price-1, "days": i})
        if rules["ma10_break"]["days"] == 0 and c < m10: rules["ma10_break"].update({"ret": c/buy_price-1, "days": i})
        if rules["trailing_15"]["days"] == 0 and (dd >= 0.15 or c < buy_price*0.85): rules["trailing_15"].update({"ret": c/buy_price-1, "days": i})
        if rules["hybrid"]["days"] == 0 and (c < m5 or dd >= 0.15): rules["hybrid"].update({"ret": c/buy_price-1, "days": i})
        if rules["fixed_5d"]["days"] == 0 and i == hold_days: rules["fixed_5d"].update({"ret": c/buy_price-1, "days": i})
            
    final_c = df.iloc[-1]['close']
    final_d = len(df) - buy_idx - 1
    for v in rules.values():
        if v["days"] == 0: v.update({"ret": final_c/buy_price-1, "days": final_d})
            
    return rules

def run_strategy_scan(strategy: str, top_n: int = 12):
    universe = get_active_stocks(400)
    results = []
    
    def _scan(s):
        az = StockAnalyzer(s['code'], s['name'])
        if az.fetch_data() and az.calc_indicators():
            st = az.analyze_strategy()
            if st == strategy:
                latest = az.df.iloc[-1]
                rfi, _ = az.get_sentiment_state()
                return {"code": s['code'], "name": s['name'], "price": latest['close'], "rfi": rfi, "advice": f"{latest['close']*0.99:.2f} 附近"}
        return None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(_scan, s): s for s in universe}
        for f in as_completed(futures):
            res = f.result()
            if res: results.append(res)
            
    return sorted(results, key=lambda x: x['rfi'], reverse=True)[:top_n]

# ================== Flask 路由 ==================
app = Flask(__name__)

@app.route('/')
def home(): return render_template_string(HTML)

@app.route('/climate', methods=['GET'])
def get_climate():
    return jsonify(MarketClimateAnalyzer().get_climate_report())

@app.route('/analyze', methods=['POST'])
def analyze():
    code, name = resolve_stock_input(request.get_json().get('stock', ''))
    if not code: return jsonify({"error":"输入无效"})
    
    az = StockAnalyzer(code, name)
    if not az.fetch_data() or not az.calc_indicators(): return jsonify({"error":"获取行情失败"})
    
    latest, prev = az.df.iloc[-1], az.df.iloc[-2]
    rfi, stage = az.get_sentiment_state()
    rank_txt = f"人气排名：第 {az.sentiment.get('rank')} 名" if az.sentiment.get('rank', 0) > 0 else "人气排名：百名开外"
    
    # 早盘剧本
    script = az.generate_playbook()
    script += f"\n📡 另类情绪诊断：{stage}\n东财人气排位：{rank_txt}  |  量价狂热指数(RFI)：{rfi}"

    chart_data = [{"date": r['date'], "open": r['open'], "high": r['high'], "low": r['low'], "close": r['close'], "ma20": r['MA20'], "volume": r['volume']} for _, r in az.df.tail(80).iterrows()]
    
    return jsonify({"report": {"name": name, "code": code, "price": latest['close'], "change": (latest['close']-prev['close'])/prev['close']*100, "text_report": script, "chart_data": chart_data}})

@app.route('/scan', methods=['POST'])
def scan():
    strategy = request.get_json().get('strategy', 'dragon_wave')
    results = run_strategy_scan(strategy)
    
    # 彻底修复原先的 JavaScript 字符串反引号 SyntaxError 崩溃问题，改用标准的 Python f-string
    html = '<h5 class="mb-3 text-white border-bottom border-secondary pb-2">🎯 雷达扫描结果</h5><div class="row g-2">'
    if not results: 
        html += '<div class="text-secondary">当前市场环境下未扫描到完全匹配该策略的标的。</div>'
    for r in results:
        html += f"""<div class="col-md-6"><div class="card p-2 stock-card h-100" onclick="quickAnalyze('{r['code']}')">
            <div class="d-flex justify-content-between mb-1"><strong class="text-white">{r['name']}</strong><span class="text-warning small">RFI: {r['rfi']}</span></div>
            <div style="font-size:12px;color:#cbd5e1;">现价: {r['price']} | 建议点位: <span style="color:#fcd34d;">{r['advice']}</span></div>
        </div></div>"""
    html += '</div>'
    return jsonify({"html": html})

@app.route('/portfolio', methods=['POST'])
def portfolio():
    holdings = request.get_json().get("holdings", [])
    txt = "📊 【投资组合穿透风控体检】\n" + "="*40 + "\n"
    for h in holdings:
        code, _ = resolve_stock_input(h.get('code',''))
        if not code: continue
        az = StockAnalyzer(code, code)
        if az.fetch_data() and az.calc_indicators():
            latest = az.df.iloc[-1]
            cost = safe_float(h.get('cost', 0))
            pnl = (latest['close'] - cost) / (cost+0.01) * 100 if cost else 0
            st = az.analyze_strategy()
            txt += f"🔹 {code} | 现价:{latest['close']:.2f} | 盈亏: {pnl:+.2f}%\n"
            txt += f"   形态判定: {'🚨 高位风险' if latest['J']>85 else ('✅ 稳健' if st=='none' else '🔥 异动状态')}\n"
    return jsonify({"html": f"<pre class='terminal-box'>{txt}</pre>"})

@app.route('/backtest', methods=['POST'])
def run_bt():
    code, name = resolve_stock_input(request.get_json().get('stock', ''))
    az = StockAnalyzer(code, name)
    if not az.fetch_data() or not az.calc_indicators(): return jsonify({"error":"数据获取异常"})
    
    df = az.df
    signals = []
    # 模拟寻找符合“缩量回踩”的买点
    for i in range(50, len(df)-5):
        row, prev = df.iloc[i], df.iloc[i-1]
        if row['close'] > row['MA60'] and row['volume'] < row['VMA5'] * 0.8 and row['J'] < 35 and prev['close'] > prev['MA20']:
            signals.append(dynamic_backtest(df, i + 1))
            
    if not signals: return jsonify({"error": "该股历史数据中未触发符合模型的买点信号，无法生成回测。"})
    
    summary = {}
    for st_key in signals[0].keys():
        rets = [s[st_key]["ret"] for s in signals]
        days = [s[st_key]["days"] for s in signals]
        win = sum(1 for r in rets if r > 0) / len(rets) * 100
        avg_ret = sum(rets) / len(rets) * 100
        summary[st_key] = {"name": signals[0][st_key]["name"], "win": win, "ret": avg_ret, "days": sum(days)/len(days), "score": win * avg_ret}
        
    sorted_st = sorted(summary.values(), key=lambda x: x["score"], reverse=True)
    medals = ["🥇", "🥈", "🥉", "4", "5"]
    
    txt = f"📅 【{name}】AI动态平仓策略寻优\n共捕捉历史同类买入信号 {len(signals)} 次，平行宇宙推演结果：\n"
    txt += "="*65 + "\n"
    txt += f"{'排名':<4} | {'退出策略规则':<16} | {'平均收益':<8} | {'历史胜率':<8} | {'平均持仓'}\n"
    txt += "-"*65 + "\n"
    for idx, st in enumerate(sorted_st):
        txt += f"{medals[idx]:<4} | {st['name']:<18} | {st['ret']:>7.2f}% | {st['win']:>7.1f}% | {st['days']:>4.1f} 天\n"
    txt += "="*65 + "\n"
    txt += f"💡 系统寻优结论：对该股执行【{sorted_st[0]['name']}】策略可获得最高资金期望效率。"
    
    return jsonify({"html": f"<pre class='terminal-box'>{txt}</pre>"})

# ================== 前端 UI ==================
HTML = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PRO-QUANT 终极版</title>
    <link href="https://cdn.bootcdn.net/ajax/libs/twitter-bootstrap/5.3.0/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.bootcdn.net/ajax/libs/bootstrap/5.3.0/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
    <style>
        body { background-color: #0b0f19; color: #f1f5f9; font-family: -apple-system, sans-serif; }
        .card { background-color: #1e293b; border: 1px solid #334155; border-radius: 12px; margin-bottom: 20px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.5); }
        .card-header-custom { background-color: #0f172a; border-bottom: 1px solid #334155; padding: 14px 20px; font-weight: 700; border-radius: 12px 12px 0 0; color: #f8fafc; }
        input.form-control { background-color: #334155 !important; color: #ffffff !important; border: 1px solid #475569 !important; border-radius: 8px; }
        .btn-primary { background-color: #3b82f6; border: none; border-radius: 8px; font-weight: 600; } 
        .btn-success { background-color: #10b981; border: none; border-radius: 8px; font-weight: 600; }
        .terminal-box { background-color: #020617; color: #a7f3d0; font-family: 'Consolas', monospace; padding: 15px; border-radius: 8px; border: 1px solid #334155; font-size: 14px; white-space: pre-wrap; line-height: 1.6; }
        .climate-panel { border-radius: 12px; padding: 18px 24px; margin-bottom: 24px; border: 1px solid #334155; background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); }
        .text-up { color: #ef4444 !important; } .text-down { color: #10b981 !important; }
        .stock-card { cursor: pointer; transition: 0.2s; } .stock-card:hover { border-color: #10b981; background-color: #334155; }
        #chart-container { height: 480px; width: 100%; margin-top: 20px; }
    </style>
</head>
<body>
<nav class="navbar navbar-dark mb-4 border-bottom border-secondary" style="background-color: #020617;">
    <div class="container-fluid"><span class="navbar-brand fw-bold" style="color:#3b82f6; font-size: 22px;">🛰️ PRO-QUANT 终极版 (真情绪融合+机器优选)</span></div>
</nav>

<div class="container-fluid px-4">
    <div class="climate-panel d-flex justify-content-between align-items-center" id="climatePanel">
        <div>
            <h4 class="mb-2 text-white"><span class="fs-6 text-secondary">上证指数</span> <span id="idxPrice">--</span> <span id="idxChange" class="fs-6">--</span></h4>
            <div class="text-secondary small">大盘情绪J值: <b id="idxJ" class="text-white">--</b> | 市场流动性: <b id="idxAmount" class="text-white">--</b> | 游资生态: <b id="idxNews" class="text-warning">--</b></div>
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
                <div class="card-header-custom">🎯 个股深度分析 & 盘中剧本</div>
                <div class="card-body">
                    <input type="text" id="s_code" class="form-control mb-3" placeholder="代码/名称 (例: 华电能源)" value="华电能源">
                    <button class="btn btn-primary w-100 py-2" onclick="analyze()">⚡ 生成次日操盘剧本</button>
                </div>
            </div>
            
            <div class="card">
                <div class="card-header-custom">📡 情绪周期雷达</div>
                <div class="card-body">
                    <button class="btn w-100 mb-2 text-white" style="background-color:#b91c1c; border:none;" onclick="scan('dragon_wave')">🐉 龙头二波 (龙回头)</button>
                    <button class="btn btn-outline-warning w-100 mb-2 text-white" onclick="scan('washout')">🛁 缩量洗盘 (黄金坑)</button>
                    <button class="btn btn-outline-info w-100 text-white" onclick="scan('short')">⚡ 短线突击 (一日游)</button>
                </div>
            </div>

            <div class="card">
                <div class="card-header-custom">🛠️ 组合诊断 & AI 动态回测</div>
                <div class="card-body">
                    <div id="ports" class="mb-2">
                        <div class="d-flex gap-1 mb-1"><input class="form-control form-control-sm p_code" placeholder="代码"><input class="form-control form-control-sm p_cost" placeholder="成本"></div>
                    </div>
                    <div class="d-flex justify-content-between mb-3">
                        <button class="btn btn-sm btn-outline-secondary text-white" onclick="addPort()">+ 新增</button>
                        <button class="btn btn-sm btn-info text-white" onclick="calcPort()">持仓体检</button>
                    </div>
                    <hr class="border-secondary">
                    <div class="text-secondary small mb-2">测试多种卖出规则的历史效率对比</div>
                    <button class="btn btn-success w-100 py-2" onclick="runBt()">🧬 启动机器二次优化鉴定</button>
                </div>
            </div>
        </div>

        <div class="col-lg-9">
            <div class="card" style="min-height: 800px;">
                <div class="card-header-custom d-flex justify-content-between">
                    <span>📺 可视化情报看板</span><span id="loader" style="display:none; color:#3b82f6;">引擎推演中...</span>
                </div>
                <div class="card-body" id="displayArea"><div class="text-center mt-5 pt-5" style="color: #475569;"><h2 class="mb-3">系统已就绪</h2><p>请在左侧输入标的，生成盘中交易剧本或启动机器回测优化。</p></div></div>
            </div>
        </div>
    </div>
</div>

<script>
    let kChart = null;
    window.onload = async function() {
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

    function addPort() {
        const div = document.createElement('div'); div.className = 'd-flex gap-1 mb-1';
        div.innerHTML = '<input class="form-control form-control-sm p_code" placeholder="代码"><input class="form-control form-control-sm p_cost" placeholder="成本">';
        document.getElementById('ports').appendChild(div);
    }

    async function analyze() {
        const stock = document.getElementById('s_code').value;
        if(!stock) return;
        if(kChart) { kChart.destroy(); kChart = null; } 
        document.getElementById('loader').style.display = 'block';
        try {
            const data = await fetch('/analyze', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({stock}) }).then(r=>r.json());
            document.getElementById('loader').style.display = 'none';
            if(data.error) { document.getElementById('displayArea').innerHTML = `<div class="alert alert-danger mx-3 mt-3">❌ ${data.error}</div>`; return; }
            
            const r = data.report;
            document.getElementById('displayArea').innerHTML = `
            <div class="d-flex justify-content-between align-items-end mb-4 px-2">
                <h2 class="text-white mb-0 fw-bold">${r.name} <span class="fs-5" style="color:#94a3b8;">${r.code}</span></h2>
                <h1 class="${r.change>=0?'text-up':'text-down'} mb-0 fw-bold">${r.price.toFixed(2)} <span class="fs-4">${r.change>=0?'+':''}${r.change.toFixed(2)}%</span></h1>
            </div>
            <pre class="terminal-box" style="font-size: 16px;">${r.text_report}</pre>
            <div id="chart-container"></div>`;
            setTimeout(() => { renderChart(r.chart_data); }, 100);
        } catch(e) { document.getElementById('loader').style.display = 'none'; }
    }

    function renderChart(data) {
        if(!data || !data.length) return;
        const options = {
            series: [ { name: 'K线', type: 'candlestick', data: data.map(i => ({x: new Date(i.date).getTime(), y: [i.open, i.high, i.low, i.close]})) },
                      { name: 'MA20', type: 'line', data: data.map(i => ({x: new Date(i.date).getTime(), y: i.ma20})) },
                      { name: '成交量', type: 'column', data: data.map(i => ({x: new Date(i.date).getTime(), y: i.volume, fillColor: i.close>=i.open?'#ef4444':'#10b981'})) } ],
            chart: { height: 480, type: 'line', background: 'transparent', toolbar: { show: false } },
            theme: { mode: 'dark' }, stroke: { width: [1, 2, 1], curve: 'smooth' }, colors: ['#fff', '#3b82f6', '#fff'],
            plotOptions: { candlestick: { colors: { upward: '#ef4444', downward: '#10b981' } }, bar: { columnWidth: '80%' } },
            xaxis: { type: 'datetime', labels: { style: { colors: '#94a3b8' } } },
            yaxis: [ { seriesName: 'K线', title: { text: '价格' }, labels: { formatter: val => val ? val.toFixed(2) : "" } }, { show: false },
                     { seriesName: '成交量', opposite: true, title: { text: '成交量' }, labels: { formatter: val => val ? (val/10000).toFixed(0)+'w' : "" } } ],
            grid: { borderColor: '#334155', strokeDashArray: 3 }
        };
        kChart = new ApexCharts(document.querySelector("#chart-container"), options);
        kChart.render();
    }

    async function scan(strategy) {
        document.getElementById('loader').style.display = 'block';
        try {
            const data = await fetch('/scan', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({strategy}) }).then(r=>r.json());
            document.getElementById('loader').style.display = 'none';
            document.getElementById('displayArea').innerHTML = data.html;
        } catch(e) { document.getElementById('loader').style.display = 'none'; }
    }
    
    function quickAnalyze(code) { document.getElementById('s_code').value = code; analyze(); }

    async function calcPort() {
        const rows = document.querySelectorAll('#ports .d-flex');
        const holdings = [];
        rows.forEach(r => {
            const code = r.querySelector('.p_code').value.trim(), cost = r.querySelector('.p_cost').value;
            if(code) holdings.push({code, cost: cost||0});
        });
        document.getElementById('loader').style.display = 'block';
        const data = await fetch('/portfolio', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({holdings}) }).then(r=>r.json());
        document.getElementById('loader').style.display = 'none';
        document.getElementById('displayArea').innerHTML = data.html;
    }

    async function runBt() {
        const stock = document.getElementById('s_code').value;
        if(!stock) return;
        document.getElementById('loader').style.display = 'block';
        try {
            const data = await fetch('/backtest', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({stock}) }).then(r=>r.json());
            document.getElementById('loader').style.display = 'none';
            if(data.error) { document.getElementById('displayArea').innerHTML = `<div class="alert alert-warning mt-3 mx-3">⚠️ ${data.error}</div>`; return; }
            document.getElementById('displayArea').innerHTML = data.html;
        } catch(e) { document.getElementById('loader').style.display = 'none'; }
    }
</script>
</body>
</html>
'''

if __name__ == '__main__':
    print("🚀 PRO-QUANT 终极版已启动！")
    print("👉 请用浏览器访问: http://127.0.0.1:10000")
    app.run(host='0.0.0.0', port=10000)
