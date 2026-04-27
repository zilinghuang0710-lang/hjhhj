import pandas as pd
import numpy as np
import re
import urllib3
import requests
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, render_template_string

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================== 常量 ==================
KDJ_WINDOW = 9
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
RSI_PERIOD = 14
BOLL_PERIOD = 20
BOLL_WIDTH = 2
SUPPORT_RESIST_WINDOW = 20
CHIP_DAYS = 60
KLINE_LIMIT = 120

active_cache = {"data": None, "time": 0}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
]

def create_session():
    session = requests.Session()
    retry = urllib3.util.retry.Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
    adapter = requests.adapters.HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.headers.update({"User-Agent": random.choice(USER_AGENTS)})
    session.verify = False
    return session

http_session = create_session()

# ================== 活跃股票池（双源） ==================
def get_active_stocks(limit=400):
    global active_cache
    now = time.time()
    if active_cache["data"] is not None and (now - active_cache["time"]) < 1800:
        return active_cache["data"][:limit]

    stocks = []
    # 东方财富
    try:
        url = "http://80.push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": str(limit), "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2",
            "fid": "f20",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f14,f10,f20,f100",
            "_": str(int(now*1000))
        }
        headers = {"Referer": "http://quote.eastmoney.com/"}
        resp = http_session.get(url, params=params, headers=headers, timeout=5).json()
        if resp and resp.get("data") and resp["data"].get("diff"):
            for item in resp["data"]["diff"]:
                code, name = item.get("f12"), item.get("f14")
                if not code or "ST" in name: continue
                stocks.append({"code": code, "name": name, "volume_ratio": item.get("f20", 1), "sector": item.get("f100", "")})
    except: pass

    # 腾讯热点备用
    if len(stocks) < 50:
        try:
            tx_url = "http://web.ifzq.gtimg.cn/appstock/app/rank/total?type=1&num=200"
            tx_data = http_session.get(tx_url, timeout=5).json()
            if tx_data and tx_data.get("data"):
                for stock in tx_data["data"].get("stocks", []):
                    code, name = stock.get("code"), stock.get("name")
                    if code and "ST" not in name:
                        stocks.append({"code": code, "name": name, "volume_ratio": 1.0, "sector": ""})
        except: pass

    if not stocks:
        stocks = [
            {"code":"002400","name":"省广集团","sector":"传媒"}, {"code":"600519","name":"贵州茅台","sector":"酿酒"},
            {"code":"300750","name":"宁德时代","sector":"电池"}, {"code":"601318","name":"中国平安","sector":"保险"},
            {"code":"688981","name":"中芯国际","sector":"半导体"},
        ]
    stocks = list({s['code']: s for s in stocks}.values())
    active_cache["data"] = stocks
    active_cache["time"] = now
    return stocks[:limit]

# ================== 股票搜索（三源） ==================
def resolve_stock_input(keyword):
    keyword = str(keyword).strip()
    if re.match(r'^\d{6}$', keyword): return keyword, f"A股_{keyword}"
    local = {
        "省广集团":"002400", "sgjt":"002400", "贵州茅台":"600519", "gzmt":"600519",
        "宁德时代":"300750", "ndsd":"300750", "中信证券":"600030", "zxzq":"600030",
        "东方财富":"300059", "dfcf":"300059",
    }
    if keyword in local: return local[keyword], keyword
    try:
        resp = http_session.get("https://searchapi.eastmoney.com/api/suggest/get", params={
            "input":keyword,"type":"14","token":"D43BF722C8E33BDC906FB84D85E326E8","count":"1"}, timeout=3)
        if resp.status_code==200:
            data = resp.json()
            if data and data.get("QuotationCodeTable") and data["QuotationCodeTable"]["Data"]:
                item = data["QuotationCodeTable"]["Data"][0]
                return item["Code"], item["Name"]
    except: pass
    try:
        resp = http_session.get("http://smartbox.gtimg.cn/s3/", params={"v":2,"q":keyword,"t":"all"}, timeout=3)
        resp.encoding = 'GBK'
        match = re.search(r'v_hint="(.*?)"', resp.text)
        if match and match.group(1):
            parts = match.group(1).split('^')[0].split(',')
            if len(parts)>=2:
                code_match = re.search(r'\d{6}', parts[0])
                if code_match: return code_match.group(0), parts[1]
    except: pass
    return None, None

