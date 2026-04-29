import pandas as pd
import numpy as np
import re
import urllib3
import requests
import time
import random
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, render_template_string

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================== 常量与网络配置 ==================
KLINE_LIMIT = 300 
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

def create_session():
    session = requests.Session()
    retry = urllib3.util.retry.Retry(total=2, backoff_factor=0.2, status_forcelist=[429, 500, 502, 503, 504])
    adapter = requests.adapters.HTTPAdapter(max_retries=retry, pool_connections=150, pool_maxsize=150)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.headers.update({"User-Agent": random.choice(USER_AGENTS)})
    session.verify = False
    return session

http_session = create_session()
active_cache = {"data": None, "time": 0}

# ================== 基础工具函数 ==================
def safe_float(val, default=0.0):
    try:
        v = float(val)
        return default if math.isnan(v) or math.isinf(v) else v
    except: return default

def safe_date(date_str):
    date_str = str(date_str).strip()
    if len(date_str) == 8 and date_str.isdigit():
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    return date_str.replace("/", "-")

def resolve_stock_input(keyword):
    keyword = str(keyword).strip()
    if re.match(r'^\d{6}$', keyword): return keyword, f"A股_{keyword}"
    try:
        resp = http_session.get("https://searchapi.eastmoney.com/api/suggest/get", params={
            "input":keyword,"type":"14","token":"D43BF722C8E33BDC906FB84D85E326E8","count":"1"}, timeout=3)
        if resp.status_code==200 and resp.json().get("QuotationCodeTable", {}).get("Data"):
            item = resp.json()["QuotationCodeTable"]["Data"][0]
            return item["Code"], item["Name"]
    except: pass
    return keyword, keyword 

def get_active_stocks(limit=400):
    global active_cache
    now = time.time()
    if active_cache["data"] is not None and (now - active_cache["time"]) < 1800:
        return active_cache["data"][:limit]

    stocks = []
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": str(max(limit, 800)), "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2",
            "fid": "f20", "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f14,f10,f20,f100", "_": str(int(now*1000))
        }
        resp = http_session.get(url, params=params, timeout=4).json()
        if resp and resp.get("data") and resp["data"].get("diff"):
            for item in resp["data"]["diff"]:
                code, name = item.get("f12"), item.get("f14")
                if not code or "ST" in name or "退" in name: continue
                stocks.append({"code": code, "name": name, "volume_ratio": item.get("f20", 1), "sector": item.get("f100", "")})
    except: pass
        
    unique_stocks = list({s['code']: s for s in stocks}.values())
    random.shuffle(unique_stocks)
    active_cache["data"] = unique_stocks
    active_cache["time"] = now
    return unique_stocks[:limit]

