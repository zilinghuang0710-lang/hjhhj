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

# ================== 常量 ==================
KDJ_WINDOW = 9
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
RSI_PERIOD = 14
BOLL_PERIOD = 20
BOLL_WIDTH = 2
SUPPORT_RESIST_WINDOW = 20
CHIP_DAYS = 60
KLINE_LIMIT = 240 

active_cache = {"data": None, "time": 0}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

def create_session():
    session = requests.Session()
    retry = urllib3.util.retry.Retry(total=1, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
    adapter = requests.adapters.HTTPAdapter(max_retries=retry, pool_connections=150, pool_maxsize=150)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.headers.update({"User-Agent": random.choice(USER_AGENTS)})
    session.verify = False
    return session

http_session = create_session()

# ================== 活跃股票池（双源 + 真实白马股兜底防止死代码卡网） ==================
def get_hardcoded_fallback_stocks():
    """使用真实的活跃资产作为兜底，彻底防止死代码打穿网络连接池"""
    codes = [
        ("000001", "平安银行"), ("000002", "万科A"), ("000063", "中兴通讯"), ("000100", "TCL科技"),
        ("000538", "云南白药"), ("000725", "京东方A"), ("000858", "五粮液"), ("002230", "科大讯飞"),
        ("002304", "洋河股份"), ("002400", "省广集团"), ("002415", "海康威视"), ("002475", "立讯精密"),
        ("002594", "比亚迪"), ("002714", "牧原股份"), ("300014", "亿纬锂能"), ("300015", "爱尔眼科"),
        ("300059", "东方财富"), ("300124", "汇川技术"), ("300274", "阳光电源"), ("300750", "宁德时代"),
        ("600000", "浦发银行"), ("600030", "中信证券"), ("600036", "招商银行"), ("600104", "上汽集团"),
        ("600276", "恒瑞医药"), ("600519", "贵州茅台"), ("600900", "长江电力"), ("601012", "隆基绿能"),
        ("601166", "兴业银行"), ("601318", "中国平安"), ("601888", "中国中免"), ("688981", "中芯国际")
    ]
    stocks = [{"code": c[0], "name": c[1], "volume_ratio": 1.0, "sector": "核心资产"} for c in codes]
    random.shuffle(stocks) # 随机打乱
    return stocks

def get_active_stocks(limit=400):
    global active_cache
    now = time.time()
    if active_cache["data"] is not None and (now - active_cache["time"]) < 1800:
        return active_cache["data"][:limit]

    stocks = []
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": str(limit), "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2",
            "fid": "f20", "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f14,f10,f20,f100", "_": str(int(now*1000))
        }
        resp = http_session.get(url, params=params, headers={"Referer": "https://quote.eastmoney.com/"}, timeout=4).json()
        if resp and resp.get("data") and resp["data"].get("diff"):
            for item in resp["data"]["diff"]:
                code, name = item.get("f12"), item.get("f14")
                if not code or "ST" in name: continue
                stocks.append({"code": code, "name": name, "volume_ratio": item.get("f20", 1), "sector": item.get("f100", "")})
    except: pass

    if len(stocks) < 50:
        try:
            tx_url = "https://web.ifzq.gtimg.cn/appstock/app/rank/total?type=1&num=200"
            tx_data = http_session.get(tx_url, timeout=4).json()
            if tx_data and tx_data.get("data"):
                for stock in tx_data["data"].get("stocks", []):
                    code, name = stock.get("code"), stock.get("name")
                    if code and "ST" not in name:
                        stocks.append({"code": code, "name": name, "volume_ratio": 1.0, "sector": ""})
        except: pass

    # 终极兜底
    if not stocks:
        stocks = get_hardcoded_fallback_stocks()
        
    unique_stocks = list({s['code']: s for s in stocks}.values())
    active_cache["data"] = unique_stocks
    active_cache["time"] = now
    return unique_stocks[:limit]

