import pandas as pd
import requests
import numpy as np
import re
import urllib3
from flask import Flask, request, jsonify, render_template_string

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

KDJ_WINDOW = 9
SUPPORT_RESIST_WINDOW = 20
CHIP_DAYS = 60
KLINE_LIMIT = 120

app = Flask(__name__)

# ---------- 复用你的原始函数 ----------
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
    except:
        pass
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
    except:
        pass
    return None, None

class StockAnalyzer:
    def __init__(self, symbol, name, cost_price=None):
        self.symbol = symbol
        self.name = name
        self.cost_price = float(cost_price) if cost_price else None
        self.df = pd.DataFrame()
        self.weekly_df = pd.DataFrame()
        self.chip_peak = 0.0

    def fetch_data(self):
        log = [f"=== 开始获取 {self.name}({self.symbol}) ==="]
        if self._fetch_eastmoney():
            self._fetch_weekly_data()
            log.append("✅ 东方财富日线获取成功")
            return True, log
        if self._fetch_tencent():
            log.append("✅ 腾讯备用源获取成功")
            self._generate_weekly_from_daily()
            return True, log
        log.append("❌ 所有行情源均失败")
        return False, log

    def _fetch_eastmoney(self):
        market_code = "0" if self.symbol.startswith(("0", "3")) else "1"
        secid = f"{market_code}.{self.symbol}"
        url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101", "fqt": "1", "end": "20500101", "lmt": str(KLINE_LIMIT)
        }
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
        except:
            pass
        return False

    def _fetch_tencent(self):
        prefix = "sz" if self.symbol.startswith(("0", "3")) else "sh"
        url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{self.symbol},day,,,{KLINE_LIMIT},qfq"
        try:
            resp = http_session.get(url, timeout=5)
            data = resp.json()
            if data.get("code") == 0:
                stock_data = data["data"].get(f"{prefix}{self.symbol}", {})
                kline = stock_data.get("qfqday") or stock_data.get("day")
                if kline:
                    parsed = [{"date": i[0], "open": float(i[1]), "close": float(i[2]),
                               "high": float(i[3]), "low": float(i[4]), "volume": float(i[5]),
                               "turnover": 0.0} for i in kline]
                    self.df = pd.DataFrame(parsed)
                    self.df['date'] = pd.to_datetime(self.df['date'])
                    return True
        except:
            pass
        return False

    def _fetch_weekly_data(self):
        market_code = "0" if self.symbol.startswith(("0", "3")) else "1"
        secid = f"{market_code}.{self.symbol}"
        url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "102", "fqt": "1", "end": "20500101", "lmt": "100"
        }
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
            except:
                pass
        self._generate_weekly_from_daily()

    def _generate_weekly_from_daily(self):
        if self.df.empty:
            return
        temp = self.df.set_index('date')
        weekly = temp.resample('W').agg({'open': 'first', 'close': 'last', 'high': 'max',
                                         'low': 'min', 'volume': 'sum', 'turnover': 'sum'}).dropna().reset_index()
        self.weekly_df = weekly

    def calculate_indicators(self):
        if self.df.empty or len(self.df) < 20:
            return False
        df = self.df.copy()
        required = ['open', 'close', 'high', 'low', 'volume', 'turnover']
        for col in required:
            if col not in df.columns:
                return False
        df['MA5'] = df['close'].rolling(5).mean()
        df['VMA5'] = df['volume'].rolling(5).mean()
        l9 = df['low'].rolling(KDJ_WINDOW, min_periods=1).min()
        h9 = df['high'].rolling(KDJ_WINDOW, min_periods=1).max()
        denom = h9 - l9
        rsv = np.where(denom == 0, 50, (df['close'] - l9) / denom * 100)
        df['K'] = pd.Series(rsv, index=df.index).ewm(com=2, adjust=False).mean()
        df['D'] = df['K'].ewm(com=2, adjust=False).mean()
        df['J'] = 3 * df['K'] - 2 * df['D']
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
        if len(df) < 10:
            return
        df['MA5'] = df['close'].rolling(5).mean()
        df['MA10'] = df['close'].rolling(10).mean()
        df['VMA5'] = df['volume'].rolling(5).mean()
        l9 = df['low'].rolling(9, min_periods=1).min()
        h9 = df['high'].rolling(9, min_periods=1).max()
        denom = h9 - l9
        rsv = np.where(denom == 0, 50, (df['close'] - l9) / denom * 100)
        df['K'] = pd.Series(rsv, index=df.index).ewm(com=2, adjust=False).mean()
        df['D'] = df['K'].ewm(com=2, adjust=False).mean()
        df['J'] = 3 * df['K'] - 2 * df['D']
        df['Support'] = df['low'].rolling(20).min()
        df['Resistance'] = df['high'].rolling(20).max()
        self.weekly_df = df

    def evaluate_strategy(self):
        if len(self.df) < 20:
            return "❌ 数据不足，无法计算指标。"
        import io, sys
        old_stdout = sys.stdout
        sys.stdout = buf = io.StringIO()
        try:
            latest = self.df.iloc[-1]
            prev = self.df.iloc[-2]
            cp, cv, vma5, jv = latest['close'], latest['volume'], latest['VMA5'], latest['J']
            sup, res, turn = latest['Support'], latest['Resistance'], latest['turnover']
            print("=" * 55)
            print(f" 🤖 【{self.name} ({self.symbol})】 主力量价深度诊断")
            print("=" * 55)
            cost_str = f"  |  🔸 成本: {self.cost_price:.2f}" if self.cost_price else "  |  🔸 持仓: 无"
            print(f"🔸 最新收盘: {cp:.2f}{cost_str}")
            print(f"🔸 支撑: {sup:.2f} | 压力: {res:.2f} | 筹码峰: {self.chip_peak:.2f}")
            print(f"🔸 KDJ(J): {jv:.2f}  |  5日均量: {vma5:.0f}手  |  换手: {turn:.2f}%")
            score, diag = 0, []
            if sup <= cp <= sup * 1.05:
                diag.append("✅ [空间] 靠近支撑，防守性好"); score += 1
            elif cp >= res * 0.95:
                diag.append("⚠️ [空间] 逼近压力，可能回落"); score -= 1
            if self.cost_price and self.cost_price > 0:
                if self.cost_price <= self.chip_peak:
                    diag.append("✅ [筹码] 成本在密集峰下方"); score += 1
                else:
                    diag.append("⚠️ [筹码] 成本高于密集峰"); score -= 1
            else:
                diag.append("ℹ️ [筹码] 未提供持仓成本")
            vol_ratio = cv / vma5 if vma5 > 0 else 1.0
            if cv <= vma5 * 1.1:
                diag.append("✅ [量价] 缩量洗盘"); score += 1
            elif cv > vma5 * 1.5:
                if cp < prev['close']:
                    diag.append("❌ [量价] 放量下跌，警惕派发"); score -= 2
                else:
                    diag.append("🚀 [信号] 带量突破，有望拉升"); score += 1
            if jv < 20:
                diag.append("✅ [KDJ] J值超卖，易反弹"); score += 1
            elif jv > 80:
                diag.append("⚠️ [KDJ] J值超买，追高风险"); score -= 1
            for m in diag: print("  " + m)
            # 周线
            if not self.weekly_df.empty and len(self.weekly_df) >= 10:
                w = self.weekly_df
                wl, wp = w.iloc[-1], w.iloc[-2]
                wc = wl['close']
                wma5, wma10 = wl['MA5'], wl['MA10']
                wvol, wvma5 = wl['volume'], wl['VMA5']
                wk, wd, wj = wl['K'], wl['D'], wl['J']
                wsup, wres = wl.get('Support',0), wl.get('Resistance',0)
                print("\n【周线中期趋势】")
                wt = 0
                if pd.notna(wma5) and pd.notna(wma10):
                    if wma5 > wma10: print("  📊 周线多头排列"); wt = 1
                    elif wma5 < wma10: print("  📊 周线空头排列"); wt = -1
                    else: print("  📊 均线粘合")
                print(f"  📈 KDJ K:{wk:.1f} D:{wd:.1f} J:{wj:.1f}")
                wkdj = 0
                if wj < 20: print("  🟢 周线超卖"); wkdj = 1
                elif wj > 100: print("  🔴 周线超买"); wkdj = -1
                if wp['K'] <= wp['D'] and wk > wd: print("  ✨ 周线金叉"); wkdj += 1
                elif wp['K'] >= wp['D'] and wk < wd: print("  ⚠️ 周线死叉"); wkdj -= 1
                wvr = wvol / wvma5 if wvma5 else 1.0
                print(f"  📊 本周量:{wvol:.0f}  量比:{wvr:.2f}")
                wv = 0
                if wc > wp['close'] and wvr > 1.1: print("  ✅ 放量上涨"); wv = 1
                elif wc < wp['close'] and wvr > 1.2: print("  ❌ 放量下跌"); wv = -2
                elif wc > wp['close'] and wvr < 0.8: print("  ⚠️ 缩量反弹"); wv = -1
                wpb = 0
                if wsup > 0 and wc <= wsup * 1.03: print("  🔹 触及中期支撑"); wpb = 1
                elif wres > 0 and wc >= wres * 0.97: print("  🔹 逼近中期压力"); wpb = -1
                twb = wt + wkdj + wv + wpb
                score += twb
                print(f"  🧩 周线影响: {'+' + str(twb) if twb>=0 else str(twb)}")
            else:
                print("\n【周线中期趋势】\n  ⚠️ 无有效周线数据，仅基于日线判断。")
            print("\n>>> 最终操作建议 <<<")
            if score >= 3:
                print("🟢 积极做多 / 坚定持有")
            elif score >= 1:
                print("🟡 谨慎观望")
            else:
                print("🔴 防守减仓")
            print("=" * 55)
        finally:
            sys.stdout = old_stdout
        return buf.getvalue()