# ================== 大盘情绪与全局风控 ==================
class MarketClimateAnalyzer:
    def __init__(self):
        self.df = pd.DataFrame()
        
    def fetch_index_data(self):
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {"secid": "1.000001", "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61", "klt": "101", "fqt": "1", "end": "20500101", "lmt": "60"}
        try:
            resp = http_session.get(url, params=params, timeout=4)
            if resp.status_code == 200:
                data = resp.json()
                if data and data.get("data") and data["data"].get("klines"):
                    klines = data["data"]["klines"]
                    self.df = pd.DataFrame([{"date": p.split(",")[0], "close": safe_float(p.split(",")[2]), "high": safe_float(p.split(",")[3]), "low": safe_float(p.split(",")[4]), "volume": safe_float(p.split(",")[5]), "amount": safe_float(p.split(",")[6])} for p in klines])
                    return True
        except: pass
        return False
        
    def get_climate_report(self):
        if not self.fetch_index_data() or self.df.empty:
            return {"status": "未知", "color": "secondary", "advice": "数据获取异常", "position": "3成", "index_price": "0.00", "news_sentiment": "中性"}
        
        df = self.df
        df['MA20'] = df['close'].rolling(20).mean()
        df['VMA5'] = df['volume'].rolling(5).mean()
        l9, h9 = df['low'].rolling(9).min(), df['high'].rolling(9).max()
        rsv = np.where((h9 - l9)==0, 50, (df['close']-l9)/(h9 - l9)*100)
        df['K'] = pd.Series(rsv, index=df.index).ewm(com=2).mean()
        df['D'] = df['K'].ewm(com=2).mean()
        df['J'] = 3*df['K'] - 2*df['D']
        
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        
        is_uptrend = latest['close'] > latest['MA20']
        is_vol_active = latest['volume'] > latest['VMA5']
        j_val = latest['J']
        change_pct = (latest['close'] - prev['close']) / prev['close'] * 100
        
        # 预估两市成交额情绪 (假设上证占全市场约40%)
        est_total_amount = (latest['amount'] / 0.4) / 100000000 # 亿元
        news_sentiment = "🔥 活跃 (资金充沛)" if est_total_amount > 10000 else ("🧊 萎靡 (存量博弈)" if est_total_amount < 6000 else "⚖️ 中性 (板块轮动)")

        if is_uptrend and is_vol_active and 20 <= j_val <= 80:
            status, color, pos, adv = "🟢 温和做多区", "success", "7成~满仓", "大盘量价齐升，趋势向上，可重仓参与主线龙头或回踩低吸。"
        elif is_uptrend and j_val > 90:
            status, color, pos, adv = "🔥 情绪高潮区", "warning", "5成", "指数加速赶顶，短线情绪极度亢奋，提防盛极而衰，切忌追高。"
        elif not is_uptrend and j_val < 15:
            status, color, pos, adv = "🧊 极度恐慌区", "info", "3成", "市场非理性错杀，J值触底，左侧资金可小仓位尝试抄底错杀龙头。"
        elif not is_uptrend and not is_vol_active:
            status, color, pos, adv = "🔴 阴跌绞肉机", "danger", "空仓或1成", "大盘破位且缩量，系统性风险极大，主力离场，建议空仓休息！"
        else:
            status, color, pos, adv = "🟡 混沌震荡区", "secondary", "3成~5成", "多空激烈博弈，盘面热点散乱。高抛低吸，不格局，控仓位。"
            
        return {
            "status": status, "color": color, "position": pos, "advice": adv,
            "index_price": f"{latest['close']:.2f}", "change": f"{change_pct:+.2f}%",
            "j_val": f"{j_val:.1f}", "news_sentiment": news_sentiment, "amount": f"{est_total_amount:.0f}亿"
        }

# ================== 个股分析引擎 ==================
class StockAnalyzer:
    def __init__(self, symbol, name):
        self.symbol = symbol
        self.name = name
        self.df = pd.DataFrame()

    def fetch_data(self, start_date=None, end_date=None):
        mkt = "0" if self.symbol.startswith(("0","3")) else "1"
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": f"{mkt}.{self.symbol}",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101", "fqt": "1", "end": "20500101", "lmt": str(KLINE_LIMIT)
        }
        try:
            resp = http_session.get(url, params=params, timeout=4)
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
                df = pd.DataFrame(parsed)
                df['date'] = pd.to_datetime(df['date'])
                
                # 时间截取过滤
                if start_date: df = df[df['date'] >= pd.to_datetime(start_date)]
                if end_date: df = df[df['date'] <= pd.to_datetime(end_date)]
                
                self.df = df.reset_index(drop=True)
                return len(self.df) >= 20
        except: pass
        return False

    def calc_indicators(self):
        df = self.df
        df['MA5'] = df['close'].rolling(5).mean()
        df['MA10'] = df['close'].rolling(10).mean()
        df['MA20'] = df['close'].rolling(20).mean()
        df['VMA5'] = df['volume'].rolling(5).mean()
        
        l9, h9 = df['low'].rolling(9).min(), df['high'].rolling(9).max()
        denom = h9 - l9
        rsv = np.where(denom==0, 50, (df['close']-l9)/denom*100)
        df['K'] = pd.Series(rsv, index=df.index).ewm(com=2).mean()
        df['D'] = df['K'].ewm(com=2).mean()
        df['J'] = 3*df['K'] - 2*df['D']
        self.df = df.fillna(0)

    # 次日盘中操作剧本推演
    def generate_intraday_advice(self):
        latest = self.df.iloc[-1]
        prev = self.df.iloc[-2]
        vol_ratio = latest['volume'] / (latest['VMA5'] + 1)
        
        # 测算爆量标准 (昨天成交量的特定比例)
        auction_vol_strong = latest['volume'] * 0.08  # 竞价量达昨日8%为爆量弱转强
        auction_vol_weak = latest['volume'] * 0.02    # 竞价量不足昨日2%为无量
        
        script = f"🎯 【次日竞价与盘中剧本推演】\n"
        script += f"➤ 强力做多剧本 (弱转强)：早盘9:25竞价成交量若达到 {auction_vol_strong/10000:.1f}万手以上，且竞价涨幅在 +1.5% ~ +4.0% 之间，说明主力资金抢筹，果断跟随。\n"
        script += f"➤ 诱多防守剧本 (强转弱)：若开盘直接秒板或高开 > 6% 但竞价成交量不足 {auction_vol_weak/10000:.1f}万手，极易被砸成诱多长上影，持股者逢高兑现，空仓者切忌打板。\n"
        script += f"➤ 盘中洗盘剧本：若平开或小幅低开回踩 {latest['MA5']:.2f} (MA5均线) 附近，分时图呈现'缩量下杀、放量拉回'，是绝佳的盘中低吸点。\n"
        
        return script