# ================== 股票搜索 ==================
def resolve_stock_input(keyword):
    keyword = str(keyword).strip()
    if re.match(r'^\d{6}$', keyword): return keyword, f"A股_{keyword}"
    try:
        resp = http_session.get("https://searchapi.eastmoney.com/api/suggest/get", params={
            "input":keyword,"type":"14","token":"D43BF722C8E33BDC906FB84D85E326E8","count":"1"}, timeout=3)
        if resp.status_code==200:
            data = resp.json()
            if data and data.get("QuotationCodeTable") and data["QuotationCodeTable"]["Data"]:
                item = data["QuotationCodeTable"]["Data"][0]
                return item["Code"], item["Name"]
    except: pass
    return keyword, keyword 

# ================== 数据安全处理 ==================
def safe_float(val):
    try:
        v = float(val)
        return 0.0 if math.isnan(v) or math.isinf(v) else v
    except: return 0.0

def safe_date(date_str):
    date_str = str(date_str).strip()
    if len(date_str) == 8 and date_str.isdigit():
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    return date_str.replace("/", "-")

# ================== K线分析引擎 ==================
class StockAnalyzer:
    def __init__(self, symbol, name, cost_price=None):
        self.symbol = symbol
        self.name = name
        self.cost_price = safe_float(cost_price) if cost_price else None
        self.df = pd.DataFrame()
        self.df_w = pd.DataFrame()
        self.chip_peak = 0.0

    def fetch_data(self, end_date=None):
        try:
            return self._fetch_data_inner(end_date)
        except: return False

    def _fetch_data_inner(self, end_date=None):
        mkt = "0" if self.symbol.startswith(("0","3")) else "1"
        url_em = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params_em = {
            "secid": f"{mkt}.{self.symbol}",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101", "fqt": "1",
            "end": end_date if end_date else "20500101",
            "lmt": str(KLINE_LIMIT)
        }
        try:
            # 宽容的连接时间，防止网络波动卡死
            resp = http_session.get(url_em, params=params_em, timeout=(3, 5))
            if resp.status_code == 200:
                data = resp.json()
                if data and data.get("data") and data["data"].get("klines"):
                    klines = data["data"]["klines"]
                    if len(klines) >= 10:
                        parsed = []
                        for item in klines:
                            p = item.split(",")
                            parsed.append({
                                "date": p[0], "open": safe_float(p[1]), "close": safe_float(p[2]),
                                "high": safe_float(p[3]), "low": safe_float(p[4]), "volume": safe_float(p[5]),
                                "turnover": safe_float(p[10])
                            })
                        self.df = pd.DataFrame(parsed)
                        return True
        except: pass
        
        prefix = "sz" if self.symbol.startswith(("0","3")) else "sh"
        tx_url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{self.symbol},day,,,{KLINE_LIMIT},qfq"
        try:
            resp = http_session.get(tx_url, timeout=(3, 5))
            if resp.status_code == 200:
                data = resp.json()
                if data and data.get("code") == 0:
                    stock_data = data["data"].get(f"{prefix}{self.symbol}")
                    if stock_data:
                        kline = stock_data.get("qfqday") or stock_data.get("day")
                        if kline and len(kline) >= 10:
                            self.df = pd.DataFrame([{
                                "date": i[0], "open": safe_float(i[1]), "close": safe_float(i[2]),
                                "high": safe_float(i[3]), "low": safe_float(i[4]), "volume": safe_float(i[5]),
                                "turnover": 0.0
                            } for i in kline])
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
        
        df['Support'] = df['low'].rolling(SUPPORT_RESIST_WINDOW).min()
        df['Resistance'] = df['high'].rolling(SUPPORT_RESIST_WINDOW).max()
        
        recent = df.tail(CHIP_DAYS)
        if not recent.empty and recent['close'].max() > recent['close'].min():
            bins = np.linspace(recent['close'].min(), recent['close'].max(), 11)
            cuts = pd.cut(recent['close'], bins=bins)
            try: dist = recent.groupby(cuts, observed=False)['volume'].sum()
            except TypeError: dist = recent.groupby(cuts)['volume'].sum()
            self.chip_peak = dist.idxmax().mid if not dist.empty else recent['close'].iloc[-1]
            
        df = df.replace([np.inf, -np.inf], np.nan).fillna(0)
        self.df = df

        try:
            df['date_dt'] = pd.to_datetime(df['date'])
            df_w = df.set_index('date_dt').resample('W-FRI').agg({
                'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
            }).dropna()
            if len(df_w) >= 2:
                df_w['MA5'] = df_w['close'].rolling(5, min_periods=1).mean()
                df_w['MA10'] = df_w['close'].rolling(10, min_periods=1).mean()
                df_w['VMA5'] = df_w['volume'].rolling(5, min_periods=1).mean()
                l9_w = df_w['low'].rolling(9, min_periods=1).min()
                h9_w = df_w['high'].rolling(9, min_periods=1).max()
                denom_w = h9_w - l9_w
                rsv_w = np.where(denom_w==0, 50, (df_w['close']-l9_w)/denom_w*100)
                df_w['K'] = pd.Series(rsv_w, index=df_w.index).ewm(com=2, adjust=False).mean()
                df_w['D'] = df_w['K'].ewm(com=2, adjust=False).mean()
                df_w['J'] = 3*df_w['K'] - 2*df_w['D']
                self.df_w = df_w.replace([np.inf, -np.inf], np.nan).fillna(0)
            else: self.df_w = pd.DataFrame()
        except: self.df_w = pd.DataFrame()
            
        return True

    def get_full_report(self):
        if not self.calculate_indicators(): return {"error": "数据不足或计算异常，可能是退市、停牌或无数据股票"}
        latest = self.df.iloc[-1]
        prev = self.df.iloc[-2]
        vol_ratio = latest['volume'] / (latest['VMA5'] if latest['VMA5'] > 0 else 1)
        
        logic_texts = []
        if self.cost_price:
            if self.cost_price < self.chip_peak: logic_texts.append(f"✅ [筹码优势] 你的成本({self.cost_price:.2f})在主力筹码峰({self.chip_peak:.2f})下方或附近，具备战略优势。")
            else: logic_texts.append(f"⚠️ [筹码劣势] 你的成本({self.cost_price:.2f})高于筹码峰({self.chip_peak:.2f})，需警惕解套盘抛压。")
                
        if vol_ratio < 0.8: logic_texts.append("✅ [量价特征] 成交量平稳或萎缩，符合'抛压枯竭'洗盘特征。")
        elif vol_ratio > 1.5: logic_texts.append("🚀 [量价特征] 底部放量显著，资金介入特征明显。")
        else: logic_texts.append("↔️ [量价特征] 均量运行，当前处于蓄势观察期。")
            
        if latest['J'] < 20: logic_texts.append("✅ [技术反弹] J值极度超卖 (<20)，有反抽需求。")
        elif latest['J'] > 80: logic_texts.append("⚠️ [技术风险] J值超买 (>80)，短线面临回调压力。")

        logic_str = "\n  ".join(logic_texts) if logic_texts else "数据平衡无明显异动。"

        w_score = 0
        if not self.df_w.empty:
            latest_w = self.df_w.iloc[-1]
            ma_w_status = f"周线多头排列 (MA5 > MA10)" if latest_w['MA5'] > latest_w['MA10'] else f"周线空头排列 (MA5 < MA10)"
            vol_ratio_w = latest_w['volume'] / (latest_w['VMA5'] if latest_w['VMA5']>0 else 1)
            
            if latest_w['MA5'] > latest_w['MA10']: w_score += 1
            else: w_score -= 1
            if latest_w['J'] <= 20: w_score += 1
            elif latest_w['J'] >= 80: w_score -= 1
            
            w_text = f"""📊 周均线状态：{ma_w_status}\n  📈 周线KDJ  K:{latest_w['K']:.1f}  D:{latest_w['D']:.1f}  J:{latest_w['J']:.1f}\n  📊 本周成交量:{int(latest_w['volume'])}手  5周均量:{int(latest_w['VMA5'])}手  量比:{vol_ratio_w:.2f}\n  🧩 周线综合影响评分： {w_score}"""
        else:
            w_text = "数据不足，无法计算周线趋势。"

        advice = "🟡 综合评级：【谨慎观望】\n   核心逻辑：盘面混沌。以支撑位为底线，不破不走，不放量不加仓。"
        if w_score >= 1 and str(logic_str).count('✅') >= 2: advice = "🔴 综合评级：【积极看多】\n   核心逻辑：中短线共振向上，缩量回踩可分批低吸。"
        elif w_score < 0 and str(logic_str).count('⚠️') >= 2: advice = "🟢 综合评级：【防守减仓】\n   核心逻辑：趋势走坏且面临抛压，逢高降低仓位。"

        txt_report = f"""=======================================================
 🤖 【{self.name} ({self.symbol})】 主力量价深度诊断（日线+周线）
=======================================================
【日线核心参数】
🔸 最新收盘: {latest['close']:.2f} 元  |  🔸 持仓成本: {self.cost_price if self.cost_price else '未填写'} 元
🔸 支撑: {latest['Support']:.2f} | 压力: {latest['Resistance']:.2f} | 筹码峰: {self.chip_peak:.2f}
🔸 KDJ(J): {latest['J']:.2f}  |  5日均量: {int(latest['VMA5'])}手  |  换手: {latest['turnover']:.2f}%

【日线逻辑推演】
  {logic_str}

【周线中期趋势】
  {w_text}

>>> 最终操作建议 <<<
{advice}
==========================================="""

        is_wash, _, _ = self.is_washout_pattern()
        intent = "区间震荡"
        if latest['close'] > prev['close'] and vol_ratio > 1.3: intent = "放量启动"
        elif is_wash: intent = "缩量洗盘"

        chart_data = []
        for _, row in self.df.tail(80).iterrows():
            chart_data.append({
                "date": safe_date(row['date']),
                "open": safe_float(row['open']), "high": safe_float(row['high']),
                "low": safe_float(row['low']), "close": safe_float(row['close']),
                "ma5": safe_float(row['MA5']), "ma20": safe_float(row['MA20']),
                "volume": safe_float(row['volume'])
            })

        return {
            "name": self.name, "code": self.symbol, "price": safe_float(latest['close']),
            "change": round((latest['close']-prev['close'])/prev['close']*100,2) if prev['close']!=0 else 0,
            "kdj": {"K":safe_float(latest['K']),"D":safe_float(latest['D']),"J":safe_float(latest['J'])},
            "vol_ratio": safe_float(vol_ratio), "intent": intent,
            "score": 85 if intent in ["放量启动","缩量洗盘"] else 50,
            "text_report": txt_report, "chart_data": chart_data
        }

    def is_washout_pattern(self):
        if len(self.df) < 40: return False, 0, ""
        df40 = self.df.tail(40)
        if df40['high'].max() / (df40['close'].iloc[0]+0.01) < 1.06: return False, 0, ""
        recent10 = self.df.tail(10)
        if (recent10['high'].max()-recent10['low'].min())/(recent10['low'].min()+0.01) > 0.18: return False, 0, ""
        if self.df['volume'].tail(5).mean() / (df40['volume'].iloc[0:20].mean()+0.01) > 0.85: return False, 0, ""
        return True, 80, "缩量洗盘"

    def score_band(self):
        latest = self.df.iloc[-1]
        score = 0
        if latest['MA5'] > latest['MA20']: score += 2
        if latest['close'] > latest['MA20']: score += 1
        if 20 < latest['J'] < 80: score += 1
        return score

