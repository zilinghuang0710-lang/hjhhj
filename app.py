import pandas as pd
import numpy as np
import re
import urllib3
import requests
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, render_template_string

# 禁用 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================== 常量 ==================
KDJ_WINDOW = 9
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
RSI_PERIOD = 14
BOLL_PERIOD = 20
BOLL_WIDTH = 2
SUPPORT_RESIST_WINDOW = 20
CHIP_DAYS = 60
KLINE_LIMIT = 120

# ================== 网络会话 ==================
def create_session():
    session = requests.Session()
    retry = urllib3.util.retry.Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    session.verify = False
    return session

http_session = create_session()

# ================== 搜索股票 ==================
def resolve_stock_input(keyword):
    # ... 与原有代码完全相同 ...
    keyword = str(keyword).strip()
    if re.match(r'^\d{6}$', keyword):
        return keyword, f"A股_{keyword}"
    offline_dict = {
        "省广集团": "002400", "sgjt": "002400",
        "贵州茅台": "600519", "gzmt": "600519",
        "宁德时代": "300750", "ndsd": "300750",
        "中信证券": "600030", "zxzq": "600030",
        "东方财富": "300059", "dfcf": "300059"
    }
    if keyword in offline_dict:
        return offline_dict[keyword], keyword
    try:
        url_em = "https://searchapi.eastmoney.com/api/suggest/get"
        resp = http_session.get(url_em, params={
            "input": keyword, "type": "14",
            "token": "D43BF722C8E33BDC906FB84D85E326E8", "count": "1"
        }, timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            if "QuotationCodeTable" in data and data["QuotationCodeTable"]["Data"]:
                item = data["QuotationCodeTable"]["Data"][0]
                return item["Code"], item["Name"]
    except: pass
    try:
        url_tencent = "http://smartbox.gtimg.cn/s3/"
        resp = http_session.get(url_tencent, params={"v": 2, "q": keyword, "t": "all"}, timeout=3)
        resp.encoding = 'GBK'
        match = re.search(r'v_hint="(.*?)"', resp.text)
        if match:
            parts = match.group(1).split('^')[0].split(',')
            if len(parts) >= 2:
                code_match = re.search(r'\d{6}', parts[0])
                if code_match:
                    return code_match.group(0), parts[1]
    except: pass
    return None, None

# ================== 升级版分析引擎（增加洗盘检测与策略评分） ==================
class StockAnalyzer:
    def __init__(self, symbol, name, cost_price=None):
        self.symbol = symbol
        self.name = name
        self.cost_price = float(cost_price) if cost_price else None
        self.df = pd.DataFrame()
        self.weekly_df = pd.DataFrame()
        self.chip_peak = 0.0

    # ---------- 数据获取 (同前) ----------
    def fetch_data(self):
        if self._fetch_eastmoney():
            self._fetch_weekly_data()
            return True
        if self._fetch_tencent():
            self._generate_weekly_from_daily()
            return True
        return False

    def _fetch_eastmoney(self):
        mkt = "0" if self.symbol.startswith(("0", "3")) else "1"
        url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {"secid": f"{mkt}.{self.symbol}", "fields1": "f1,f2,f3,f4,f5,f6",
                  "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                  "klt": "101", "fqt": "1", "end": "20500101", "lmt": str(KLINE_LIMIT)}
        try:
            resp = http_session.get(url, params=params, timeout=5)
            if resp.status_code == 200 and "klines" in resp.json().get("data", {}):
                klines = resp.json()["data"]["klines"]
                parsed = [{"date": p[0], "open": float(p[1]), "close": float(p[2]),
                           "high": float(p[3]), "low": float(p[4]), "volume": float(p[5]),
                           "turnover": float(p[10])} for p in (item.split(",") for item in klines)]
                self.df = pd.DataFrame(parsed)
                self.df['date'] = pd.to_datetime(self.df['date'])
                return True
        except: pass
        return False

    def _fetch_tencent(self):
        prefix = "sz" if self.symbol.startswith(("0", "3")) else "sh"
        url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{self.symbol},day,,,{KLINE_LIMIT},qfq"
        try:
            resp = http_session.get(url, timeout=5)
            data = resp.json()
            if data.get("code") == 0:
                stock = data["data"].get(f"{prefix}{self.symbol}", {})
                kline = stock.get("qfqday") or stock.get("day")
                if kline:
                    parsed = [{"date": i[0], "open": float(i[1]), "close": float(i[2]),
                               "high": float(i[3]), "low": float(i[4]), "volume": float(i[5]),
                               "turnover": 0.0} for i in kline]
                    self.df = pd.DataFrame(parsed)
                    self.df['date'] = pd.to_datetime(self.df['date'])
                    return True
        except: pass
        return False

    def _fetch_weekly_data(self):
        mkt = "0" if self.symbol.startswith(("0", "3")) else "1"
        url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {"secid": f"{mkt}.{self.symbol}", "fields1": "f1,f2,f3,f4,f5,f6",
                  "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                  "klt": "102", "fqt": "1", "end": "20500101", "lmt": "100"}
        for _ in range(2):
            try:
                resp = http_session.get(url, params=params, timeout=8)
                if resp.status_code == 200 and "klines" in resp.json().get("data", {}):
                    klines = resp.json()["data"]["klines"]
                    if klines:
                        parsed = [{"date": p[0], "open": float(p[1]), "close": float(p[2]),
                                   "high": float(p[3]), "low": float(p[4]), "volume": float(p[5]),
                                   "turnover": float(p[10])} for p in (item.split(",") for item in klines)]
                        self.weekly_df = pd.DataFrame(parsed)
                        self.weekly_df['date'] = pd.to_datetime(self.weekly_df['date'])
                        return
            except: pass
        self._generate_weekly_from_daily()

    def _generate_weekly_from_daily(self):
        if self.df.empty: return
        temp = self.df.set_index('date')
        weekly = temp.resample('W').agg({'open':'first','close':'last','high':'max',
                                         'low':'min','volume':'sum','turnover':'sum'}).dropna().reset_index()
        self.weekly_df = weekly

    # ---------- 指标计算 ----------
    def calculate_indicators(self):
        if self.df.empty or len(self.df) < 20: return False
        df = self.df.copy()
        required = ['open','close','high','low','volume','turnover']
        for col in required:
            if col not in df.columns: return False
        df['MA5'] = df['close'].rolling(5).mean()
        df['MA10'] = df['close'].rolling(10).mean()
        df['MA20'] = df['close'].rolling(20).mean()
        df['VMA5'] = df['volume'].rolling(5).mean()
        l9 = df['low'].rolling(KDJ_WINDOW, min_periods=1).min()
        h9 = df['high'].rolling(KDJ_WINDOW, min_periods=1).max()
        denom = h9 - l9
        rsv = np.where(denom == 0, 50, (df['close'] - l9) / denom * 100)
        df['K'] = pd.Series(rsv, index=df.index).ewm(com=2, adjust=False).mean()
        df['D'] = df['K'].ewm(com=2, adjust=False).mean()
        df['J'] = 3*df['K'] - 2*df['D']
        ema12 = df['close'].ewm(span=MACD_FAST, adjust=False).mean()
        ema26 = df['close'].ewm(span=MACD_SLOW, adjust=False).mean()
        df['DIF'] = ema12 - ema26
        df['DEA'] = df['DIF'].ewm(span=MACD_SIGNAL, adjust=False).mean()
        df['MACD'] = 2*(df['DIF'] - df['DEA'])
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.ewm(alpha=1/RSI_PERIOD, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/RSI_PERIOD, adjust=False).mean()
        rs = avg_gain / avg_loss
        df['RSI'] = 100 - (100 / (1 + rs))
        df['BOLL_MID'] = df['close'].rolling(BOLL_PERIOD).mean()
        std = df['close'].rolling(BOLL_PERIOD).std()
        df['BOLL_UP'] = df['BOLL_MID'] + BOLL_WIDTH*std
        df['BOLL_DN'] = df['BOLL_MID'] - BOLL_WIDTH*std
        df['Support'] = df['low'].rolling(SUPPORT_RESIST_WINDOW).min()
        df['Resistance'] = df['high'].rolling(SUPPORT_RESIST_WINDOW).max()
        recent = df.tail(CHIP_DAYS)
        if not recent.empty:
            mn, mx = recent['close'].min(), recent['close'].max()
            if mx > mn:
                bins = np.linspace(mn, mx, 11)
                dist = recent.groupby(pd.cut(recent['close'], bins=bins))['volume'].sum()
                self.chip_peak = dist.idxmax().mid if not dist.empty else recent['close'].iloc[-1]
            else:
                self.chip_peak = recent['close'].iloc[-1]
        self.df = df
        if not self.weekly_df.empty:
            self._calc_weekly()
        return True

    def _calc_weekly(self):
        df = self.weekly_df.copy()
        if len(df) < 10: return
        df['MA5'] = df['close'].rolling(5).mean()
        df['MA10'] = df['close'].rolling(10).mean()
        df['VMA5'] = df['volume'].rolling(5).mean()
        l9 = df['low'].rolling(9, min_periods=1).min()
        h9 = df['high'].rolling(9, min_periods=1).max()
        denom = h9 - l9
        rsv = np.where(denom == 0, 50, (df['close'] - l9) / denom * 100)
        df['K'] = pd.Series(rsv, index=df.index).ewm(com=2, adjust=False).mean()
        df['D'] = df['K'].ewm(com=2, adjust=False).mean()
        df['J'] = 3*df['K'] - 2*df['D']
        df['Support'] = df['low'].rolling(20).min()
        df['Resistance'] = df['high'].rolling(20).max()
        self.weekly_df = df

    # ---------- 洗盘模型判断 ----------
    def is_washout_pattern(self):
        """返回 (是否为洗盘, 置信度分数, 描述)"""
        if len(self.df) < 40:
            return False, 0, "数据不足"
        df = self.df.tail(40)
        # 1. 前段拉升：20日内涨幅>12%
        price_start = df['close'].iloc[0]
        price_peak = df['high'].iloc[-20:-1].max()
        if price_peak / price_start < 1.12:
            return False, 0, "无前期拉升"
        # 2. 近10日横盘：振幅<10%，价格围绕MA20
        recent10 = df.tail(10)
        high_10 = recent10['high'].max()
        low_10 = recent10['low'].min()
        if (high_10 - low_10) / low_10 > 0.10:
            return False, 0, "横盘幅度过大"
        ma20 = df['MA20'].tail(10)
        if (recent10['close'].values < ma20.values * 0.98).any():
            return False, 0, "未守住MA20"
        # 3. 缩量：近5日均量 < 前拉升期均量的0.7倍
        vol_rise = df['volume'].iloc[10:30].mean()
        vol_now = df['volume'].tail(5).mean()
        if vol_now / vol_rise > 0.7:
            return False, 0, "缩量不充分"
        # 4. 最近3天有缩量下跌但不破支撑
        last_3 = df.tail(3)
        if last_3['close'].iloc[-1] < last_3['close'].iloc[0] and last_3['volume'].mean() < df['VMA5'].tail(3).mean() * 0.8:
            score = 80
            desc = "拉升后横盘缩量洗盘，当前缩量下探支撑，主力未出货"
        else:
            score = 60
            desc = "横盘缩量中，等待进一步缩量确认"
        return True, score, desc

    # ---------- 短线/波段策略评分 ----------
    def score_short_term(self):
        """短线一日游评分，高分适合快进快出"""
        if not self.df.empty and len(self.df) >= 20:
            latest = self.df.iloc[-1]
            prev = self.df.iloc[-2]
            score = 0
            if latest['RSI'] < 30: score += 3   # 超卖反弹
            if latest['J'] < 20: score += 2
            if latest['close'] > prev['close'] and latest['volume'] / latest['VMA5'] > 1.5:
                score += 3
            if latest['close'] < latest['BOLL_DN']: score += 2
            return score
        return 0

    def score_band(self):
        """波段持有评分，侧重趋势和量价健康度"""
        if not self.df.empty and len(self.df) >= 20:
            df = self.df
            latest = df.iloc[-1]
            score = 0
            if df['MA5'].iloc[-1] > df['MA20'].iloc[-1]: score += 2
            if df['DIF'].iloc[-1] > df['DEA'].iloc[-1]: score += 1
            if df['RSI'].iloc[-1] > 50: score += 1
            if latest['close'] > df['MA20'].iloc[-1]: score += 2
            # 量能充沛
            if latest['volume'] > df['VMA5'].iloc[-1] * 1.2: score += 1
            return score
        return 0

# ================== 选股推荐模块 ==================
# 候选池（可按需扩展）
CANDIDATE_POOL = {
    "主板": ["600519", "600030", "002400", "000858", "601318", "600036", "601166", "000002", "600887", "601888"],
    "科创板": ["688981", "688012", "688111", "688036", "688185", "688008", "688009", "688126", "688256", "688005"]
}

def scan_stocks(market, strategy):
    """扫描指定板块，按策略返回前3名"""
    codes = CANDIDATE_POOL.get(market, [])
    results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(analyze_single, code, strategy): code for code in codes}
        for future in as_completed(futures):
            res = future.result()
            if res:
                results.append(res)
    # 排序
    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:3]

def analyze_single(code, strategy):
    name = f"股票{code}"
    analyzer = StockAnalyzer(code, name)
    if not analyzer.fetch_data():
        return None
    if not analyzer.calculate_indicators():
        return None
    latest = analyzer.df.iloc[-1]
    if strategy == "short":
        score = analyzer.score_short_term()
        advice = {
            "介入价": f"{(latest['close'] * 0.98):.2f}",
            "止损价": f"{(latest['low'] * 0.97):.2f}",
            "止盈价": f"{(latest['close'] * 1.05):.2f}",
            "走强确认": "次日涨幅>3%且量比>1.5，早盘竞价量>昨日5%",
            "操作风格": "一日游套利"
        }
    elif strategy == "band":
        score = analyzer.score_band()
        advice = {
            "介入价": f"{(latest['MA20'] * 1.01):.2f}",
            "止损价": f"{(latest['Support'] * 0.95):.2f}",
            "止盈价": f"{(latest['Resistance'] * 1.05):.2f}",
            "走强确认": "站上MA20且MACD零轴金叉，量能持续放大",
            "操作风格": "波段持有"
        }
    else:  # washout
        is_wash, score, desc = analyzer.is_washout_pattern()
        if not is_wash:
            return None
        advice = {
            "介入价": f"{latest['Support']:.2f}",
            "止损价": f"{(latest['Support'] * 0.96):.2f}",
            "止盈价": f"{(latest['Resistance'] * 1.08):.2f}",
            "走强确认": "缩量止跌后放量阳线站上5日线",
            "操作风格": "缩量洗盘低吸"
        }
    return {
        "code": code,
        "name": resolver_name(code),
        "score": score,
        "close": f"{latest['close']:.2f}",
        "advice": advice
    }

def resolver_name(code):
    # 简单名称映射，也可实时查询
    mapping = {
        "600519": "贵州茅台", "600030": "中信证券", "002400": "省广集团",
        "000858": "五粮液", "601318": "中国平安", "600036": "招商银行",
        "601166": "兴业银行", "000002": "万科A", "600887": "伊利股份", "601888": "中国中免",
        "688981": "中芯国际", "688012": "中微公司", "688111": "金山办公",
        "688036": "传音控股", "688185": "康希诺", "688008": "澜起科技",
        "688009": "中国通号", "688126": "沪硅产业", "688256": "寒武纪", "688005": "容百科技"
    }
    return mapping.get(code, f"股票{code}")

# ================== Flask 应用 ==================
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>量价诊断+智能选股 V4.0</title>
<style>
body{background:#0d0d0d;color:#cfcfcf;font-family:Arial;padding:20px}
.terminal{max-width:800px;margin:0 auto;background:#181818;border:1px solid #333;border-radius:10px;padding:20px}
h2{color:#00ff00;text-align:center}
input,button{width:100%;padding:12px;margin:8px 0;border-radius:6px;border:1px solid #444;background:#000;color:#00ff00;font-size:16px}
button{background:#28a745;color:white;font-weight:bold;cursor:pointer}
button:hover{background:#218838}
pre{background:#000;color:#00ff41;padding:15px;border-radius:6px;white-space:pre-wrap;font-size:14px;line-height:1.6;max-height:400px;overflow-y:auto}
.flex{display:flex;gap:10px}
.flex button{flex:1}
</style></head>
<body>
<div class="terminal">
<h2>📈 量价诊断终端 V4.0 (选股+洗盘识别)</h2>
<input type="text" id="stock" placeholder="名称/拼音/代码 (如:省广集团)" value="省广集团">
<input type="number" id="cost" placeholder="持仓成本(可选)">
<button onclick="analyze()">开始诊断</button>
<div class="flex">
  <button onclick="scan('主板','short')">🔍 主板短线推荐</button>
  <button onclick="scan('主板','band')">📊 主板波段推荐</button>
</div>
<div class="flex">
  <button onclick="scan('科创板','short')">🔍 科创板短线推荐</button>
  <button onclick="scan('科创板','band')">📊 科创板波段推荐</button>
</div>
<button onclick="scan('all','washout')">🛁 洗盘模型选股</button>
<pre id="result">等待输入...</pre>
</div>
<script>
async function analyze(){
  const stock=document.getElementById("stock").value;
  const cost=document.getElementById("cost").value || "";
  const res=document.getElementById("result");
  res.textContent="正在分析...";
  try{
    const resp=await fetch("/analyze",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({stock,cost})});
    const data=await resp.json();
    res.textContent=data.report || data.error;
  }catch(e){
    res.textContent="网络异常，请重试。";
  }
}
async function scan(market, strategy){
  const res=document.getElementById("result");
  res.textContent="扫描中，请稍候（可能需要1-2分钟）...";
  try{
    const resp=await fetch("/scan",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({market, strategy})});
    const data=await resp.json();
    if(data.error) res.textContent=data.error;
    else res.textContent=data.result;
  }catch(e){
    res.textContent="扫描超时或网络异常。";
  }
}
</script>
</body>
</html>
'''

app = Flask(__name__)

@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE)

@app.route('/analyze', methods=['POST'])
def analyze():
    data = request.get_json()
    stock_input = data.get('stock', '').strip()
    cost_str = data.get('cost', '').strip()
    if not stock_input:
        return jsonify({"error": "请输入股票名称或代码"})
    cost = float(cost_str) if cost_str else None
    code, name = resolve_stock_input(stock_input)
    if not code:
        return jsonify({"error": f"无法识别 '{stock_input}'"})
    analyzer = StockAnalyzer(code, name, cost)
    if not analyzer.fetch_data():
        return jsonify({"error": "数据获取失败"})
    if not analyzer.calculate_indicators():
        return jsonify({"error": "指标计算失败"})
    report = analyzer.evaluate_strategy()   # 沿用原有evaluate
    return jsonify({"report": report})

@app.route('/scan', methods=['POST'])
def scan():
    data = request.get_json()
    market = data.get('market', '主板')
    strategy = data.get('strategy', 'short')
    if strategy == 'washout':
        # 跨市场扫描洗盘
        all_codes = CANDIDATE_POOL.get('主板', []) + CANDIDATE_POOL.get('科创板', [])
        results = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(analyze_single, code, 'washout'): code for code in all_codes}
            for future in as_completed(futures):
                res = future.result()
                if res:
                    results.append(res)
        results.sort(key=lambda x: x['score'], reverse=True)
        top = results[:6]
        if not top:
            return jsonify({"result": "未找到满足洗盘模型的股票"})
        text = "🛁 缩量洗盘选股结果（前6名）：\n"
        for r in top:
            text += f"\n{r['name']}({r['code']}) 收盘:{r['close']} 评分:{r['score']}\n"
            text += f"  介入: {r['advice']['介入价']}  止损: {r['advice']['止损价']}  止盈: {r['advice']['止盈价']}\n"
            text += f"  确认: {r['advice']['走强确认']}\n"
        return jsonify({"result": text})
    else:
        results = scan_stocks(market, strategy)
        if not results:
            return jsonify({"result": "暂无推荐"})
        text = f"📌 {market} {'短线' if strategy=='short' else '波段'}推荐（前3）:\n"
        for r in results:
            text += f"\n{r['name']}({r['code']}) 收盘:{r['close']} 评分:{r['score']}\n"
            text += f"  风格: {r['advice']['操作风格']}\n"
            text += f"  介入: {r['advice']['介入价']}  止损: {r['advice']['止损价']}  止盈: {r['advice']['止盈价']}\n"
            text += f"  走强确认: {r['advice']['走强确认']}\n"
        return jsonify({"result": text})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