# ================== AI 多维度回测引擎 ==================
def backtest_strategy(df, buy_idx):
    """平行运行5套退出策略，返回各策略的收益"""
    buy_price = df['open'].iloc[buy_idx]
    if buy_price <= 0: return None
    
    results = {
        "fixed_5d": {"return": 0, "days": 0, "sell_reason": "固定5天"},
        "ma5_break": {"return": 0, "days": 0, "sell_reason": "跌破MA5"},
        "ma10_break": {"return": 0, "days": 0, "sell_reason": "跌破MA10"},
        "trailing_15": {"return": 0, "days": 0, "sell_reason": "回撤15%止盈"},
        "hybrid": {"return": 0, "days": 0, "sell_reason": "综合风控退场"}
    }
    
    max_price_since_buy = buy_price
    
    # 模拟未来走势
    for step in range(1, len(df) - buy_idx):
        curr_idx = buy_idx + step
        curr_row = df.iloc[curr_idx]
        curr_close = curr_row['close']
        curr_ma5 = curr_row['MA5']
        curr_ma10 = curr_row['MA10']
        
        max_price_since_buy = max(max_price_since_buy, curr_row['high'])
        drawdown_from_peak = (max_price_since_buy - curr_close) / max_price_since_buy
        
        # 1. 跌破 MA5
        if results["ma5_break"]["days"] == 0 and curr_close < curr_ma5:
            results["ma5_break"] = {"return": curr_close/buy_price - 1, "days": step, "sell_reason": "跌破 MA5"}
            
        # 2. 跌破 MA10
        if results["ma10_break"]["days"] == 0 and curr_close < curr_ma10:
            results["ma10_break"] = {"return": curr_close/buy_price - 1, "days": step, "sell_reason": "跌破 MA10"}
            
        # 3. 移动止盈/止损 15%
        if results["trailing_15"]["days"] == 0 and (drawdown_from_peak >= 0.15 or curr_close < buy_price * 0.85):
            results["trailing_15"] = {"return": curr_close/buy_price - 1, "days": step, "sell_reason": "触发15%风控线"}
            
        # 4. 混合策略 (跌破MA5 或 回撤15%)
        if results["hybrid"]["days"] == 0 and (curr_close < curr_ma5 or drawdown_from_peak >= 0.15):
            results["hybrid"] = {"return": curr_close/buy_price - 1, "days": step, "sell_reason": "复合破位"}

        # 5. 固定5天
        if step == 5 and results["fixed_5d"]["days"] == 0:
            results["fixed_5d"] = {"return": curr_close/buy_price - 1, "days": step, "sell_reason": "固定 5 天"}
            
    # 收尾处理：如果一直没触发，按最后一天收盘价计算
    final_close = df.iloc[-1]['close']
    final_step = len(df) - buy_idx - 1
    for k, v in results.items():
        if v["days"] == 0:
            results[k] = {"return": final_close/buy_price - 1, "days": final_step, "sell_reason": "持有至今"}
            
    return results