# ================== 策略与回测 ==================
def analyze_single_scan(code, strategy, name, sector):
    az = StockAnalyzer(code, name)
    if not az.fetch_data() or not az.calculate_indicators(): return None
    latest = az.df.iloc[-1]
    if strategy == "short":
        score = 0
        if latest['J'] < 35: score += 2
        if latest['close'] > az.df.iloc[-2]['close'] and latest['volume']/(latest['VMA5']+1) > 1.1: score += 2
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
    ex = ThreadPoolExecutor(max_workers=50)
    futures = {ex.submit(analyze_single_scan, s["code"], strategy, s["name"], s["sector"]): s for s in stocks}
    
    try:
        for f in as_completed(futures.keys()):
            res = f.result()
            if res:
                results.append(res)
                if len(results) >= top_n + 5: break
    finally:
        for f in futures.keys(): f.cancel()
        ex.shutdown(wait=False)

    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:top_n]

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
        future = full.df[full.df['date'] > str(test_date)].head(hold_days)
        if len(future) < hold_days: continue
        buy_price = full.df[full.df['date'] <= str(test_date)].iloc[-1]['close']
        sell_price = future.iloc[-1]['close']
        profit = (sell_price - buy_price) / (buy_price+0.01) * 100
        results.append({"code":code, "name":full.name, "profit": profit, "success": profit > 0})
    if not results: return None
    acc = sum(1 for r in results if r['success'])/len(results)*100
    return {"date":test_date_str, "strategy":strategy, "hold":hold_days, "total":len(results), "accuracy":acc, "picks":results}

