import pandas as pd
import numpy as np
import re
import urllib3
import requests
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

# ================== 分析引擎 ==================
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
        log = [f"=== 开始获取 {self.name}({self.symbol}) ==="]
        if self._fetch_eastmoney():
            self._fetch_weekly_data()
            log.append("✅ 东方财富日线成功")
            return True, log
        if self._fetch_tencent():
            log.append("✅ 腾讯备用成功")
            self._generate_weekly_from_daily()
            return True, log
        log.append("❌ 所有行情源均失败")
        return False, log

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

    # ---------- 计算所有指标 ----------
    def calculate_indicators(self):
        if self.df.empty or len(self.df) < 20: return False
        df = self.df.copy()
        required = ['open','close','high','low','volume','turnover']
        for col in required:
            if col not in df.columns: return False

        # 均线
        df['MA5'] = df['close'].rolling(5).mean()
        df['MA10'] = df['close'].rolling(10).mean()
        df['MA20'] = df['close'].rolling(20).mean()
        df['VMA5'] = df['volume'].rolling(5).mean()

        # KDJ
        l9 = df['low'].rolling(KDJ_WINDOW, min_periods=1).min()
        h9 = df['high'].rolling(KDJ_WINDOW, min_periods=1).max()
        denom = h9 - l9
        rsv = np.where(denom == 0, 50, (df['close'] - l9) / denom * 100)
        df['K'] = pd.Series(rsv, index=df.index).ewm(com=2, adjust=False).mean()
        df['D'] = df['K'].ewm(com=2, adjust=False).mean()
        df['J'] = 3*df['K'] - 2*df['D']

        # MACD
        ema12 = df['close'].ewm(span=MACD_FAST, adjust=False).mean()
        ema26 = df['close'].ewm(span=MACD_SLOW, adjust=False).mean()
        df['DIF'] = ema12 - ema26
        df['DEA'] = df['DIF'].ewm(span=MACD_SIGNAL, adjust=False).mean()
        df['MACD'] = 2*(df['DIF'] - df['DEA'])

        # RSI
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.ewm(alpha=1/RSI_PERIOD, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/RSI_PERIOD, adjust=False).mean()
        rs = avg_gain / avg_loss
        df['RSI'] = 100 - (100 / (1 + rs))

        # BOLL
        df['BOLL_MID'] = df['close'].rolling(BOLL_PERIOD).mean()
        std = df['close'].rolling(BOLL_PERIOD).std()
        df['BOLL_UP'] = df['BOLL_MID'] + BOLL_WIDTH*std
        df['BOLL_DN'] = df['BOLL_MID'] - BOLL_WIDTH*std

        # 支撑/压力
        df['Support'] = df['low'].rolling(SUPPORT_RESIST_WINDOW).min()
        df['Resistance'] = df['high'].rolling(SUPPORT_RESIST_WINDOW).max()

        # 筹码峰
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

    # ---------- 综合诊断（增强版） ----------
    def evaluate_strategy(self):
        if len(self.df) < 20:
            return "❌ 数据不足"
        import io, sys
        old_stdout = sys.stdout
        sys.stdout = buf = io.StringIO()
        try:
            df = self.df
            latest = df.iloc[-1]
            prev = df.iloc[-2]

            print("=" * 55)
            print(f" 🤖 【{self.name} ({self.symbol})】 综合量价诊断 (V3.5)")
            print("=" * 55)

            # --- 基础价格 ---
            cost_str = f"持仓成本: {self.cost_price:.2f}" if self.cost_price else "无持仓"
            print(f"💰 收盘: {latest['close']:.2f}  |  {cost_str}  |  换手: {latest['turnover']:.2f}%")

            # ========== 评分系统 ==========
            score = 0
            signals = []

            # ===== 1. 均线系统 =====
            ma5 = latest['MA5']; ma10 = latest['MA10']; ma20 = latest['MA20']
            print(f"\n📊 均线: MA5={ma5:.2f} MA10={ma10:.2f} MA20={ma20:.2f}")
            if pd.notna(ma5) and pd.notna(ma20):
                if ma5 > ma10 > ma20:
                    print("   ✅ 多头排列，趋势向上")
                    signals.append("均线多头排列"); score += 2
                elif ma5 < ma10 < ma20:
                    print("   ❌ 空头排列，趋势向下")
                    signals.append("均线空头排列"); score -= 2
                else:
                    print("   ↔️ 均线交织，震荡格局")
            # 金叉死叉
            if prev['MA10'] <= prev['MA20'] and latest['MA10'] > latest['MA20']:
                print("   ✨ MA10上穿MA20，中期走强")
                signals.append("MA10金叉MA20"); score += 1
            elif prev['MA10'] >= prev['MA20'] and latest['MA10'] < latest['MA20']:
                print("   ⚠️ MA10下穿MA20，中期走弱")
                signals.append("MA10死叉MA20"); score -= 1

            # ===== 2. KDJ =====
            k, d, j = latest['K'], latest['D'], latest['J']
            print(f"\n📈 KDJ: K={k:.1f} D={d:.1f} J={j:.1f}")
            if j < 20:
                print("   ✅ J值超卖，反弹需求")
                signals.append("KDJ超卖"); score += 2
            elif j > 100:
                print("   ⚠️ J值超买，警惕回调")
                signals.append("KDJ超买"); score -= 2
            if prev['K'] <= prev['D'] and k > d:
                print("   ✨ KDJ金叉")
                signals.append("KDJ金叉"); score += 1
            elif prev['K'] >= prev['D'] and k < d:
                print("   ⚠️ KDJ死叉")
                signals.append("KDJ死叉"); score -= 1

            # ===== 3. MACD =====
            dif, dea, macd = latest['DIF'], latest['DEA'], latest['MACD']
            print(f"\n🔮 MACD: DIF={dif:.3f} DEA={dea:.3f} MACD柱={macd:.3f}")
            if dif > dea:
                if macd > 0:
                    print("   ✅ 零轴上方多头运行")
                    signals.append("MACD多头"); score += 1
                else:
                    print("   ↔️ 柱线收缩，动能减弱")
            else:
                if macd < 0:
                    print("   ❌ 零轴下方空头运行")
                    signals.append("MACD空头"); score -= 1
                else:
                    print("   ↔️ 柱线缩短，下跌放缓")
            # 金叉死叉
            if prev['DIF'] <= prev['DEA'] and dif > dea:
                print("   ✨ MACD金叉")
                signals.append("MACD金叉"); score += 2
            elif prev['DIF'] >= prev['DEA'] and dif < dea:
                print("   ⚠️ MACD死叉")
                signals.append("MACD死叉"); score -= 2
            # 背离检测（简单：价格新高但MACD柱未新高 -> 顶背离）
            recent_high = df['close'].tail(20).idxmax()
            if recent_high == df.index[-1] and latest['close'] > df['close'].iloc[-2]:
                if latest['MACD'] < df.loc[recent_high-1, 'MACD']:
                    print("   ⚠️ 顶背离风险（价格新高，MACD柱未配合）")
                    signals.append("MACD顶背离"); score -= 3

            # ===== 4. RSI =====
            rsi = latest['RSI']
            print(f"\n📉 RSI(14): {rsi:.1f}")
            if rsi < 30:
                print("   ✅ 超卖区间，可能反弹")
                signals.append("RSI超卖"); score += 2
            elif rsi > 70:
                print("   ⚠️ 超买区间，可能回调")
                signals.append("RSI超买"); score -= 2
            elif rsi > 50:
                print("   ↔️ 偏强，多头优势")
            else:
                print("   ↔️ 偏弱，空头优势")

            # ===== 5. BOLL =====
            up, mid, dn = latest['BOLL_UP'], latest['BOLL_MID'], latest['BOLL_DN']
            band_width = (up - dn) / mid * 100
            close = latest['close']
            print(f"\n📐 BOLL: 上轨={up:.2f} 中轨={mid:.2f} 下轨={dn:.2f} 带宽={band_width:.1f}%")
            if close > up:
                print("   🚀 股价突破上轨，超强走势")
                signals.append("突破上轨"); score += 2
            elif close < dn:
                print("   🔻 股价跌破下轨，加速赶底")
                signals.append("跌破下轨"); score -= 1
            else:
                print("   ↔️ 股价在布林通道内运行")
            if band_width < 5:
                print("   ⚠️ 布林带极度收窄，即将变盘")
                signals.append("布林收窄（变盘）")

            # ===== 6. 量价关系 =====
            vol = latest['volume']; vma5 = latest['VMA5']
            vol_ratio = vol / vma5 if vma5 else 1
            print(f"\n🌊 量能: 今日{vol:.0f}手  5日均{vma5:.0f}手  量比{vol_ratio:.2f}")
            if vol_ratio < 0.5:
                print("   💤 地量水平，变盘临近")
                signals.append("地量"); score += 1
            elif vol_ratio > 2.5:
                print("   🔥 天量换手，关注方向")
                signals.append("天量"); score -= 1
            # 价量配合
            if close > prev['close'] and vol_ratio > 1.5:
                print("   ✅ 放量上涨，健康")
                signals.append("放量上涨"); score += 2
            elif close < prev['close'] and vol_ratio > 1.5:
                print("   ❌ 放量下跌，危险")
                signals.append("放量下跌"); score -= 3
            elif close > prev['close'] and vol_ratio < 0.8:
                print("   ⚠️ 缩量反弹，力度弱")
                signals.append("缩量反弹"); score -= 1
            elif close < prev['close'] and vol_ratio < 0.8:
                print("   ✅ 缩量回调，洗盘概率大")
                signals.append("缩量下跌（洗盘）"); score += 2

            # ===== 7. 支撑/压力与筹码 =====
            sup = latest['Support']; res = latest['Resistance']
            print(f"\n🏔️ 支撑: {sup:.2f}  压力: {res:.2f}  筹码峰: {self.chip_peak:.2f}")
            if sup <= close <= sup*1.03:
                print("   ✅ 靠近支撑，防守位明确")
                signals.append("接近支撑"); score += 1
            elif close >= res*0.97:
                print("   ⚠️ 逼近压力位，突破需放量")
                signals.append("接近压力"); score -= 1
            if self.cost_price and self.cost_price > 0:
                if self.cost_price <= self.chip_peak:
                    print("   ✅ 成本低于筹码峰，安全")
                    signals.append("成本优势"); score += 1
                else:
                    print("   ⚠️ 成本高于筹码峰，易洗盘")
                    signals.append("成本劣势"); score -= 1

            # ===== 8. 周线趋势（简化） =====
            if not self.weekly_df.empty and len(self.weekly_df) >= 10:
                wdf = self.weekly_df
                wl = wdf.iloc[-1]
                wp = wdf.iloc[-2]
                print(f"\n📅 周线: 收盘{wl['close']:.2f}  MA5={wl['MA5']:.2f}  MA10={wl['MA10']:.2f}")
                if wl['MA5'] > wl['MA10']:
                    print("   ✅ 周线多头，中期向好")
                    score += 1
                elif wl['MA5'] < wl['MA10']:
                    print("   ⚠️ 周线空头，中期谨慎")
                    score -= 1
                wj = wl['J']
                if wj < 20:
                    print("   🟢 周线J值超卖"); score += 1
                elif wj > 100:
                    print("   🔴 周线J值超买"); score -= 1
                if wp['K'] <= wp['D'] and wl['K'] > wl['D']:
                    print("   ✨ 周线KDJ金叉"); score += 1
                elif wp['K'] >= wp['D'] and wl['K'] < wl['D']:
                    print("   ⚠️ 周线KDJ死叉"); score -= 1

            # ========== 综合结论 ==========
            print("\n" + "="*55)
            print(f"📌 综合评分: {score}")
            if score >= 6:
                print("🟢 强烈看多！多项指标共振向上，可积极做多或持有。")
            elif score >= 3:
                print("🟡 偏多，但存在瑕疵，持有为主，新仓需等待回踩。")
            elif score >= 0:
                print("🟠 中性，信号矛盾，观望或轻仓，等待方向选择。")
            elif score >= -3:
                print("🟣 偏空，多数指标走弱，应减仓或设置止损。")
            else:
                print("🔴 强烈看空！主力出货迹象明显，建议空仓或严格止损。")
            print("="*55)
        finally:
            sys.stdout = old_stdout
        return buf.getvalue()