def run_advanced_backtest(code, name, start_date):
    az = StockAnalyzer(code, name)
    # 获取从 start_date 至今的数据
    if not az.fetch_data(start_date=start_date) or not az.calc_indicators():
        return {"error": "获取历史数据失败或数据不足"}
        
    df = az.df
    signals = []
    
    # 模拟寻找买点信号 (以缩量回踩/龙回头为例)
    for i in range(30, len(df) - 5): # 留出未来几天计算收益
        row = df.iloc[i]
        prev = df.iloc[i-1]
        
        # 买点逻辑：MA20之上，近期缩量，J值处于低位超卖
        if row['close'] > row['MA20'] and row['volume'] < row['VMA5'] * 0.8 and row['J'] < 30:
            # 找到买点，传入明天开盘的 index 进行回测
            sim_res = backtest_strategy(df, i + 1)
            if sim_res:
                signals.append(sim_res)
                
    if not signals:
        return {"error": f"该区间内 ({start_date} 至今) 未触发符合模型的入场信号。"}
        
    # 聚合汇总各策略表现
    strategy_metrics = {}
    for st_name in ["fixed_5d", "ma5_break", "ma10_break", "trailing_15", "hybrid"]:
        rets = [s[st_name]["return"] for s in signals]
        days = [s[st_name]["days"] for s in signals]
        win_rate = sum(1 for r in rets if r > 0) / len(rets) * 100
        avg_ret = sum(rets) / len(rets) * 100
        avg_days = sum(days) / len(days)
        
        # 核心：计算策略的期望得分 (盈亏比加权)
        expected_value = win_rate * avg_ret 
        
        strategy_metrics[st_name] = {
            "name": st_name,
            "win_rate": win_rate,
            "avg_ret": avg_ret,
            "avg_days": avg_days,
            "score": expected_value
        }
        
    # 排序评优
    sorted_st = sorted(strategy_metrics.values(), key=lambda x: x["score"], reverse=True)
    medals = ["🥇", "🥈", "🥉", "4", "5"]
    
    report_text = f"📅 【{name} ({code})】动态优化回测报告\n"
    report_text += f"测算区间：{start_date} 至今  |  共触发真实买点信号：{len(signals)} 次\n"
    report_text += "=" * 60 + "\n"
    report_text += f"{'排名':<4} | {'策略类型':<14} | {'平均收益':<8} | {'胜率':<8} | {'平均持仓'}\n"
    report_text += "-" * 60 + "\n"
    
    st_mapping = {
        "fixed_5d": "固定持仓 5天", "ma5_break": "跌破 MA5 止盈损",
        "ma10_break": "跌破 MA10 止盈损", "trailing_15": "极值回撤 15%", "hybrid": "破MA5或回撤15%"
    }
    
    for idx, st in enumerate(sorted_st):
        s_name = st_mapping[st['name']]
        report_text += f"{medals[idx]:<4} | {s_name:<12} | {st['avg_ret']:>6.2f}% | {st['win_rate']:>5.1f}% | {st['avg_days']:>4.1f} 天\n"
        
    report_text += "=" * 60 + "\n"
    report_text += f"💡 机器深度优化结论：\n"
    report_text += f"针对【{name}】的股性，历史数据表明采用【{st_mapping[sorted_st[0]['name']]}】的退出机制能够获得最高的资金期望效率。\n"
    report_text += f"建议在实战中严格执行此纪律，克服主观情绪波动。"
    
    return {"report": report_text}

# ================== Flask 后端路由 ==================
app = Flask(__name__)

@app.route('/')
def home(): return render_template_string(HTML)

@app.route('/climate', methods=['GET'])
def get_climate():
    report = MarketClimateAnalyzer().get_climate_report()
    return jsonify(report)

