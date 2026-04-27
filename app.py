import pandas as pd
import numpy as np
import re
import urllib3
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, render_template_string

# 屏蔽 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================== 常量配置 ==================
KDJ_WINDOW = 9
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
SUPPORT_RESIST_WINDOW = 20
CHIP_DAYS = 60
KLINE_LIMIT = 120

active_cache = {"data": None, "time": 0}

# ================== 强力网络会话 ==================
def create_session():
    session = requests.Session()
    retry = urllib3.util.retry.Retry(total=3, backoff_factor=0.5)
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    session.verify = False
    return session

http_session = create_session()

# ================== 活跃股票池 ==================
def get_active_stocks(limit=200):
    global active_cache
    now = time.time()
    if active_cache["data"] is not None and (now - active_cache["time"]) < 1800:
        return active_cache["data"][:limit]
    try:
        url = "http://80.push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": str(min(limit+50, 500)), "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2", "fid": "f20",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f14,f2,f3,f10,f20,f21,f15,f100",
            "_": str(int(time.time()*1000))
        }
        resp = http_session.get(url, params=params, timeout=5)
        data = resp.json()
        if data and data.get("data") and data["data"].get("diff"):
            stocks = []
            for item in data["data"]["diff"]:
                code = item.get("f12")
                name = item.get("f14")
                if not code: continue
                f20 = item.get("f20", 0)
                f10 = item.get("f10", 0)
                if "ST" in name or f20 is None or f20 <= 0 or f10 is None:
                    continue
                stocks.append({"code":code, "name":name, "volume_ratio":f20, "turnover":f10, "sector":item.get("f100", "")})
            stocks.sort(key=lambda x: x["volume_ratio"], reverse=True)
            active_cache["data"] = stocks
            active_cache["time"] = now
            return stocks[:limit]
    except: pass
    return [{"code":"002400","name":"省广集团","sector":"文化传媒"}, {"code":"600519","name":"贵州茅台","sector":"酿酒行业"}]

# ================== 股票智能搜索 ==================
def resolve_stock_input(keyword):
    keyword = str(keyword).strip()
    if re.match(r'^\d{6}$', keyword):
        return keyword, f"A股_{keyword}"
    offline = {"省广集团":"002400","sgjt":"002400","贵州茅台":"600519","gzmt":"600519","宁德时代":"300750","中信证券":"600030","东方财富":"300059"}
    if keyword in offline:
        return offline[keyword], keyword
    try:
        resp = http_session.get("https://searchapi.eastmoney.com/api/suggest/get", params={
            "input":keyword,"type":"14","token":"D43BF722C8E33BDC906FB84D85E326E8","count":"1"}, timeout=3)
        if resp.status_code==200:
            data = resp.json()
            if data and data.get("QuotationCodeTable") and data["QuotationCodeTable"]["Data"]:
                item = data["QuotationCodeTable"]["Data"][0]
                return item["Code"], item["Name"]
    except: pass
    return None, None