# ================== 极速三源K线引擎（腾讯优先） ==================
class StockAnalyzer:
    def __init__(self, symbol, name, cost_price=None):
        self.symbol, self.name = symbol, name
        self.cost_price = float(cost_price) if cost_price else None
        self.df = pd.DataFrame()
        self.chip_peak = 0.0

    def fetch_data(self, end_date=None):
        # 腾讯（最快）
        prefix = "sz" if self.symbol.startswith(("0","3")) else "sh"
        tx_url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{self.symbol},day,,,{KLINE_LIMIT},qfq"
        try:
            resp = http_session.get(tx_url, timeout=2)
            if resp.status_code == 200:
                data = resp.json()
                if data and data.get("code") == 0:
                    stock_data = data["data"].get(f"{prefix}{self.symbol}")
                    if stock_data:
                        kline = stock_data.get("qfqday") or stock_data.get("day")
                        if kline and len(kline) >= 10:
                            self.df = pd.DataFrame([{
                                "date": i[0], "open": float(i[1]), "close": float(i[2]),
                                "high": float(i[3]), "low": float(i[4]), "volume": float(i[5]),
                                "turnover": 0.0
                            } for i in kline])
                            return True
        except: pass

        # 东财
        mkt = "0" if self.symbol.startswith(("0","3")) else "1"
        url_em = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
        params_em = {
            "secid": f"{mkt}.{self.symbol}",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101", "fqt": "1",
            "end": end_date if end_date else "20500101",
            "lmt": str(KLINE_LIMIT)
        }
        headers = {"Referer": "http://quote.eastmoney.com/"}
        try:
            resp = http_session.get(url_em, params=params_em, headers=headers, timeout=2)
            if resp.status_code == 200:
                data = resp.json()
                if data and data.get("data") and data["data"].get("klines"):
                    klines = data["data"]["klines"]
                    if len(klines) >= 10:
                        self.df = pd.DataFrame([{
                            "date": p[0], "open": float(p[1]), "close": float(p[2]),
                            "high": float(p[3]), "low": float(p[4]), "volume": float(p[5]),
                            "turnover": float(p[10])
                        } for p in (item.split(",") for item in klines)])
                        return True
        except: pass

        # 新浪（超快失败）
        try:
            sina_prefix = "sz" if self.symbol.startswith(("0","3")) else "sh"
            sina_url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={sina_prefix}{self.symbol}&scale=240&ma=no&datalen={KLINE_LIMIT}"
            resp = http_session.get(sina_url, timeout=1)
            if resp.status_code == 200 and resp.text:
                raw = resp.json()
                if isinstance(raw, list) and len(raw) >= 10:
                    self.df = pd.DataFrame([{
                        "date": item["day"], "open": float(item["open"]), "close": float(item["close"]),
                        "high": float(item["high"]), "low": float(item["low"]),
                        "volume": float(item["volume"]), "turnover": 0.0
                    } for item in raw])
                    return True
        except: pass

        return False

    def calculate_indicators(self):
        if self.df.empty or len(self.df) < 10: return False
        df = self.df
        df['MA5'] = df['close'].rolling(5).mean()
        df['MA10'] = df['close'].rolling(10).mean()
        df['MA20'] = df['close'].rolling(20).mean()
        df['VMA5'] = df['volume'].rolling(5).mean()
        l9, h9 = df['low'].rolling(9, min_periods=1).min(), df['high'].rolling(9, min_periods=1).max()
        denom = h9 - l9
        rsv = np.where(denom==0, 50, (df['close']-l9)/denom*100)
        df['K'] = pd.Series(rsv, index=df.index).ewm(com=2, adjust=False).mean()
        df['D'] = df['K'].ewm(com=2, adjust=False).mean()
        df['J'] = 3*df['K'] - 2*df['D']
        ema12 = df['close'].ewm(span=12, adjust=False).mean()
        ema26 = df['close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = ema12 - ema26
        df['DEA'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['MACD'] = 2*(df['DIF'] - df['DEA'])
        delta = df['close'].diff()
        gain = delta.where(delta>0, 0).ewm(alpha=1/RSI_PERIOD, adjust=False).mean()
        loss = (-delta.where(delta<0, 0)).ewm(alpha=1/RSI_PERIOD, adjust=False).mean()
        df['RSI'] = 100 - (100/(1+gain/loss))
        df['BOLL_MID'] = df['close'].rolling(BOLL_PERIOD).mean()
        std = df['close'].rolling(BOLL_PERIOD).std()
        df['BOLL_UP'] = df['BOLL_MID'] + BOLL_WIDTH*std
        df['BOLL_DN'] = df['BOLL_MID'] - BOLL_WIDTH*std
        df['Support'] = df['low'].rolling(SUPPORT_RESIST_WINDOW).min()
        df['Resistance'] = df['high'].rolling(SUPPORT_RESIST_WINDOW).max()
        recent = df.tail(CHIP_DAYS)
        if not recent.empty and recent['close'].max() > recent['close'].min():
            bins = np.linspace(recent['close'].min(), recent['close'].max(), 11)
            dist = recent.groupby(pd.cut(recent['close'], bins=bins, observed=False))['volume'].sum()
            self.chip_peak = dist.idxmax().mid if not dist.empty else recent['close'].iloc[-1]
        self.df = df.fillna(0)
        return True

    def get_full_report(self):
        if not self.calculate_indicators(): return {"error": "数据不足"}
        latest, prev = self.df.iloc[-1], self.df.iloc[-2]
        vol_ratio = latest['volume'] / (latest['VMA5'] if latest['VMA5'] > 0 else 1)
        is_wash, _, _ = self.is_washout_pattern()
        intent = "区间震荡"
        if latest['close'] > prev['close'] and vol_ratio > 1.3: intent = "放量启动"
        elif is_wash: intent = "缩量洗盘"
        elif latest['close'] < prev['close'] and vol_ratio > 1.5: intent = "恐慌砸盘"

        txt_report = f"""=======================================================
 🤖 【{self.name} ({self.symbol})】 全维度深度量价诊断
=======================================================
💰 收盘: {latest['close']:.2f}  |  换手: {latest['turnover']:.2f}%

📊 均线: MA5={latest['MA5']:.2f} MA10={latest['MA10']:.2f} MA20={latest['MA20']:.2f}
   {'✅ 多头排列' if latest['MA5']>latest['MA10']>latest['MA20'] else '⚠️ 均线缠绕/空头'}

📈 KDJ: K={latest['K']:.1f} D={latest['D']:.1f} J={latest['J']:.1f}
   {'✅ J值超卖，反弹需求' if latest['J']<20 else '⚠️ 高位钝化' if latest['J']>80 else '↔️ 中轴震荡'}

🔮 MACD: DIF={latest['DIF']:.3f} DEA={latest['DEA']:.3f} MACD柱={latest['MACD']:.3f}
   {'✅ 零轴上方多头' if latest['DIF']>0 else '↔️ 弱势区间'}

📉 RSI(14): {latest['RSI']:.1f}
   {'✅ 超卖区' if latest['RSI']<30 else '⚠️ 超买区' if latest['RSI']>70 else '↔️ 中性'}

📐 BOLL: 上轨={latest['BOLL_UP']:.2f} 中轨={latest['BOLL_MID']:.2f} 下轨={latest['BOLL_DN']:.2f}
   {'🔹 触及下轨支撑' if latest['close']<=latest['BOLL_DN']*1.02 else '🔸 逼近上轨' if latest['close']>=latest['BOLL_UP']*0.98 else '↔️ 通道内运行'}

🌊 量能: 今日{latest['volume']/10000:.1f}万手  5日均{latest['VMA5']/10000:.1f}万手  量比{vol_ratio:.2f}
   {'✅ 极致缩量，抛压枯竭' if vol_ratio<0.8 else '🚀 明显放量' if vol_ratio>1.5 else '↔️ 温和换手'}

🏔️ 支撑: {latest['Support']:.2f}   压力: {latest['Resistance']:.2f}   筹码峰: {self.chip_peak:.2f}
======================================================="""

        chart_data = []
        for _, row in self.df.tail(80).iterrows():
            chart_data.append({
                "date": row['date'],
                "open": round(row['open'], 2), "high": round(row['high'], 2),
                "low": round(row['low'], 2), "close": round(row['close'], 2),
                "ma5": round(row['MA5'], 2), "ma20": round(row['MA20'], 2),
                "volume": round(row['volume'], 0)
            })
        return {
            "name": self.name, "code": self.symbol, "price": round(latest['close'],2),
            "change": round((latest['close']-prev['close'])/prev['close']*100,2),
            "kdj": {"K":round(latest['K'],1),"D":round(latest['D'],1),"J":round(latest['J'],1)},
            "vol_ratio": round(vol_ratio,2), "intent": intent,
            "score": 85 if intent in ["放量启动","缩量洗盘"] else 50,
            "text_report": txt_report, "chart_data": chart_data
        }

    def is_washout_pattern(self):
        if len(self.df) < 40: return False, 0, ""
        df40 = self.df.tail(40)
        if df40['high'].max() / df40['close'].iloc[0] < 1.06: return False, 0, ""
        recent10 = self.df.tail(10)
        if (recent10['high'].max()-recent10['low'].min())/recent10['low'].min() > 0.18: return False, 0, ""
        if self.df['volume'].tail(5).mean() / df40['volume'].iloc[0:20].mean() > 0.85: return False, 0, ""
        return True, 80, "缩量洗盘"

    def score_band(self):
        latest = self.df.iloc[-1]
        score = 0
        if latest['MA5'] > latest['MA20']: score += 2
        if latest['close'] > latest['MA20']: score += 1
        if 20 < latest['J'] < 80: score += 1
        return score

# ================== 策略扫描 ==================
def analyze_single_scan(code, strategy, name, sector):
    az = StockAnalyzer(code, name)
    if not az.fetch_data() or not az.calculate_indicators(): return None
    latest = az.df.iloc[-1]
    if strategy == "short":
        score = 0
        if latest['J'] < 35: score += 2
        if latest['close'] > az.df.iloc[-2]['close'] and latest['volume']/latest['VMA5'] > 1.1: score += 2
        if score < 2: return None
        advice = {"介入价": f"{latest['close']*0.99:.2f}", "风格": "一日游"}
    elif strategy == "band":
        score = az.score_band()
        if score < 2: return None
        advice = {"介入价": f"{latest['MA20']*1.01:.2f}", "风格": "中线波段"}
    else:
        is_wash, score, _ = az.is_washout_pattern()
        if not is_wash: return None
        advice = {"介入价": f"{latest['Support']:.2f}", "风格": "洗盘低吸"}
    return {"code":code, "name":name, "sector":sector, "score":score, "close":f"{latest['close']:.2f}", "advice":advice}

def scan_stocks(strategy, top_n=10):
    stocks = get_active_stocks(400)
    results = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(analyze_single_scan, s["code"], strategy, s["name"], s["sector"]): s for s in stocks}
        for f in as_completed(futures):
            time.sleep(0.08)
            res = f.result()
            if res: results.append(res)
    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:top_n]