@app.route('/analyze', methods=['POST'])
def analyze():
    d = request.get_json()
    code, name = resolve_stock_input(d.get('stock'))
    if not code: return jsonify({"error":"无法识别股票或股票不存在"})
    
    az = StockAnalyzer(code, name)
    if not az.fetch_data() or not az.calc_indicators(): 
        return jsonify({"error":"数据获取异常，可能是退市/停牌股票"})
        
    latest = az.df.iloc[-1]
    prev = az.df.iloc[-2]
    change = (latest['close'] - prev['close']) / prev['close'] * 100
    
    # 结合大盘风控与盘中剧本
    intraday_script = az.generate_intraday_advice()
    
    txt = f"📊 基本面数据: 收盘 {latest['close']:.2f} | MA5 {latest['MA5']:.2f} | MA20 {latest['MA20']:.2f}\n\n"
    txt += intraday_script
    
    chart_data = []
    for _, row in az.df.tail(80).iterrows():
        chart_data.append({
            "date": row['date'].strftime("%Y-%m-%d"),
            "open": row['open'], "high": row['high'],
            "low": row['low'], "close": row['close'],
            "ma5": row['MA5'], "ma20": row['MA20'], "volume": row['volume']
        })
        
    return jsonify({
        "report": {
            "name": name, "code": code, "price": latest['close'], "change": change,
            "text_report": txt, "chart_data": chart_data
        }
    })

@app.route('/backtest_advanced', methods=['POST'])
def run_bt_advanced():
    d = request.get_json()
    stock_input = d.get('stock')
    start_date = d.get('start_date', '2025-01-01')
    
    code, name = resolve_stock_input(stock_input)
    if not code: return jsonify({"error":"无法识别回测股票"})
    
    res = run_advanced_backtest(code, name, start_date)
    return jsonify(res)