# ================== 双核分析引擎 ==================
class StockAnalyzer:
    def __init__(self, symbol, name, cost_price=None):
        self.symbol = symbol
        self.name = name
        self.cost_price = float(cost_price) if cost_price else None
        self.df = pd.DataFrame()
        self.chip_peak = 0.0

    def fetch_data(self):
        """双核引擎：东财失败立刻无缝切换腾讯"""
        # 1. 尝试东财主引擎
        mkt = "0" if self.symbol.startswith(("0","3")) else "1"
        url_em = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
        params_em = {"secid":f"{mkt}.{self.symbol}","fields1":"f1,f2,f3,f4,f5,f6",
                     "fields2":"f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                     "klt":"101","fqt":"1","end":"20500101","lmt":str(KLINE_LIMIT)}
        try:
            resp = http_session.get(url_em, params=params_em, timeout=5)
            if resp.status_code==200:
                res_json = resp.json()
                if res_json and res_json.get("data") and res_json["data"].get("klines"):
                    klines = res_json["data"]["klines"]
                    parsed = [{"date":p[0],"open":float(p[1]),"close":float(p[2]),"high":float(p[3]),"low":float(p[4]),
                               "volume":float(p[5]),"turnover":float(p[10])} for p in (item.split(",") for item in klines)]
                    self.df = pd.DataFrame(parsed)
                    return True
        except: pass

        # 2. 尝试腾讯备用引擎 (东财失败时的救命稻草)
        prefix = "sz" if self.symbol.startswith(("0", "3")) else "sh"
        url_tx = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{self.symbol},day,,,{KLINE_LIMIT},qfq"
        try:
            resp = http_session.get(url_tx, timeout=5)
            data = resp.json()
            if data and data.get("code") == 0:
                stock_data = data["data"].get(f"{prefix}{self.symbol}", {})
                kline = stock_data.get("qfqday") or stock_data.get("day")
                if kline:
                    parsed = [{"date": i[0], "open": float(i[1]), "close": float(i[2]), "high": float(i[3]), 
                               "low": float(i[4]), "volume": float(i[5]), "turnover": 0.0} for i in kline]
                    self.df = pd.DataFrame(parsed)
                    return True
        except: pass
        return False

    def calculate_indicators(self):
        if self.df.empty or len(self.df) < 20: return False
        df = self.df
        df['MA5'] = df['close'].rolling(5).mean()
        df['MA20'] = df['close'].rolling(20).mean()
        df['VMA5'] = df['volume'].rolling(5).mean()
        
        # KDJ
        l9 = df['low'].rolling(9, min_periods=1).min()
        h9 = df['high'].rolling(9, min_periods=1).max()
        denom = h9 - l9
        rsv = np.where(denom==0, 50, (df['close']-l9)/denom*100)
        df['K'] = pd.Series(rsv, index=df.index).ewm(com=2, adjust=False).mean()
        df['D'] = df['K'].ewm(com=2, adjust=False).mean()
        df['J'] = 3*df['K'] - 2*df['D']
        
        # 支撑压力与筹码
        df['Support'] = df['low'].rolling(20).min()
        df['Resistance'] = df['high'].rolling(20).max()
        recent = df.tail(60)
        if not recent.empty:
            mn, mx = recent['close'].min(), recent['close'].max()
            if mx > mn:
                bins = np.linspace(mn, mx, 11)
                dist = recent.groupby(pd.cut(recent['close'], bins=bins))['volume'].sum()
                self.chip_peak = dist.idxmax().mid if not dist.empty else recent['close'].iloc[-1]
        self.df = df.fillna(0)
        return True

    def get_full_report(self):
        if not self.calculate_indicators(): return {"error": "有效数据不足，无法计算指标"}
        latest = self.df.iloc[-1]; prev = self.df.iloc[-2]
        vol_ratio = latest['volume'] / (latest['VMA5'] if latest['VMA5'] > 0 else 1)
        
        # 洗盘推演
        is_wash = False
        if len(self.df) >= 40:
            df40 = self.df.tail(40)
            if df40['high'].max() / df40['close'].iloc[0] >= 1.10:
                if (self.df.tail(10)['high'].max() - self.df.tail(10)['low'].min())/self.df.tail(10)['low'].min() <= 0.12:
                    if self.df['volume'].tail(5).mean() / df40['volume'].iloc[0:20].mean() <= 0.8:
                        is_wash = True

        intent = "区间震荡"
        if latest['close'] > prev['close'] and vol_ratio > 1.5: intent = "放量启动"
        elif is_wash: intent = "缩量洗盘"
        elif latest['close'] < prev['close'] and vol_ratio > 1.8: intent = "恐慌砸盘"

        # K线图数据准备
        chart_data = []
        for _, row in self.df.tail(80).iterrows():
            chart_data.append({
                "date": row['date'], "open": round(row['open'], 2), "high": round(row['high'], 2),
                "low": round(row['low'], 2), "close": round(row['close'], 2),
                "ma5": round(row['MA5'], 2), "ma20": round(row['MA20'], 2)
            })

        return {
            "name": self.name, "code": self.symbol, "price": round(latest['close'], 2),
            "change": round((latest['close']-prev['close'])/prev['close']*100, 2),
            "kdj": {"K":round(latest['K'],1),"D":round(latest['D'],1),"J":round(latest['J'],1)},
            "vol_ratio": round(vol_ratio, 2), "intent": intent,
            "score": 85 if intent in ["放量启动","缩量洗盘"] else 50,
            "support": round(latest['Support'],2), "resistance": round(latest['Resistance'],2),
            "chip_peak": round(self.chip_peak, 2), "chart_data": chart_data
        }

# ================== 策略扫描模块 ==================
def analyze_single(code, strategy, name, sector):
    analyzer = StockAnalyzer(code, name)
    if not analyzer.fetch_data() or not analyzer.calculate_indicators(): return None
    latest = analyzer.df.iloc[-1]

    if strategy == "short":
        score = 0
        if latest['J'] < 20: score += 3
        if latest['close'] > analyzer.df.iloc[-2]['close'] and latest['volume']/latest['VMA5'] > 1.5: score += 3
        advice = {"介入价":f"{latest['close']*0.98:.2f}", "风格":"短线一日游"}
    elif strategy == "band":
        score = 0
        if latest['MA5'] > latest['MA20']: score += 2
        if latest['close'] > latest['MA20']: score += 2
        if 30 < latest['J'] < 70: score += 1
        advice = {"介入价":f"{latest['MA20']*1.01:.2f}", "风格":"中线波段"}
    else:
        # 洗盘检测
        if len(analyzer.df) < 40: return None
        df40 = analyzer.df.tail(40)
        if df40['high'].max() / df40['close'].iloc[0] < 1.10: return None
        if analyzer.df['volume'].tail(5).mean() / df40['volume'].iloc[0:20].mean() > 0.8: return None
        score = 80
        advice = {"介入价":f"{latest['Support']:.2f}", "风格":"洗盘低吸"}
        
    return {"code":code, "name":name, "sector":sector, "score":score, "close":f"{latest['close']:.2f}", "advice":advice}

def scan_stocks(strategy, top_n=10):
    stocks = get_active_stocks(200)
    results = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(analyze_single, s["code"], strategy, s["name"], s["sector"]): s for s in stocks}
        for f in as_completed(futures):
            res = f.result()
            if res: results.append(res)
    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:top_n]