# ================== 回测 ==================
BACKTEST_POOL = ["600519","000858","601318","000002","600036","601166","002400","300750","688981","600030","300059"]
def run_backtest(strategy, test_date_str, hold_days=1):
    end_date = test_date_str.replace("-","")
    test_date = pd.to_datetime(test_date_str).date()
    results = []
    for code in BACKTEST_POOL:
        az = StockAnalyzer(code, f"BT_{code}")
        if not az.fetch_data(end_date=end_date) or not az.calculate_indicators(): continue
        valid = False
        if strategy == "short" and az.df.iloc[-1]['J'] < 35: valid = True
        elif strategy == "band" and az.score_band() >= 2: valid = True
        elif strategy == "washout" and az.is_washout_pattern()[0]: valid = True
        if not valid: continue
        full = StockAnalyzer(code, f"FULL_{code}")
        if not full.fetch_data(): continue
        future = full.df[full.df['date'] > pd.Timestamp(test_date)].head(hold_days)
        if len(future) < hold_days: continue
        buy_price = full.df[full.df['date'] <= pd.Timestamp(test_date)].iloc[-1]['close']
        sell_price = future.iloc[-1]['close']
        profit = (sell_price - buy_price) / buy_price * 100
        results.append({"code":code, "name":full.name, "profit": profit, "success": profit > 0})
    if not results: return None
    acc = sum(1 for r in results if r['success'])/len(results)*100
    return {"date":test_date_str, "strategy":strategy, "hold":hold_days, "total":len(results), "accuracy":acc, "picks":results}