# ================== Flask路由 ==================
app = Flask(__name__)

@app.route('/')
def home(): return render_template_string(HTML)

@app.route('/analyze', methods=['POST'])
def analyze():
    d = request.get_json()
    code, name = resolve_stock_input(d.get('stock'))
    if not code: return jsonify({"error":"无法识别股票或股票不存在"})
    az = StockAnalyzer(code, name, d.get('cost'))
    if not az.fetch_data(): return jsonify({"error":"网络连接异常或该股票近期无数据（可能是退市/停牌）"})
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
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {}
        for h in holdings:
            code, _ = resolve_stock_input(h['code'])
            if code: futures[ex.submit(StockAnalyzer(code, code).fetch_data)] = (code, safe_float(h['cost']), safe_float(h['weight']))
        for f in as_completed(futures):
            code, cost, weight = futures[f]
            az = StockAnalyzer(code, code)
            if f.result() and az.calculate_indicators():
                curr_p = az.df.iloc[-1]['close']
                profit_pct = (curr_p - cost)/(cost+0.01)*100
                res_text += f"🔹 {code} 现价:{curr_p:.2f} 盈亏:{profit_pct:+.2f}% 状态:{'✅ 健康' if az.df.iloc[-1]['J']<80 else '⚠️ 高位'}\n"
    res_text += "="*40
    return jsonify({"report": res_text})