# ================== 极客深色风前端 UI ==================
HTML = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PRO-QUANT V18 终极全功能版</title>
    <link href="https://cdn.bootcdn.net/ajax/libs/twitter-bootstrap/5.3.0/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.bootcdn.net/ajax/libs/bootstrap/5.3.0/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
    <style>
        body { background-color: #0b0f19; color: #f1f5f9; font-family: -apple-system, sans-serif; }
        .card { background-color: #1e293b; border: 1px solid #334155; border-radius: 12px; margin-bottom: 20px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.5); }
        .card-header-custom { background-color: #0f172a; border-bottom: 1px solid #334155; padding: 14px 20px; font-weight: 700; border-radius: 12px 12px 0 0; color: #f8fafc; }
        input.form-control, select.form-select { background-color: #334155 !important; color: #ffffff !important; border: 1px solid #475569 !important; border-radius: 8px; }
        input::placeholder { color: #94a3b8 !important; }
        .btn-primary { background-color: #3b82f6; border: none; border-radius: 8px; font-weight: 600; } 
        .btn-primary:hover { background-color: #2563eb; }
        .btn-success { background-color: #10b981; border: none; border-radius: 8px; font-weight: 600; }
        .terminal-box { background-color: #020617; color: #a7f3d0; font-family: 'Consolas', monospace; padding: 15px; border-radius: 8px; border: 1px solid #334155; font-size: 14px; white-space: pre-wrap; line-height: 1.6; }
        .climate-panel { border-radius: 12px; padding: 18px 24px; margin-bottom: 24px; border: 1px solid #334155; background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.5); }
        .text-up { color: #ef4444 !important; } 
        .text-down { color: #10b981 !important; }
        #chart-container { height: 480px; width: 100%; margin-top: 20px; display: block; position: relative; }
    </style>
</head>
<body>
<nav class="navbar navbar-dark mb-4 border-bottom border-secondary" style="background-color: #020617;">
    <div class="container-fluid"><span class="navbar-brand fw-bold" style="color:#3b82f6; font-size: 22px;">🛰️ PRO-QUANT V18 (动态回测+早盘推演)</span></div>
</nav>

<div class="container-fluid px-4">
    <div class="climate-panel d-flex justify-content-between align-items-center" id="climatePanel">
        <div>
            <h4 class="mb-2 text-white"><span class="fs-6 text-secondary">上证指数</span> <span id="idxPrice" class="fw-bold">--</span> <span id="idxChange" class="fs-6">--</span></h4>
            <div class="text-secondary small">
                情绪指数(J值): <b id="idxJ" class="text-white">--</b> | 
                市场流动性: <b id="idxAmount" class="text-white">--</b> |
                游资活跃度: <b id="idxNews" class="text-warning">--</b>
            </div>
        </div>
        <div class="text-end">
            <h3 class="mb-2 fw-bold" id="climateStatus" style="color: #94a3b8;">系统评估中...</h3>
            <div class="text-white fs-5"><span class="text-secondary">AI建议最高仓位:</span> <b id="climatePos" class="text-warning">--</b></div>
        </div>
    </div>
    <div class="alert alert-dark mb-4 border-secondary text-info" id="climateAdvice" style="background-color: #020617; font-size: 15px;">正在加载全局风控策略...</div>

    <div class="row">
        <div class="col-lg-3">
            <div class="card">
                <div class="card-header-custom">🎯 个股深度分析 & 盘中剧本</div>
                <div class="card-body">
                    <input type="text" id="s_code" class="form-control mb-3" placeholder="输入代码或名称 (例: 华电能源)" value="华电能源">
                    <button class="btn btn-primary w-100 py-2" onclick="analyze()">⚡ 生成次日操盘剧本</button>
                </div>
            </div>
            
            <div class="card">
                <div class="card-header-custom">🛠️ AI 动态退出策略回测优化</div>
                <div class="card-body">
                    <div class="text-secondary small mb-2">测试不同卖出规则在该股的历史表现</div>
                    <input type="text" id="bt_code" class="form-control mb-2" placeholder="回测标的 (默认同上)">
                    <label class="text-secondary small">起始时间 (包含2025至今及2026年数据)</label>
                    <input type="date" id="bt_start_date" class="form-control mb-3" value="2025-01-01">
                    <button class="btn btn-success w-100 py-2" onclick="runAdvancedBt()">🧬 启动机器二次优化鉴定</button>
                </div>
            </div>
        </div>

        <div class="col-lg-9">
            <div class="card" style="min-height: 800px;">
                <div class="card-header-custom d-flex justify-content-between align-items-center">
                    <span>📺 可视化情报看板</span>
                    <span id="loader" style="display:none; color:#3b82f6;">
                        <div class="spinner-border spinner-border-sm mr-2" role="status"></div> 核心算力调度中...
                    </span>
                </div>
                <div class="card-body" id="displayArea">
                    <div class="text-center mt-5 pt-5" style="color: #475569;">
                        <h2 class="mb-3">系统已就绪</h2>
                        <p>请在左侧输入标的，生成盘中交易剧本或启动机器回测优化。</p>
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>

<script>
    window.onload = async function() {
        try {
            const res = await fetch('/climate');
            const data = await res.json();
            document.getElementById('idxPrice').innerText = data.index_price;
            document.getElementById('idxChange').innerText = data.change;
            document.getElementById('idxChange').className = data.change.includes('+') ? 'text-up fs-5 ms-2' : 'text-down fs-5 ms-2';
            document.getElementById('idxJ').innerText = data.j_val;
            document.getElementById('idxAmount').innerText = data.amount;
            document.getElementById('idxNews').innerText = data.news_sentiment;
            
            document.getElementById('climateStatus').innerText = data.status;
            const colorMap = {"success":"#10b981", "warning":"#f59e0b", "danger":"#ef4444", "info":"#38bdf8", "secondary":"#94a3b8"};
            document.getElementById('climateStatus').style.color = colorMap[data.color] || "#fff";
            document.getElementById('climatePos').innerText = data.position;
            document.getElementById('climateAdvice').innerHTML = `<b>💡 大局观风控体系：</b> ${data.advice}`;
        } catch(e) { console.log("大盘加载失败"); }
    };

    async function analyze() {
        const stock = document.getElementById('s_code').value;
        document.getElementById('bt_code').value = stock; // 同步给回测输入框
        if(!stock) return;
        if(kChart) { kChart.destroy(); kChart = null; } 
        
        document.getElementById('loader').style.display = 'block';
        try {
            const res = await fetch('/analyze', {
                method: 'POST', headers: {'Content-Type':'application/json'},
                body: JSON.stringify({stock})
            });
            const data = await res.json();
            document.getElementById('loader').style.display = 'none';
            if(data.error) { document.getElementById('displayArea').innerHTML = `<div class="alert alert-danger mx-3 mt-3">❌ ${data.error}</div>`; return; }
            
            const r = data.report;
            const color = r.change >= 0 ? 'text-up' : 'text-down';
            
            document.getElementById('displayArea').innerHTML = `
            <div class="d-flex justify-content-between align-items-end mb-4 px-2">
                <h2 class="text-white mb-0 fw-bold">${r.name} <span class="fs-5" style="color:#94a3b8;">${r.code}</span></h2>
                <h1 class="${color} mb-0 fw-bold">${r.price.toFixed(2)} <span class="fs-4">${r.change >= 0 ? '+'+r.change : r.change}%</span></h1>
            </div>
            
            <div class="mb-4">
                <pre class="terminal-box" style="font-size: 16px;">${r.text_report}</pre>
            </div>
            
            <div id="chart-container"></div>`;
            setTimeout(() => { renderChart(r.chart_data); }, 100);
        } catch(e) {
            document.getElementById('loader').style.display = 'none';
        }
    }

    function renderChart(data) {
        if(!data || data.length === 0) return;
        const kLineData = data.map(i => ({ x: new Date(i.date).getTime(), y: [i.open, i.high, i.low, i.close] }));
        const ma20Data = data.map(i => ({ x: new Date(i.date).getTime(), y: i.ma20 }));
        const volData = data.map(i => ({ x: new Date(i.date).getTime(), y: i.volume, fillColor: i.close >= i.open ? 'rgba(239,68,68,0.5)' : 'rgba(16,185,129,0.5)' }));

        const options = {
            series: [ { name: 'K线', type: 'candlestick', data: kLineData }, { name: 'MA20', type: 'line', data: ma20Data }, { name: '成交量', type: 'column', data: volData } ],
            chart: { height: 480, type: 'line', background: 'transparent', toolbar: { show: false } },
            theme: { mode: 'dark' }, stroke: { width: [1, 2, 1], curve: 'smooth' }, colors: ['#fff', '#3b82f6', '#fff'],
            plotOptions: { candlestick: { colors: { upward: '#ef4444', downward: '#10b981' }, wick: { useFillColor: true } }, bar: { columnWidth: '80%' } },
            xaxis: { type: 'datetime', labels: { style: { colors: '#94a3b8' } } },
            yaxis: [ { seriesName: 'K线', title: { text: '价格', style: { color: '#64748b' } }, labels: { style: { colors: '#94a3b8' }, formatter: val => val ? val.toFixed(2) : "" } },
                     { seriesName: 'K线', show: false },
                     { seriesName: '成交量', opposite: true, title: { text: '成交量(手)', style: { color: '#64748b' } }, labels: { style: { colors: '#94a3b8' }, formatter: val => val ? (val/10000).toFixed(0) + 'w' : "" } } ],
            grid: { borderColor: '#334155', strokeDashArray: 3 }, legend: { position: 'top', horizontalAlign: 'left', labels: { colors: '#f8fafc' } }
        };
        kChart = new ApexCharts(document.querySelector("#chart-container"), options);
        kChart.render();
    }

    async function runAdvancedBt() {
        const stock = document.getElementById('bt_code').value || document.getElementById('s_code').value;
        const start_date = document.getElementById('bt_start_date').value;
        
        if(!stock) { alert("请先输入股票"); return; }
        
        document.getElementById('loader').style.display = 'block';
        try {
            const res = await fetch('/backtest_advanced', { 
                method: 'POST', headers: {'Content-Type':'application/json'}, 
                body: JSON.stringify({stock, start_date}) 
            });
            const data = await res.json();
            document.getElementById('loader').style.display = 'none';
            if(data.error) { document.getElementById('displayArea').innerHTML = `<div class="alert alert-warning mt-3 mx-3">⚠️ ${data.error}</div>`; return; }
            
            document.getElementById('displayArea').innerHTML = `<div class="px-2"><h4 class="text-white border-bottom border-secondary pb-3 mb-4">🧬 AI 动态平仓策略优化报告</h4><pre class="terminal-box" style="font-size:15px; color:#60a5fa;">${data.report}</pre></div>`;
        } catch(e) { document.getElementById('loader').style.display = 'none'; }
    }
</script>
</body>
</html>'''

if __name__ == '__main__':
    print("🚀 PRO-QUANT V18 终极版引擎已启动！(支持早盘推演与动态回测优化)")
    print("👉 浏览器访问 http://127.0.0.1:10000")
    app.run(host='0.0.0.0', port=10000)