# ================== Flask 应用 ==================
app = Flask(__name__)

@app.route('/')
def home(): return render_template_string(HTML)

@app.route('/analyze', methods=['POST'])
def analyze():
    d = request.get_json()
    code, name = resolve_stock_input(d.get('stock'))
    if not code: return jsonify({"error":"无法识别股票"})
    az = StockAnalyzer(code, name, d.get('cost'))
    if not az.fetch_data(): return jsonify({"error":"行情获取失败（已尝试腾讯/东财/新浪）"})
    rep = az.get_full_report()
    if "error" in rep: return jsonify(rep)
    return jsonify({"report":rep})

@app.route('/scan', methods=['POST'])
def scan():
    strategy = request.get_json().get('strategy','washout')
    return jsonify({"results": scan_stocks(strategy, 10)})

@app.route('/portfolio', methods=['POST'])
def portfolio():
    holdings = request.get_json().get('holdings',[])
    if not holdings: return jsonify({"error":"请输入有效仓位"})
    res_text = "📊 【持仓组合健康度分析】\n" + "="*40 + "\n"
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {}
        for h in holdings:
            code, _ = resolve_stock_input(h['code'])
            if code: futures[ex.submit(StockAnalyzer(code, code).fetch_data)] = (code, float(h['cost']), float(h['weight']))
        for f in as_completed(futures):
            code, cost, weight = futures[f]
            az = StockAnalyzer(code, code)
            if f.result() and az.calculate_indicators():
                curr_p = az.df.iloc[-1]['close']
                profit_pct = (curr_p - cost)/cost*100
                res_text += f"🔹 {code} 现价:{curr_p:.2f} 盈亏:{profit_pct:+.2f}% 状态:{'✅ 健康' if az.df.iloc[-1]['J']<80 else '⚠️ 高位'}\n"
    res_text += "="*40
    return jsonify({"report": res_text})