@app.route('/backtest', methods=['POST'])
def run_bt():
    d = request.get_json()
    res = run_backtest(d.get('strategy'), d.get('date'), int(d.get('hold_days')))
    if not res: return jsonify({"error":"当日无信号或历史数据不全"})
    txt = f"📅 回测: {res['date']} | 策略: {res['strategy']} | 持有{res['hold']}天\n🎯 信号数:{res['total']}  | 胜率:{res['accuracy']:.1f}%\n"+"-"*30+"\n"
    for p in res['picks']: txt += f" {p['name']}({p['code']}) 收益:{p['profit']:+.2f}% {'✅' if p['success'] else '❌'}\n"
    return jsonify({"report": txt})

# ================== 前端 UI (终极版：图表必现 + 强制白字护眼深色) ==================
HTML = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PRO-QUANT V13 (图表必现修复版)</title>
    <link href="https://cdn.bootcdn.net/ajax/libs/twitter-bootstrap/5.3.0/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.bootcdn.net/ajax/libs/bootstrap/5.3.0/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
    <style>
        /* 严苛的高对比深色模式配色方案 */
        body { background-color: #0f172a; color: #f8fafc; font-family: -apple-system, sans-serif; }
        
        .card { background-color: #1e293b; border: 1px solid #334155; border-radius: 10px; margin-bottom: 15px; }
        .card-header-custom { background-color: #0f172a; border-bottom: 1px solid #334155; padding: 12px 15px; font-weight: bold; border-radius: 10px 10px 0 0; color: #f8fafc; }
        
        /* 强制重写原生 input 和 select 的背景和字体颜色！彻底告别黑字 */
        input.form-control, select.form-select, textarea {
            background-color: #334155 !important;
            color: #ffffff !important;
            border: 1px solid #475569 !important;
        }
        input::placeholder { color: #94a3b8 !important; }
        
        .btn-primary { background-color: #10b981; border: none; color: #fff;} 
        .btn-primary:hover { background-color: #059669; }
        
        /* 报告输出框高亮调整 */
        .terminal-box { background-color: #020617; color: #a7f3d0; font-family: 'Consolas', monospace; padding: 15px; border-radius: 8px; border: 1px solid #334155; font-size: 14px; white-space: pre-wrap; line-height: 1.6; }
        
        .metric-box { background-color: #020617; border: 1px solid #334155; border-radius: 8px; padding: 10px; text-align: center; color: #ffffff;}
        .metric-title { color: #94a3b8; font-size: 13px; margin-bottom: 4px; }
        
        .stock-card { cursor: pointer; transition: 0.2s; } 
        .stock-card:hover { border-color: #10b981; background-color: #334155; }
        
        .text-up { color: #ef4444 !important; } /* A股红涨 */
        .text-down { color: #10b981 !important; } /* A股绿跌 */
        .text-secondary-light { color: #cbd5e1 !important; }
        
        /* 确保图表容器不坍塌 */
        #chart-container { height: 450px; width: 100%; margin-top: 20px; display: block; position: relative; }
    </style>
</head>
<body>
<nav class="navbar navbar-dark mb-3 border-bottom border-secondary" style="background-color: #020617;">
    <div class="container-fluid"><span class="navbar-brand fw-bold" style="color:#34d399;">🛰️ PRO-QUANT V13 (图表必现修复版)</span></div>
</nav>

<div class="container-fluid px-4">
    <div class="row">
        <div class="col-lg-3">
            <div class="card">
                <div class="card-header-custom">🎯 个股深度侦测</div>
                <div class="card-body">
                    <input type="text" id="s_code" class="form-control mb-2" placeholder="代码/名称/拼音" value="省广集团">
                    <input type="number" step="0.01" id="s_cost" class="form-control mb-3" placeholder="持仓成本(可选)">
                    <button class="btn btn-primary w-100 fw-bold" onclick="analyze()">🚀 执行侦测</button>
                </div>
            </div>
            
            <div class="card">
                <div class="card-header-custom">📡 策略扫描</div>
                <div class="card-body">
                    <button class="btn btn-outline-info w-100 mb-2 text-white" onclick="scan('short')">⚡ 短线突击</button>
                    <button class="btn btn-outline-success w-100 mb-2 text-white" onclick="scan('band')">🌊 中线波段</button>
                    <button class="btn btn-outline-warning w-100 text-white" onclick="scan('washout')">🛁 缩量洗盘坑</button>
                </div>
            </div>
            
            <div class="card">
                <div class="card-header-custom">🛠️ 资产与回测</div>
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
                        <button class="btn btn-sm btn-success" onclick="calcPort()">运算组合</button>
                    </div>
                    <hr style="border-color: #475569;">
                    <select id="bt_strat" class="form-select form-select-sm mb-1">
                        <option value="washout">洗盘策略</option>
                        <option value="short">短线策略</option>
                        <option value="band">波段策略</option>
                    </select>
                    <input type="date" id="bt_date" class="form-control form-control-sm mb-1">
                    <select id="bt_hold" class="form-select form-select-sm mb-2">
                        <option value="1">持有1天</option>
                        <option value="3">持有3天</option>
                        <option value="5">持有5天</option>
                    </select>
                    <button class="btn btn-sm btn-outline-danger w-100 text-white" onclick="runBt()">⏳ 历史回测</button>
                </div>
            </div>
        </div>

        <div class="col-lg-9">
            <div class="card" style="min-height: 800px;">
                <div class="card-header-custom d-flex justify-content-between align-items-center">
                    <span>📺 核心情报大屏</span>
                    <span id="loader" style="display:none; color:#34d399;">
                        <div class="spinner-border spinner-border-sm mr-2" role="status"></div> 核心引擎推演中...
                    </span>
                </div>
                <div class="card-body" id="displayArea">
                    <div class="text-center mt-5" style="color: #64748b;"><h3>等待指令...</h3></div>
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
        
        if(kChart) { kChart.destroy(); kChart = null; } // 渲染前摧毁旧画布
        
        document.getElementById('loader').style.display = 'block';
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 20000); // 放宽至 20 秒
        
        try {
            const res = await fetch('/analyze', {
                method: 'POST', headers: {'Content-Type':'application/json'},
                body: JSON.stringify({stock, cost}), signal: controller.signal
            });
            clearTimeout(timeout);
            const data = await res.json();
            document.getElementById('loader').style.display = 'none';
            
            if(data.error) { 
                document.getElementById('displayArea').innerHTML = `<div class="alert" style="background-color:#7f1d1d; color:#fff; border:none;">❌ ${data.error}</div>`; 
                return; 
            }
            
            const r = data.report;
            const color = r.change >= 0 ? 'text-up' : 'text-down';
            
            // 拼接框架，预留 <div id="chart-container"></div>
            document.getElementById('displayArea').innerHTML = `
            <div class="d-flex justify-content-between align-items-end mb-3">
                <h3 class="text-white mb-0 fw-bold">${r.name} <span class="fs-6" style="color:#94a3b8;">${r.code}</span></h3>
                <h2 class="${color} mb-0 fw-bold">${r.price.toFixed(2)} <span class="fs-6">${r.change >= 0 ? '+'+r.change : r.change}%</span></h2>
            </div>
            
            <div class="row g-2 mb-3">
                <div class="col-3"><div class="metric-box"><div class="metric-title">主力意图</div><div class="fw-bold" style="color:#38bdf8;">${r.intent}</div></div></div>
                <div class="col-3"><div class="metric-box"><div class="metric-title">KDJ (J值)</div><div class="fw-bold text-white">${r.kdj.J.toFixed(1)}</div></div></div>
                <div class="col-3"><div class="metric-box"><div class="metric-title">动能量比</div><div class="fw-bold text-white">${r.vol_ratio.toFixed(2)}</div></div></div>
                <div class="col-3"><div class="metric-box"><div class="metric-title">系统评分</div><div class="fw-bold" style="color:#34d399;">${r.score} 分</div></div></div>
            </div>
            
            <div id="chart-container"></div>
            
            <div class="mt-4">
                <button class="btn w-100 text-start" type="button" data-bs-toggle="collapse" data-bs-target="#textReport" aria-expanded="true" style="background-color:#1e293b; border:1px solid #334155; color:#f8fafc;">
                    📄 展开/折叠 深度量价诊断报告 (日线+周线)
                </button>
                <div class="collapse show mt-2" id="textReport">
                    <pre class="terminal-box">${r.text_report}</pre>
                </div>
            </div>`;
            
            // 确保 DOM 框架渲染完毕后，再触发图表绘制
            setTimeout(() => { 
                try { renderChart(r.chart_data); } 
                catch(err) { console.error("图表绘制错误:", err); }
            }, 100);
            
        } catch(e) {
            clearTimeout(timeout);
            document.getElementById('loader').style.display = 'none';
            document.getElementById('displayArea').innerHTML = `<div class="alert" style="background-color:#991b1b; color:#fff; border:none;">⏰ 请求超时或渲染崩溃: ${e.message}</div>`;
        }
    }

    function renderChart(data) {
        if(!data || data.length === 0) return;
        
        // 数据清洗转换
        const kLineData = data.map(i => ({ x: new Date(i.date).getTime(), y: [i.open, i.high, i.low, i.close] }));
        const ma20Data = data.map(i => ({ x: new Date(i.date).getTime(), y: i.ma20 }));
        const volData = data.map(i => ({ 
            x: new Date(i.date).getTime(), 
            y: i.volume, 
            fillColor: i.close >= i.open ? 'rgba(239,68,68,0.5)' : 'rgba(16,185,129,0.5)' // 红涨绿跌
        }));

        const options = {
            series: [
                { name: 'K线', type: 'candlestick', data: kLineData },
                { name: 'MA20', type: 'line', data: ma20Data },
                { name: '成交量', type: 'column', data: volData }
            ],
            chart: { height: 450, type: 'line', background: 'transparent', toolbar: { show: false } },
            theme: { mode: 'dark' },
            stroke: { width: [1, 2, 1], curve: 'smooth' },
            colors: ['#fff', '#fbbf24', '#fff'], // MA20用醒目的金黄色
            plotOptions: {
                candlestick: { colors: { upward: '#ef4444', downward: '#10b981' }, wick: { useFillColor: true } },
                bar: { columnWidth: '80%' }
            },
            xaxis: { type: 'datetime', labels: { style: { colors: '#94a3b8' } } },
            
            // 【核心修复】混合图表 3个 Series 必须完美对应 3个 Y轴配置
            yaxis: [
                {
                    seriesName: 'K线', // 绑定 K线
                    title: { text: '价格', style: { color: '#64748b' } },
                    labels: { style: { colors: '#94a3b8' }, formatter: val => val ? val.toFixed(2) : "" }
                },
                {
                    seriesName: 'K线', // 把 MA20 也绑定到 K线的 Y轴比例尺上！
                    show: false       // 隐藏该轴（因为它和K线是共用的）
                },
                {
                    seriesName: '成交量', // 绑定 成交量
                    opposite: true,     // 放在右边
                    title: { text: '成交量(手)', style: { color: '#64748b' } },
                    labels: { style: { colors: '#94a3b8' }, formatter: val => val ? (val/10000).toFixed(0) + 'w' : "" }
                }
            ],
            grid: { borderColor: '#334155', strokeDashArray: 3 },
            legend: { position: 'top', horizontalAlign: 'left', labels: { colors: '#f8fafc' } }
        };
        
        kChart = new ApexCharts(document.querySelector("#chart-container"), options);
        kChart.render();
    }

    async function scan(strategy) {
        document.getElementById('loader').style.display = 'block';
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 30000); // 扫描允许等30秒
        try {
            const res = await fetch('/scan', { 
                method: 'POST', headers: {'Content-Type':'application/json'}, 
                body: JSON.stringify({strategy}), signal: controller.signal
            });
            clearTimeout(timeout);
            const data = await res.json();
            document.getElementById('loader').style.display = 'none';
            
            let html = `<h5 class="mb-3 text-white border-bottom border-secondary pb-2">🎯 扫描结果 (点击卡片执行侦测)</h5><div class="row g-2">`;
            if(!data.results || data.results.length === 0) html += "<div style='color:#94a3b8;'>未扫描到符合模型要求的标的，或网络拥堵</div>";
            data.results.forEach(r => { html += `<div class="col-md-6"><div class="card p-2 stock-card h-100" onclick="quickAnalyze('${r.code}')"><div class="d-flex justify-content-between mb-1"><strong class="text-white">${r.name}</strong><span class="text-success small fw-bold">${r.score}分</span></div><div style="font-size:12px;color:#cbd5e1;">现价: ${r.close} | 介入位: <span style="color:#fcd34d;">${r.advice.介入价}</span></div></div></div>`; });
            html += '</div>';
            document.getElementById('displayArea').innerHTML = html;
        } catch(e) {
            clearTimeout(timeout);
            document.getElementById('loader').style.display = 'none';
            document.getElementById('displayArea').innerHTML = `<div class="alert" style="background-color:#991b1b; color:#fff; border:none;">⏰ 扫描请求超时或由于网络原因中断</div>`;
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
    print("✅ PRO-QUANT V13 (图表必现修复版) 启动成功！")
    print("👉 访问 http://127.0.0.1:10000")
    app.run(host='0.0.0.0', port=10000)