def analyze_hot_stocks():
    top = get_active_stocks(20)
    results = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(StockAnalyzer(s["code"], s["name"]).fetch_data): StockAnalyzer(s["code"], s["name"]) for s in top}
        for f in as_completed(futures):
            az = futures[f]
            if f.result() and az.calculate_indicators():
                r = az.get_full_report()
                if "error" not in r:
                    # 匹配板块信息
                    sector = next((item['sector'] for item in top if item['code'] == az.symbol), "")
                    results.append({"code":az.symbol,"name":az.name,"sector":sector,"price":r["price"],
                                    "change":r["change"],"intent":r["intent"],"score":r["score"],
                                    "kdj_j":r["kdj"]["J"],"vol_ratio":r["vol_ratio"]})
    results.sort(key=lambda x: x['score'], reverse=True)
    return results

# ================== Flask 应用与高级前端模板 ==================
HTML = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PRO-QUANT 全功能量价终端 V6.0</title>
    <link href="https://cdn.bootcdn.net/ajax/libs/twitter-bootstrap/5.3.0/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
    <style>
        :root { --bg: #0d1117; --panel: #161b22; --border: #30363d; --text: #c9d1d9; --acc: #2ea043; }
        body { background: var(--bg); color: var(--text); font-family: -apple-system, sans-serif; }
        .navbar-custom { background: #010409; border-bottom: 1px solid var(--border); padding: 15px 0; }
        .main-container { max-width: 1400px; margin: 2rem auto; padding: 0 15px; }
        .card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; margin-bottom: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.5); }
        .card-header-custom { background: rgba(255,255,255,0.02); border-bottom: 1px solid var(--border); padding: 15px 20px; font-weight: bold; border-radius: 12px 12px 0 0; }
        .btn-primary { background: var(--acc); border: none; font-weight: 600; }
        .btn-primary:hover { background: #3fb950; }
        .form-control { background: #010409; border: 1px solid var(--border); color: #fff; }
        .form-control:focus { background: #010409; color: #fff; border-color: var(--acc); box-shadow: none; }
        
        .metric-box { background: #010409; border: 1px solid var(--border); border-radius: 8px; padding: 15px; text-align: center; }
        .metric-title { font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 5px; }
        .metric-value { font-size: 24px; font-weight: 700; color: #f0f6fc; }
        
        .stock-card { cursor: pointer; transition: 0.2s; border-left: 4px solid transparent; }
        .stock-card:hover { border-color: var(--acc); transform: translateX(5px); background: #1c2128; }
        .text-up { color: #f85149 !important; }
        .text-down { color: #3fb950 !important; }
        
        .loader-overlay { display: none; position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: rgba(13,17,23,0.8); z-index: 10; justify-content: center; align-items: center; border-radius: 12px; flex-direction: column; }
        #chart-container { height: 400px; width: 100%; margin-top: 10px; }
    </style>
</head>
<body>

<nav class="navbar-custom">
    <div class="container-fluid px-4">
        <span class="text-success fw-bold fs-4">🛰️ PRO-QUANT COMMAND CENTER V6.0 (双核稳定版)</span>
    </div>
</nav>

<div class="main-container">
    <div class="row">
        <div class="col-lg-3">
            <div class="card position-relative">
                <div class="card-header-custom">🎯 标的解析引擎</div>
                <div class="card-body">
                    <input type="text" id="stockCode" class="form-control mb-3" placeholder="股票名称/代码/拼音" value="省广集团">
                    <input type="number" id="costPrice" class="form-control mb-3" placeholder="持仓成本(可选)">
                    <button class="btn btn-primary w-100 py-2" onclick="analyze()">🚀 执行全维度诊断</button>
                </div>
            </div>

            <div class="card">
                <div class="card-header-custom">🔍 策略扫描雷达</div>
                <div class="card-body">
                    <div class="d-grid gap-2">
                        <button class="btn btn-outline-info" onclick="scan('short')">⚡ 短线突击 (一日游)</button>
                        <button class="btn btn-outline-success" onclick="scan('band')">🌊 中线趋势 (波段)</button>
                        <button class="btn btn-outline-warning" onclick="scan('washout')">🛁 洗盘识别 (黄金坑)</button>
                    </div>
                    <hr class="border-secondary my-4">
                    <button class="btn btn-outline-danger w-100" onclick="hot()">🔥 异动热点 Top20</button>
                </div>
            </div>
        </div>

        <div class="col-lg-9">
            <div class="card position-relative" style="min-height: 700px;">
                <div class="loader-overlay" id="mainLoader">
                    <div class="spinner-border text-success mb-3" role="status"></div>
                    <div class="text-success">正在通过双核引擎获取数据...</div>
                </div>
                
                <div class="card-header-custom d-flex justify-content-between align-items-center">
                    <span>📺 可视化情报看板</span>
                    <span id="updateTime" class="badge bg-secondary">等待指令</span>
                </div>
                
                <div class="card-body" id="displayArea">
                    <div class="text-center text-secondary mt-5 pt-5">
                        <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="mb-3">
                            <path d="M21 12V7H5a2 2 0 0 1 0-4h14v4"></path><path d="M3 5v14a2 2 0 0 0 2 2h16v-5"></path><path d="M18 12a2 2 0 0 0 0 4h4v-4Z"></path>
                        </svg>
                        <h5>系统就绪，请在左侧输入标的或启动雷达扫描</h5>
                        <p class="small">Dual-Core Engine Powered by EastMoney & Tencent API</p>
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>

<script>
    let klineChart = null;

    function showLoader() { document.getElementById('mainLoader').style.display = 'flex'; }
    function hideLoader() { document.getElementById('mainLoader').style.display = 'none'; }
    function updateTime() { document.getElementById('updateTime').innerText = new Date().toLocaleTimeString(); }

    async function analyze() {
        const stock = document.getElementById('stockCode').value;
        const cost = document.getElementById('costPrice').value;
        if(!stock) return;
        
        showLoader();
        const res = await fetch('/analyze', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({stock, cost})
        });
        const data = await res.json();
        hideLoader();
        updateTime();

        if(data.error) {
            document.getElementById('displayArea').innerHTML = `<div class="alert alert-danger mt-4 text-center">❌ ${data.error}</div>`;
            return;
        }
        
        const r = data.report;
        const colorClass = r.change >= 0 ? 'text-up' : 'text-down';
        const intentColor = r.intent === '缩量洗盘' ? '#3fb950' : (r.intent === '恐慌砸盘' ? '#f85149' : '#58a6ff');

        let html = `
            <div class="d-flex justify-content-between align-items-end mb-4">
                <div>
                    <h2 class="mb-0 text-white">${r.name} <span class="fs-5 text-secondary">${r.code}</span></h2>
                    <div class="mt-2 text-secondary">支撑: ${r.support} | 压力: ${r.resistance} | 筹码密集峰: ${r.chip_peak}</div>
                </div>
                <div class="text-end">
                    <h1 class="${colorClass} mb-0">${r.price}</h1>
                    <span class="${colorClass} fs-5">${r.change >= 0 ? '+'+r.change : r.change}%</span>
                </div>
            </div>

            <div class="row g-3 mb-4">
                <div class="col-md-3"><div class="metric-box"><div class="metric-title">主力意图</div><div class="metric-value" style="color:${intentColor}">${r.intent}</div></div></div>
                <div class="col-md-3"><div class="metric-box"><div class="metric-title">KDJ (J值)</div><div class="metric-value">${r.kdj.J}</div></div></div>
                <div class="col-md-3"><div class="metric-box"><div class="metric-title">动能量比</div><div class="metric-value">${r.vol_ratio}</div></div></div>
                <div class="col-md-3"><div class="metric-box"><div class="metric-title">策略评分</div><div class="metric-value text-success">${r.score}</div></div></div>
            </div>
            
            <div id="chart-container"></div>
        `;
        
        if (cost) {
            const profit = ((r.price - cost) / cost * 100).toFixed(2);
            const pClass = profit >= 0 ? 'text-up' : 'text-down';
            html += `<div class="mt-3 text-center text-secondary">持仓成本: ${cost} | 盈亏估算: <span class="${pClass} fw-bold">${profit > 0 ? '+'+profit : profit}%</span></div>`;
        }
        
        document.getElementById('displayArea').innerHTML = html;
        renderChart(r.chart_data);
    }

    function renderChart(chartData) {
        const seriesData = chartData.map(item => ({ x: new Date(item.date).getTime(), y: [item.open, item.high, item.low, item.close] }));
        const ma5Data = chartData.map(item => ({ x: new Date(item.date).getTime(), y: item.ma5 }));
        const ma20Data = chartData.map(item => ({ x: new Date(item.date).getTime(), y: item.ma20 }));

        const options = {
            series: [
                { name: 'K线', type: 'candlestick', data: seriesData },
                { name: 'MA5', type: 'line', data: ma5Data },
                { name: 'MA20', type: 'line', data: ma20Data }
            ],
            chart: { height: 400, type: 'line', background: 'transparent', toolbar: { show: true } },
            theme: { mode: 'dark' },
            stroke: { width: [1, 2, 2], curve: 'smooth' },
            colors: ['#ffffff', '#f4ce14', '#00e676'],
            plotOptions: { candlestick: { colors: { upward: '#f85149', downward: '#3fb950' }, wick: { useFillColor: true } } },
            xaxis: { type: 'datetime', labels: { style: { colors: '#8b949e' }, datetimeUTC: false } },
            yaxis: { tooltip: { enabled: true }, labels: { style: { colors: '#8b949e' } } },
            grid: { borderColor: '#30363d', strokeDashArray: 3 },
            legend: { position: 'top', horizontalAlign: 'left' }
        };

        if(klineChart) { klineChart.destroy(); }
        klineChart = new ApexCharts(document.querySelector("#chart-container"), options);
        klineChart.render();
    }

    async function scan(strategy) {
        showLoader();
        const res = await fetch('/scan', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({strategy}) });
        const data = await res.json();
        hideLoader(); updateTime();

        let title = strategy === 'short' ? '⚡ 短线突击榜' : (strategy === 'band' ? '🌊 中线波段榜' : '🛁 洗盘黄金坑');
        let html = `<h4 class="mb-4 text-white border-bottom border-secondary pb-2">${title}</h4><div class="row g-3">`;
        
        data.results.forEach(r => {
            html += `
            <div class="col-md-6">
                <div class="card p-3 stock-card h-100" onclick="quickAnalyze('${r.code}')">
                    <div class="d-flex justify-content-between mb-2">
                        <strong class="text-white fs-5">${r.name}</strong>
                        <span class="badge bg-success fs-6">${r.score} 分</span>
                    </div>
                    <div class="text-secondary small mb-2">${r.code} | ${r.sector || '板块未知'}</div>
                    <div class="d-flex justify-content-between align-items-center mt-auto border-top border-secondary pt-2">
                        <span class="text-white">现价 <b class="fs-5">${r.close}</b></span>
                        <span class="text-info small">建议介入位: ${r.advice.介入价}</span>
                    </div>
                </div>
            </div>`;
        });
        html += '</div>';
        document.getElementById('displayArea').innerHTML = html;
    }

    async function hot() {
        showLoader();
        const res = await fetch('/hot');
        const data = await res.json();
        hideLoader(); updateTime();

        let html = `<h4 class="mb-4 text-danger border-bottom border-secondary pb-2">🔥 市场资金异动 Top20 (按量比排序)</h4><div class="row g-3">`;
        data.stocks.forEach(r => {
            const colorClass = r.change >= 0 ? 'text-up' : 'text-down';
            html += `
            <div class="col-md-6">
                <div class="card p-3 stock-card h-100" onclick="quickAnalyze('${r.code}')">
                    <div class="d-flex justify-content-between mb-2">
                        <strong class="text-white fs-5">${r.name}</strong>
                        <strong class="${colorClass} fs-5">${r.change >= 0 ? '+'+r.change : r.change}%</strong>
                    </div>
                    <div class="row text-secondary small text-center mt-2">
                        <div class="col-4 border-end border-secondary">量比<br><b class="text-warning">${r.vol_ratio}</b></div>
                        <div class="col-4 border-end border-secondary">J值<br><b class="text-white">${r.kdj_j}</b></div>
                        <div class="col-4">意图<br><b class="text-info">${r.intent}</b></div>
                    </div>
                </div>
            </div>`;
        });
        html += '</div>';
        document.getElementById('displayArea').innerHTML = html;
    }

    function quickAnalyze(code) { document.getElementById('stockCode').value = code; analyze(); }
</script>
</body>
</html>'''

app = Flask(__name__)

@app.route('/')
def home(): return render_template_string(HTML)

@app.route('/analyze', methods=['POST'])
def analyze():
    d = request.get_json()
    stock_input = d.get('stock')
    code, name = resolve_stock_input(stock_input)
    if not code: return jsonify({"error":"无法识别股票，请检查输入"})
    az = StockAnalyzer(code, name, d.get('cost'))
    if not az.fetch_data(): return jsonify({"error":"行情数据获取失败，服务器双核接口均超时。"})
    rep = az.get_full_report()
    if "error" in rep: return jsonify({"error":rep["error"]})
    return jsonify({"report":rep})

@app.route('/scan', methods=['POST'])
def scan():
    strategy = request.get_json().get('strategy','washout')
    results = scan_stocks(strategy, 10)
    return jsonify({"results":results})

@app.route('/hot')
def hot():
    stocks = analyze_hot_stocks()
    return jsonify({"stocks":stocks})

if __name__ == '__main__':
    print("✅ PRO-QUANT V6.0 终极双核终端已启动")
    print("👉 请在浏览器中打开: http://127.0.0.1:10000")
    app.run(host='0.0.0.0', port=10000)