@app.route('/backtest', methods=['POST'])
def run_bt():
    d = request.get_json()
    res = run_backtest(d.get('strategy'), d.get('date'), int(d.get('hold_days')))
    if not res: return jsonify({"error":"无信号"})
    txt = f"📅 回测: {res['date']} | 策略: {res['strategy']} | 持有{res['hold']}天\n"
    txt += f"🎯 信号数:{res['total']}  | 胜率:{res['accuracy']:.1f}%\n"+"-"*30+"\n"
    for p in res['picks']: txt += f" {p['name']}({p['code']}) 收益:{p['profit']:+.2f}% {'✅' if p['success'] else '❌'}\n"
    return jsonify({"report": txt})

# ================== 前端 HTML ==================
HTML = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PRO-QUANT V8.5 极速全能版</title>
    <link href="https://cdn.bootcdn.net/ajax/libs/twitter-bootstrap/5.3.0/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.bootcdn.net/ajax/libs/bootstrap/5.3.0/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
    <style>
        :root { --bg:#0d1117; --panel:#161b22; --border:#30363d; --text:#c9d1d9; --acc:#2ea043; }
        body { background: var(--bg); color: var(--text); font-family: -apple-system, sans-serif; }
        .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 15px; }
        .card-header-custom { background: rgba(255,255,255,0.02); border-bottom: 1px solid var(--border); padding: 12px 15px; font-weight: bold; border-radius: 10px 10px 0 0; }
        .form-control, .form-select { background: #010409; border: 1px solid var(--border); color: #fff; }
        .btn-primary { background: var(--acc); border: none; } .btn-primary:hover { background: #3fb950; }
        .terminal-box { background: #010409; color: #39ff14; font-family: 'Consolas', monospace; padding: 15px; border-radius: 8px; border: 1px solid var(--border); font-size: 13px; }
        .metric-box { background: #010409; border: 1px solid var(--border); border-radius: 8px; padding: 10px; text-align: center; }
        .stock-card { cursor: pointer; transition: 0.2s; } .stock-card:hover { border-color: var(--acc); background: #1c2128; }
        .text-up { color: #f85149 !important; } .text-down { color: #3fb950 !important; }
        #chart-container { height: 450px; width: 100%; margin-top: 15px; }
    </style>
</head>
<body>
<nav class="navbar navbar-dark bg-dark mb-3 border-bottom border-secondary">
    <div class="container-fluid"><span class="navbar-brand fw-bold text-success">🛰️ PRO-QUANT V8.5 三源极速引擎</span></div>
</nav>

<div class="container-fluid px-4">
    <div class="row">
        <div class="col-lg-3">
            <div class="card">
                <div class="card-header-custom">🎯 个股深度侦测</div>
                <div class="card-body">
                    <input type="text" id="s_code" class="form-control mb-2" placeholder="代码/名称/拼音" value="省广集团">
                    <input type="number" id="s_cost" class="form-control mb-3" placeholder="持仓成本(可选)">
                    <button class="btn btn-primary w-100" onclick="analyze()">🚀 执行侦测</button>
                </div>
            </div>
            <div class="card">
                <div class="card-header-custom">📡 策略扫描 (各5~10只)</div>
                <div class="card-body">
                    <button class="btn btn-outline-info w-100 mb-2" onclick="scan('short')">⚡ 短线突击</button>
                    <button class="btn btn-outline-success w-100 mb-2" onclick="scan('band')">🌊 中线波段</button>
                    <button class="btn btn-outline-warning w-100" onclick="scan('washout')">🛁 缩量洗盘坑</button>
                </div>
            </div>
            <div class="card">
                <div class="card-header-custom">🛠️ 资产与回测</div>
                <div class="card-body">
                    <div id="ports" class="mb-2">
                        <div class="d-flex gap-1 mb-1"><input class="form-control form-control-sm p_code" placeholder="代码"><input class="form-control form-control-sm p_cost" placeholder="成本"><input class="form-control form-control-sm p_weight" placeholder="仓位%"></div>
                    </div>
                    <div class="d-flex justify-content-between mb-3">
                        <button class="btn btn-sm btn-outline-secondary" onclick="addPort()">+加一行</button>
                        <button class="btn btn-sm btn-success" onclick="calcPort()">运算组合</button>
                    </div>
                    <hr class="border-secondary">
                    <select id="bt_strat" class="form-select form-select-sm mb-1"><option value="washout">洗盘策略</option><option value="short">短线策略</option><option value="band">波段策略</option></select>
                    <input type="date" id="bt_date" class="form-control form-control-sm mb-1">
                    <select id="bt_hold" class="form-select form-select-sm mb-2"><option value="1">持有1天</option><option value="3">持有3天</option><option value="5">持有5天</option></select>
                    <button class="btn btn-sm btn-outline-danger w-100" onclick="runBt()">⏳ 历史回测</button>
                </div>
            </div>
        </div>

        <div class="col-lg-9">
            <div class="card" style="min-height: 800px;">
                <div class="card-header-custom d-flex justify-content-between">
                    <span>📺 核心情报大屏</span><span id="loader" class="text-success" style="display:none;">处理中...</span>
                </div>
                <div class="card-body" id="displayArea">
                    <div class="text-center text-secondary mt-5"><h3>等待指令...</h3></div>
                </div>
            </div>
        </div>
    </div>
</div>

<script>
    document.getElementById('bt_date').value = new Date().toISOString().slice(0,10);
    let kChart = null;

    async function analyze() {
        const stock = document.getElementById('s_code').value, cost = document.getElementById('s_cost').value;
        if(!stock) return;
        document.getElementById('loader').style.display = 'block';
        const res = await fetch('/analyze', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({stock, cost}) });
        const data = await res.json();
        document.getElementById('loader').style.display = 'none';
        if(data.error) { document.getElementById('displayArea').innerHTML = `<div class="alert alert-danger">❌ ${data.error}</div>`; return; }
        const r = data.report;
        const color = r.change >= 0 ? 'text-up' : 'text-down';
        let html = `<div class="d-flex justify-content-between align-items-end mb-3"><h3 class="text-white mb-0">${r.name} <span class="fs-6 text-secondary">${r.code}</span></h3><h2 class="${color} mb-0">${r.price} <span class="fs-6">${r.change >= 0 ? '+'+r.change : r.change}%</span></h2></div>
        <div class="row g-2 mb-3">
            <div class="col-3"><div class="metric-box"><div class="metric-title">主力意图</div><div class="fw-bold text-info">${r.intent}</div></div></div>
            <div class="col-3"><div class="metric-box"><div class="metric-title">KDJ (J值)</div><div class="fw-bold">${r.kdj.J}</div></div></div>
            <div class="col-3"><div class="metric-box"><div class="metric-title">动能量比</div><div class="fw-bold">${r.vol_ratio}</div></div></div>
            <div class="col-3"><div class="metric-box"><div class="metric-title">系统评分</div><div class="fw-bold text-success">${r.score}</div></div></div>
        </div>
        <div id="chart-container"></div>
        <button class="btn btn-sm btn-outline-secondary mt-3 w-100" type="button" data-bs-toggle="collapse" data-bs-target="#textReport">📄 展开/折叠 全维度文本诊断</button>
        <div class="collapse mt-2" id="textReport"><pre class="terminal-box">${r.text_report}</pre></div>`;
        document.getElementById('displayArea').innerHTML = html;
        renderChart(r.chart_data);
    }

    function renderChart(data) {
        const kLineData = data.map(i => ({ x: new Date(i.date).getTime(), y: [i.open, i.high, i.low, i.close] }));
        const ma20Data = data.map(i => ({ x: new Date(i.date).getTime(), y: i.ma20 }));
        const volData = data.map(i => ({ x: new Date(i.date).getTime(), y: i.volume, fillColor: i.close >= i.open ? 'rgba(248,81,73,0.5)' : 'rgba(63,185,80,0.5)' }));

        const options = {
            series: [
                { name: 'K线', type: 'candlestick', data: kLineData },
                { name: 'MA20', type: 'line', data: ma20Data },
                { name: '成交量', type: 'column', data: volData }
            ],
            chart: { height: 450, type: 'line', background: 'transparent', toolbar: { show: false }, stacked: false },
            theme: { mode: 'dark' },
            stroke: { width: [1, 2, 1], curve: 'smooth' },
            colors: ['#fff', '#00e676', '#00e676'],
            plotOptions: {
                candlestick: { colors: { upward: '#f85149', downward: '#3fb950' }, wick: { useFillColor: true } },
                bar: { columnWidth: '80%', borderRadius: 0 }
            },
            xaxis: { type: 'datetime', labels: { style: { colors: '#8b949e' } } },
            yaxis: [
                { seriesName: 'K线', title: { text: '价格', style: { color: '#8b949e' } }, labels: { style: { colors: '#8b949e' } }, tooltip: { enabled: true } },
                { seriesName: '成交量', opposite: true, title: { text: '成交量(手)', style: { color: '#8b949e' } }, labels: { style: { colors: '#8b949e' } }, min: 0 }
            ],
            grid: { borderColor: '#30363d', strokeDashArray: 3 },
            legend: { position: 'top', horizontalAlign: 'left' }
        };
        if(kChart) kChart.destroy();
        kChart = new ApexCharts(document.querySelector("#chart-container"), options);
        kChart.render();
    }

    async function scan(strategy) {
        document.getElementById('loader').style.display = 'block';
        const res = await fetch('/scan', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({strategy}) });
        const data = await res.json();
        document.getElementById('loader').style.display = 'none';
        let html = `<h5 class="mb-3 text-white border-bottom border-secondary pb-2">🎯 扫描结果 (点击卡片查看K线图)</h5><div class="row g-2">`;
        if(!data.results || data.results.length === 0) html += "<div class='text-secondary'>未扫描到标的，请稍后再试。</div>";
        data.results.forEach(r => { html += `<div class="col-md-6"><div class="card p-2 stock-card h-100" onclick="quickAnalyze('${r.code}')"><div class="d-flex justify-content-between mb-1"><strong class="text-white">${r.name}</strong><span class="text-success small">${r.score}分</span></div><div class="text-secondary" style="font-size:12px;">现价: ${r.close} | 介入位: ${r.advice.介入价}</div></div></div>`; });
        html += '</div>';
        document.getElementById('displayArea').innerHTML = html;
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
            const code = r.querySelector('.p_code').value.trim(), cost = r.querySelector('.p_cost').value, weight = r.querySelector('.p_weight').value;
            if(code) holdings.push({code, cost: cost||0, weight: weight||0});
        });
        document.getElementById('loader').style.display = 'block';
        const res = await fetch('/portfolio', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({holdings}) });
        const data = await res.json();
        document.getElementById('loader').style.display = 'none';
        document.getElementById('displayArea').innerHTML = `<h5 class="text-white">🛠️ 持仓组合结算</h5><pre class="terminal-box mt-3">${data.report || data.error}</pre>`;
    }

    async function runBt() {
        const strategy = document.getElementById('bt_strat').value, date = document.getElementById('bt_date').value, hold_days = document.getElementById('bt_hold').value;
        document.getElementById('loader').style.display = 'block';
        const res = await fetch('/backtest', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({strategy, date, hold_days}) });
        const data = await res.json();
        document.getElementById('loader').style.display = 'none';
        document.getElementById('displayArea').innerHTML = `<h5 class="text-white">⏳ 历史回测报告</h5><pre class="terminal-box mt-3">${data.report || data.error}</pre>`;
    }
</script>
</body>
</html>'''

if __name__ == '__main__':
    print("⚡ PRO-QUANT V8.5 极速全能版已启动")
    print("👉 浏览器访问: http://127.0.0.1:10000")
    app.run(host='0.0.0.0', port=10000)