# ---------- Flask 路由 ----------
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>智能量价终端</title>
<style>
body{background:#0d0d0d;color:#cfcfcf;font-family:Arial;padding:20px}
.terminal{max-width:600px;margin:0 auto;background:#181818;border:1px solid #333;border-radius:10px;padding:20px}
h2{color:#00ff00;text-align:center}
input,button{width:100%;padding:12px;margin:8px 0;border-radius:6px;border:1px solid #444;background:#000;color:#00ff00;font-size:16px}
button{background:#28a745;color:white;font-weight:bold;cursor:pointer}
button:hover{background:#218838}
pre{background:#000;color:#00ff41;padding:15px;border-radius:6px;white-space:pre-wrap;font-size:14px;line-height:1.5;max-height:400px;overflow-y:auto}
</style></head>
<body>
<div class="terminal">
<h2>📈 量价诊断终端 V3.2</h2>
<input type="text" id="stock" placeholder="名称/拼音/代码 (如:省广集团, sgjt, 002400)" value="省广集团">
<input type="number" id="cost" placeholder="持仓成本(可选)">
<button onclick="analyze()">开始分析</button>
<pre id="result">等待输入...</pre>
</div>
<script>
async function analyze(){
  const stock=document.getElementById("stock").value;
  const cost=document.getElementById("cost").value || "";
  const res=document.getElementById("result");
  res.textContent="正在分析...";
  try{
    const resp=await fetch("/analyze",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({stock,cost})
    });
    const data=await resp.json();
    if(data.error) res.textContent=data.error;
    else res.textContent=data.report;
  }catch(e){
    res.textContent="网络异常，请重试。";
  }
}
</script>
</body>
</html>
'''

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