# ================== Flask 应用 ==================
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>智能量价终端 V3.5</title>
<style>
body{background:#0d0d0d;color:#cfcfcf;font-family:Arial;padding:20px}
.terminal{max-width:650px;margin:0 auto;background:#181818;border:1px solid #333;border-radius:10px;padding:20px}
h2{color:#00ff00;text-align:center}
input,button{width:100%;padding:12px;margin:8px 0;border-radius:6px;border:1px solid #444;background:#000;color:#00ff00;font-size:16px}
button{background:#28a745;color:white;font-weight:bold;cursor:pointer}
button:hover{background:#218838}
pre{background:#000;color:#00ff41;padding:15px;border-radius:6px;white-space:pre-wrap;font-size:14px;line-height:1.6;max-height:500px;overflow-y:auto}
</style></head>
<body>
<div class="terminal">
<h2>📈 量价诊断终端 V3.5 (多指标综合)</h2>
<input type="text" id="stock" placeholder="名称/拼音/代码 (如:省广集团, sgjt, 002400)" value="省广集团">
<input type="number" id="cost" placeholder="持仓成本(可选)">
<button onclick="analyze()">开始诊断</button>
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
        return jsonify({"error": f"无法识别 '{stock_input}'，请检查拼写或使用6位代码"})
    analyzer = StockAnalyzer(code, name, cost)
    success, log = analyzer.fetch_data()
    if not success:
        return jsonify({"error": "\n".join(log)})
    if not analyzer.calculate_indicators():
        return jsonify({"error": "指标计算失败"})
    report = analyzer.evaluate_strategy()
    return jsonify({"report": report})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)