import pandas as pd
import numpy as np
import re
import urllib3
import requests
import threading
import time
import io
import base64
import datetime
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

active_cache = {"data": None, "time": 0}

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

# ================== 动态获取活跃A股（含板块） ==================
def get_active_stocks(limit=200):
    global active_cache
    now = time.time()
    if active_cache["data"] is not None and (now - active_cache["time"]) < 1800:
        return active_cache["data"][:limit]
    try:
        url = "http://80.push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1",
            "pz": str(min(limit + 50, 500)),
            "po": "1",
            "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2",
            "invt": "2",
            "fid": "f20",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f14,f2,f3,f10,f20,f21,f15,f100",
            "_": str(int(time.time() * 1000))
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
                sector = item.get("f100", "")
                stocks.append({
                    "code": code,
                    "name": name,
                    "volume_ratio": f20,
                    "turnover": f10,
                    "sector": sector
                })
            stocks.sort(key=lambda x: x["volume_ratio"], reverse=True)
            active_cache["data"] = stocks
            active_cache["time"] = now
            return stocks[:limit]
    except Exception as e:
        print(f"获取活跃股票失败: {e}")
    return [
        {"code": "600519", "name": "贵州茅台", "sector": "酿酒行业"},
        {"code": "000858", "name": "五粮液", "sector": "酿酒行业"},
        {"code": "601318", "name": "中国平安", "sector": "保险"},
        {"code": "002400", "name": "省广集团", "sector": "文化传媒"},
        {"code": "688981", "name": "中芯国际", "sector": "半导体"},
        {"code": "300750", "name": "宁德时代", "sector": "电池"}
    ]

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

    def fetch_data(self, end_date=None):
        if self._fetch_eastmoney(end_date):
            self._fetch_weekly_data(end_date)
            return True
        if self._fetch_tencent():
            self._generate_weekly_from_daily()
            return True
        return False

    def _fetch_eastmoney(self, end_date=None):
        mkt = "0" if self.symbol.startswith(("0", "3")) else "1"
        url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {"secid": f"{mkt}.{self.symbol}", "fields1": "f1,f2,f3,f4,f5,f6",
                  "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                  "klt": "101", "fqt": "1", "end": end_date if end_date else "20500101",
                  "lmt": str(KLINE_LIMIT)}
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

    def _fetch_weekly_data(self, end_date=None):
        mkt = "0" if self.symbol.startswith(("0", "3")) else "1"
        url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {"secid": f"{mkt}.{self.symbol}", "fields1": "f1,f2,f3,f4,f5,f6",
                  "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                  "klt": "102", "fqt": "1", "end": end_date if end_date else "20500101",
                  "lmt": "100"}
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

    def evaluate_strategy(self):
        if len(self.df) < 20: return "❌ 数据不足"
        import io, sys
        old_stdout = sys.stdout
        sys.stdout = buf = io.StringIO()
        try:
            df = self.df
            latest = df.iloc[-1]
            prev = df.iloc[-2]
            print("=" * 55)
            print(f" 🤖 【{self.name} ({self.symbol})】 综合量价诊断 (V4.2)")
            print("=" * 55)
            cost_str = f"持仓成本: {self.cost_price:.2f}" if self.cost_price else "无持仓"
            print(f"💰 收盘: {latest['close']:.2f}  |  {cost_str}  |  换手: {latest['turnover']:.2f}%")

            score = 0
            signals = []

            ma5 = latest['MA5']; ma10 = latest['MA10']; ma20 = latest['MA20']
            print(f"\n📊 均线: MA5={ma5:.2f} MA10={ma10:.2f} MA20={ma20:.2f}")
            if pd.notna(ma5) and pd.notna(ma20):
                if ma5 > ma10 > ma20:
                    print("   ✅ 多头排列"); signals.append("均线多头排列"); score += 2
                elif ma5 < ma10 < ma20:
                    print("   ❌ 空头排列"); signals.append("均线空头排列"); score -= 2
                else:
                    print("   ↔️ 震荡交织")
            if prev['MA10'] <= prev['MA20'] and latest['MA10'] > latest['MA20']:
                print("   ✨ MA10金叉MA20"); signals.append("MA10金叉MA20"); score += 1
            elif prev['MA10'] >= prev['MA20'] and latest['MA10'] < latest['MA20']:
                print("   ⚠️ MA10死叉MA20"); signals.append("MA10死叉MA20"); score -= 1

            k, d, j = latest['K'], latest['D'], latest['J']
            print(f"\n📈 KDJ: K={k:.1f} D={d:.1f} J={j:.1f}")
            if j < 20: print("   ✅ J值超卖"); signals.append("KDJ超卖"); score += 2
            elif j > 100: print("   ⚠️ J值超买"); signals.append("KDJ超买"); score -= 2
            if prev['K'] <= prev['D'] and k > d: print("   ✨ KDJ金叉"); signals.append("KDJ金叉"); score += 1
            elif prev['K'] >= prev['D'] and k < d: print("   ⚠️ KDJ死叉"); signals.append("KDJ死叉"); score -= 1

            dif, dea, macd = latest['DIF'], latest['DEA'], latest['MACD']
            print(f"\n🔮 MACD: DIF={dif:.3f} DEA={dea:.3f} MACD柱={macd:.3f}")
            if dif > dea:
                if macd > 0: print("   ✅ 零轴上方多头"); signals.append("MACD多头"); score += 1
                else: print("   ↔️ 柱线收缩")
            else:
                if macd < 0: print("   ❌ 零轴下方空头"); signals.append("MACD空头"); score -= 1
                else: print("   ↔️ 柱线缩短")
            if prev['DIF'] <= prev['DEA'] and dif > dea: print("   ✨ MACD金叉"); signals.append("MACD金叉"); score += 2
            elif prev['DIF'] >= prev['DEA'] and dif < dea: print("   ⚠️ MACD死叉"); signals.append("MACD死叉"); score -= 2

            rsi = latest['RSI']
            print(f"\n📉 RSI(14): {rsi:.1f}")
            if rsi < 30: print("   ✅ 超卖区间"); signals.append("RSI超卖"); score += 2
            elif rsi > 70: print("   ⚠️ 超买区间"); signals.append("RSI超买"); score -= 2

            up, mid, dn = latest['BOLL_UP'], latest['BOLL_MID'], latest['BOLL_DN']
            band_width = (up - dn) / mid * 100
            close = latest['close']
            print(f"\n📐 BOLL: 上轨={up:.2f} 中轨={mid:.2f} 下轨={dn:.2f} 带宽={band_width:.1f}%")
            if close > up: print("   🚀 突破上轨"); signals.append("突破上轨"); score += 2
            elif close < dn: print("   🔻 跌破下轨"); signals.append("跌破下轨"); score -= 1
            if band_width < 5: print("   ⚠️ 布林收窄，变盘临近"); signals.append("布林收窄")

            vol = latest['volume']; vma5 = latest['VMA5']
            vol_ratio = vol / vma5 if vma5 else 1
            print(f"\n🌊 量能: 今日{vol:.0f}手  5日均{vma5:.0f}手  量比{vol_ratio:.2f}")
            if vol_ratio < 0.5: print("   💤 地量"); signals.append("地量"); score += 1
            elif vol_ratio > 2.5: print("   🔥 天量"); signals.append("天量"); score -= 1
            if close > prev['close'] and vol_ratio > 1.5:
                print("   ✅ 放量上涨"); signals.append("放量上涨"); score += 2
            elif close < prev['close'] and vol_ratio > 1.5:
                print("   ❌ 放量下跌"); signals.append("放量下跌"); score -= 3
            elif close > prev['close'] and vol_ratio < 0.8:
                print("   ⚠️ 缩量反弹"); signals.append("缩量反弹"); score -= 1
            elif close < prev['close'] and vol_ratio < 0.8:
                print("   ✅ 缩量回调，洗盘概率大"); signals.append("缩量下跌（洗盘）"); score += 2

            sup = latest['Support']; res = latest['Resistance']
            print(f"\n🏔️ 支撑: {sup:.2f}  压力: {res:.2f}  筹码峰: {self.chip_peak:.2f}")
            if sup <= close <= sup*1.03: print("   ✅ 靠近支撑"); signals.append("接近支撑"); score += 1
            elif close >= res*0.97: print("   ⚠️ 逼近压力"); signals.append("接近压力"); score -= 1
            if self.cost_price and self.cost_price > 0:
                if self.cost_price <= self.chip_peak: print("   ✅ 成本低于筹码峰"); signals.append("成本优势"); score += 1
                else: print("   ⚠️ 成本高于筹码峰"); signals.append("成本劣势"); score -= 1

            if not self.weekly_df.empty and len(self.weekly_df) >= 10:
                wdf = self.weekly_df; wl = wdf.iloc[-1]; wp = wdf.iloc[-2]
                print(f"\n📅 周线: 收盘{wl['close']:.2f}  MA5={wl['MA5']:.2f}  MA10={wl['MA10']:.2f}")
                if wl['MA5'] > wl['MA10']: print("   ✅ 周线多头"); score += 1
                elif wl['MA5'] < wl['MA10']: print("   ⚠️ 周线空头"); score -= 1
                wj = wl['J']
                if wj < 20: print("   🟢 周线J值超卖"); score += 1
                elif wj > 100: print("   🔴 周线J值超买"); score -= 1
                if wp['K'] <= wp['D'] and wl['K'] > wl['D']: print("   ✨ 周线KDJ金叉"); score += 1
                elif wp['K'] >= wp['D'] and wl['K'] < wl['D']: print("   ⚠️ 周线KDJ死叉"); score -= 1

            print("\n" + "="*55)
            print(f"📌 综合评分: {score}")
            if score >= 6: print("🟢 强烈看多！")
            elif score >= 3: print("🟡 偏多，谨慎乐观")
            elif score >= 0: print("🟠 中性，观望")
            elif score >= -3: print("🟣 偏空，注意风险")
            else: print("🔴 强烈看空！")
            print("="*55)
        finally:
            sys.stdout = old_stdout
        return buf.getvalue()

    def score_short_term(self):
        if not self.df.empty and len(self.df) >= 20:
            latest = self.df.iloc[-1]
            prev = self.df.iloc[-2]
            score = 0
            if latest['RSI'] < 30: score += 3
            if latest['J'] < 20: score += 2
            if latest['close'] > prev['close'] and latest['volume'] / latest['VMA5'] > 1.5: score += 3
            if latest['close'] < latest['BOLL_DN']: score += 2
            return score
        return 0

    def score_band(self):
        if not self.df.empty and len(self.df) >= 20:
            df = self.df
            latest = df.iloc[-1]
            score = 0
            if df['MA5'].iloc[-1] > df['MA20'].iloc[-1]: score += 2
            if df['DIF'].iloc[-1] > df['DEA'].iloc[-1]: score += 1
            if df['RSI'].iloc[-1] > 50: score += 1
            if latest['close'] > df['MA20'].iloc[-1]: score += 2
            if latest['volume'] > df['VMA5'].iloc[-1] * 1.2: score += 1
            return score
        return 0

    def is_washout_pattern(self):
        if len(self.df) < 40: return False, 0, "数据不足"
        df = self.df.tail(40)
        price_start = df['close'].iloc[0]
        price_peak = df['high'].iloc[10:30].max() if len(df) > 30 else df['high'].max()
        if price_peak / price_start < 1.12: return False, 0, "无前期拉升"
        recent10 = df.tail(10)
        high_10, low_10 = recent10['high'].max(), recent10['low'].min()
        if (high_10 - low_10) / low_10 > 0.10: return False, 0, "横盘幅度过大"
        ma20 = df['MA20'].tail(10)
        if (recent10['close'].values < ma20.values * 0.98).any(): return False, 0, "未守住MA20"
        vol_rise = df['volume'].iloc[10:30].mean()
        vol_now = df['volume'].tail(5).mean()
        if vol_now / vol_rise > 0.7: return False, 0, "缩量不充分"
        last_3 = df.tail(3)
        if last_3['close'].iloc[-1] < last_3['close'].iloc[0] and last_3['volume'].mean() < df['VMA5'].tail(3).mean() * 0.8:
            score = 80
            desc = "拉升后横盘缩量洗盘，当前缩量下探支撑"
        else:
            score = 60
            desc = "横盘缩量中，等待进一步确认"
        return True, score, desc

# ================== 选股相关 (优化竞价量并输出5-10只) ==================
def analyze_single(code, strategy):
    name = f"股票{code}"
    analyzer = StockAnalyzer(code, name)
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(analyzer.fetch_data)
            if not future.result(timeout=5): return None
            future = ex.submit(analyzer.calculate_indicators)
            if not future.result(timeout=3): return None
    except:
        return None

    latest = analyzer.df.iloc[-1]
    prev_close = latest['close']
    prev_vol = latest['volume'] if strategy != 'short' else analyzer.df.iloc[-2]['volume']
    # 计算竞价量阈值：昨日成交量的5%
    bid_vol_threshold = prev_vol * 0.05
    bid_amount_threshold = bid_vol_threshold * prev_close  # 估算金额（手*价格）

    if strategy == "short":
        score = analyzer.score_short_term()
        advice = {
            "介入价": f"{(latest['close'] * 0.98):.2f}",
            "止损价": f"{(latest['low'] * 0.97):.2f}",
            "止盈价": f"{(latest['close'] * 1.05):.2f}",
            "走强确认": f"次日涨幅>3%且量比>1.5，早盘竞价量>{bid_vol_threshold:.0f}手（约{bid_amount_threshold/10000:.0f}万元）",
            "操作风格": "一日游套利"
        }
    elif strategy == "band":
        score = analyzer.score_band()
        advice = {
            "介入价": f"{(latest['MA20'] * 1.01):.2f}",
            "止损价": f"{(latest['Support'] * 0.95):.2f}",
            "止盈价": f"{(latest['Resistance'] * 1.05):.2f}",
            "走强确认": f"站上MA20且MACD零轴金叉，量能持续放大，竞价量>{bid_vol_threshold:.0f}手（约{bid_amount_threshold/10000:.0f}万元）",
            "操作风格": "波段持有"
        }
    else:  # washout
        is_wash, score, desc = analyzer.is_washout_pattern()
        if not is_wash: return None
        advice = {
            "介入价": f"{latest['Support']:.2f}",
            "止损价": f"{(latest['Support'] * 0.96):.2f}",
            "止盈价": f"{(latest['Resistance'] * 1.08):.2f}",
            "走强确认": f"缩量止跌后放量阳线站上5日线，次日竞价量>{bid_vol_threshold:.0f}手（约{bid_amount_threshold/10000:.0f}万元）",
            "操作风格": "缩量洗盘低吸"
        }
    return {
        "code": code,
        "name": analyzer.name,
        "score": score,
        "close": f"{latest['close']:.2f}",
        "sector": "",
        "advice": advice
    }

def scan_stocks(market, strategy, limit=200, top_n=10):
    active_list = get_active_stocks(limit)
    code_to_sector = {item["code"]: item.get("sector", "") for item in active_list}
    codes = [item["code"] for item in active_list]
    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(analyze_single, code, strategy): code for code in codes}
        for future in as_completed(futures):
            try:
                res = future.result(timeout=10)
                if res:
                    res["sector"] = code_to_sector.get(res["code"], "")
                    results.append(res)
            except: pass
    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:top_n]

# ================== 回测模块 ==================
# 为回测准备固定股票池（沪深300部分成分股，确保有足够历史数据）
BACKTEST_POOL = [
    "600519", "000858", "601318", "000002", "600036", "601166", "600887", "601888",
    "002400", "300750", "688981", "688012", "688111", "002415", "000651", "600030",
    "000725", "601012", "601688", "600276", "300059", "002475", "601211", "600570"
]

def run_backtest(strategy, test_date_str):
    """针对指定日期运行选股，返回选股列表和后续5日表现"""
    end_date = test_date_str.replace("-", "")
    test_date = pd.to_datetime(test_date_str).date()
    results = []
    valid_count = 0
    for code in BACKTEST_POOL:
        analyzer = StockAnalyzer(code, f"BT{code}")
        # 获取截至测试日的数据
        if not analyzer.fetch_data(end_date=end_date):
            continue
        if not analyzer.calculate_indicators():
            continue
        # 根据策略评分或判断
        if strategy == "short":
            score = analyzer.score_short_term()
            if score < 4: continue   # 阈值可调
        elif strategy == "band":
            score = analyzer.score_band()
            if score < 3: continue
        elif strategy == "washout":
            is_wash, score, desc = analyzer.is_washout_pattern()
            if not is_wash: continue
        else:
            continue
        # 获取未来5日收益（需要实际数据，这里我们通过再次获取完整数据来模拟）
        full_analyzer = StockAnalyzer(code, f"FULL{code}")
        if not full_analyzer.fetch_data():   # 获取最近120天数据
            continue
        full_df = full_analyzer.df
        # 找到测试日之后第5个交易日
        future_dates = full_df[full_df['date'] > pd.Timestamp(test_date)].head(5)
        if len(future_dates) < 3:
            continue
        future_close = future_dates.iloc[-1]['close']
        test_close = full_df[full_df['date'] <= pd.Timestamp(test_date)].iloc[-1]['close']
        ret = (future_close - test_close) / test_close * 100
        valid_count += 1
        results.append({
            "code": code,
            "name": analyzer.name,
            "score": score,
            "profit": ret,
            "success": ret > 3  # 大于3%视为成功
        })
    if valid_count == 0:
        return None
    success_count = sum(1 for r in results if r['success'])
    accuracy = success_count / len(results) * 100
    return {
        "date": test_date_str,
        "strategy": strategy,
        "total": len(results),
        "success": success_count,
        "accuracy": accuracy,
        "picks": results
    }

# ================== Flask 应用 ==================
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>量价诊断终端 V4.3 (回测版)</title>
<style>
body{background:#0d0d0d;color:#cfcfcf;font-family:Arial;padding:20px}
.terminal{max-width:900px;margin:0 auto;background:#181818;border:1px solid #333;border-radius:10px;padding:20px}
h2{color:#00ff00;text-align:center}
input,button{width:100%;padding:12px;margin:8px 0;border-radius:6px;border:1px solid #444;background:#000;color:#00ff00;font-size:16px}
button{background:#28a745;color:white;font-weight:bold;cursor:pointer}
button:hover{background:#218838}
pre{background:#000;color:#00ff41;padding:15px;border-radius:6px;white-space:pre-wrap;font-size:14px;line-height:1.6;max-height:400px;overflow-y:auto}
.flex{display:flex;gap:10px}
.flex button{flex:1}
.holding-row{display:flex;gap:8px;margin:5px 0}
.holding-row input{flex:1}
img{max-width:100%;margin-top:10px}
</style></head>
<body>
<div class="terminal">
<h2>📈 量价诊断终端 V4.3 (全A股+回测)</h2>
<input type="text" id="stock" placeholder="名称/拼音/代码" value="省广集团">
<input type="number" id="cost" placeholder="持仓成本(可选)">
<button onclick="analyze()">开始诊断</button>
<button onclick="showChart()">📊 查看日线/周线图表</button>
<div class="flex">
  <button onclick="scanAll('short')">🔍 短线推荐 (10只)</button>
  <button onclick="scanAll('band')">📊 波段推荐 (10只)</button>
  <button onclick="scanAll('washout')">🛁 洗盘选股 (10只)</button>
</div>
<h3 style="color:#ffaa00;">📈 选股准确率回测</h3>
<select id="bt_strategy"><option value="short">短线</option><option value="band">波段</option><option value="washout">洗盘</option></select>
<button onclick="runBacktest()">运行回测 (近30日)</button>
<button onclick="showBacktestStats()">显示回测统计</button>
<h3 style="color:#ffaa00;">💰 组合仓位分析</h3>
<div id="holdings">
  <div class="holding-row">
    <input type="text" class="hcode" placeholder="股票代码">
    <input type="number" class="hcost" placeholder="成本">
    <input type="number" class="hweight" placeholder="仓位%">
    <button onclick="addRow()">+</button>
  </div>
</div>
<button onclick="analyzePortfolio()">📉 分析我的持仓</button>
<pre id="result">等待输入...</pre>
<img id="chartImg" style="display:none;">
</div>
<script>
async function analyze(){
  const stock=document.getElementById("stock").value;
  const cost=document.getElementById("cost").value || "";
  const res=document.getElementById("result");
  res.textContent="正在分析...";
  const resp=await fetch("/analyze",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({stock,cost})});
  const data=await resp.json();
  res.textContent=data.report || data.error;
}
async function showChart(){
  const stock=document.getElementById("stock").value;
  if(!stock) return alert("请输入股票代码");
  const img=document.getElementById("chartImg");
  img.style.display="block";
  img.src="/chart?code="+encodeURIComponent(stock);
}
async function scanAll(strategy){
  const res=document.getElementById("result");
  res.textContent="扫描全市场活跃股，预计1-2分钟...";
  const resp=await fetch("/scan",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({strategy: strategy, top_n: 10})});
  const data=await resp.json();
  res.textContent=data.result || data.error;
}
function addRow(){
  const container=document.getElementById("holdings");
  const row=document.createElement("div");
  row.className="holding-row";
  row.innerHTML=`
    <input type="text" class="hcode" placeholder="股票代码">
    <input type="number" class="hcost" placeholder="成本">
    <input type="number" class="hweight" placeholder="仓位%">
    <button onclick="this.parentElement.remove()">-</button>
  `;
  container.appendChild(row);
}
async function analyzePortfolio(){
  const rows=document.querySelectorAll(".holding-row");
  const holdings=[];
  rows.forEach(row=>{
    const code=row.querySelector(".hcode").value.trim();
    const cost=row.querySelector(".hcost").value;
    const weight=row.querySelector(".hweight").value||"0";
    if(code) holdings.push({code,cost:parseFloat(cost)||0,weight:parseFloat(weight)||0});
  });
  if(holdings.length===0) return alert("请至少输入一只股票！");
  const res=document.getElementById("result");
  res.textContent="正在分析持仓组合...";
  const resp=await fetch("/portfolio",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({holdings})});
  const data=await resp.json();
  res.textContent=data.report||data.error;
}
async function runBacktest(){
  const strategy=document.getElementById("bt_strategy").value;
  const res=document.getElementById("result");
  res.textContent="回测计算中，大约需要2-3分钟...";
  const resp=await fetch("/backtest",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({strategy})});
  const data=await resp.json();
  res.textContent=data.result || data.error;
}
async function showBacktestStats(){
  const strategy=document.getElementById("bt_strategy").value;
  const res=document.getElementById("result");
  const resp=await fetch("/backtest_stats",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({strategy})});
  const data=await resp.json();
  res.textContent=data.result || data.error;
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
    report = analyzer.evaluate_strategy()
    return jsonify({"report": report})

@app.route('/chart')
def chart():
    code = request.args.get('code', '').strip()
    if not code:
        return "需要股票代码", 400
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        analyzer = StockAnalyzer(code, "图表")
        if not analyzer.fetch_data() or not analyzer.calculate_indicators():
            return "数据不足", 500
        df = analyzer.df.tail(60)
        fig, axs = plt.subplots(5, 1, figsize=(12, 15), sharex=True)
        axs[0].plot(df['date'], df['close'], 'k')
        axs[0].plot(df['date'], df['MA5'], label='MA5')
        axs[0].plot(df['date'], df['MA20'], label='MA20')
        axs[0].legend()
        axs[1].bar(df['date'], df['volume'], color='gray')
        axs[1].set_ylabel('Volume')
        axs[2].plot(df['date'], df['K'], label='K')
        axs[2].plot(df['date'], df['D'], label='D')
        axs[2].axhline(50, color='gray')
        axs[2].legend()
        axs[3].bar(df['date'], df['MACD'], color=['red' if v>0 else 'green' for v in df['MACD']])
        axs[3].plot(df['date'], df['DIF'], label='DIF')
        axs[3].plot(df['date'], df['DEA'], label='DEA')
        axs[3].axhline(0, color='gray')
        axs[3].legend()
        axs[4].plot(df['date'], df['RSI'])
        axs[4].axhline(70, linestyle='--', color='r')
        axs[4].axhline(30, linestyle='--', color='g')
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=100)
        buf.seek(0)
        img = base64.b64encode(buf.read()).decode()
        plt.close(fig)
        return f'<html><body><img src="data:image/png;base64,{img}" style="max-width:100%"></body></html>'
    except Exception as e:
        return f"图表生成失败: {e}", 500

@app.route('/scan', methods=['POST'])
def scan():
    data = request.get_json()
    strategy = data.get('strategy', 'short')
    top_n = int(data.get('top_n', 10))
    results = scan_stocks("all", strategy, top_n=top_n)
    if not results:
        return jsonify({"result": "没有符合条件的股票"})
    text = f"📌 全市场{'短线' if strategy=='short' else '波段' if strategy=='band' else '洗盘'}选股结果（前{len(results)}只）：\n"
    for r in results:
        sector_info = f"【{r.get('sector', '')}】" if r.get('sector') else ""
        text += f"\n🏷️ {r['name']}({r['code']}) {sector_info}  收盘:{r['close']}  评分:{r['score']}\n"
        text += f"  风格: {r['advice']['操作风格']}\n"
        text += f"  介入: {r['advice']['介入价']}  止损: {r['advice']['止损价']}  止盈: {r['advice']['止盈价']}\n"
        text += f"  走强确认: {r['advice']['走强确认']}\n"
    return jsonify({"result": text})

# 仓位分析路由（同前）
@app.route('/portfolio', methods=['POST'])
def portfolio():
    # ... 略，同前版实现 ...
    pass

# ================== 回测路由 ==================
backtest_results_cache = []  # 存储最近一次回测结果

@app.route('/backtest', methods=['POST'])
def backtest():
    global backtest_results_cache
    data = request.get_json()
    strategy = data.get('strategy', 'short')
    # 回测最近30个交易日（跳过最近几天避免未来数据）
    end_today = datetime.date.today()
    start_date = end_today - datetime.timedelta(days=60)  # 取60天窗口
    dates = pd.date_range(end=end_today - pd.tseries.offsets.BDay(3), periods=30, freq='B')
    results_list = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(run_backtest, strategy, dt.strftime('%Y-%m-%d')): dt for dt in dates}
        for future in as_completed(futures):
            res = future.result()
            if res:
                results_list.append(res)
    results_list.sort(key=lambda x: x['date'])
    backtest_results_cache = results_list
    if not results_list:
        return jsonify({"result": "回测失败，无有效数据"})
    # 计算汇总统计
    accuracies = [r['accuracy'] for r in results_list]
    avg_acc = sum(accuracies) / len(accuracies)
    text = f"📈 回测完成 ({strategy}策略)  历史平均准确率: {avg_acc:.1f}%\n"
    text += f"最近5日平均准确率: {sum(accuracies[-5:])/min(5,len(accuracies)):.1f}%\n"
    text += f"最近30日平均准确率: {avg_acc:.1f}% (共{len(results_list)}天)\n"
    if len(results_list) > 0:
        yesterday = results_list[-1]
        text += f"昨日({yesterday['date']})准确率: {yesterday['accuracy']:.1f}% ({yesterday['success']}/{yesterday['total']})\n"
    return jsonify({"result": text})

@app.route('/backtest_stats', methods=['POST'])
def backtest_stats():
    if not backtest_results_cache:
        return jsonify({"result": "暂无回测数据，请先运行回测"})
    strategy = request.get_json().get('strategy', 'short')
    # 重新统计（可使用缓存）
    results_list = backtest_results_cache
    accuracies = [r['accuracy'] for r in results_list]
    text = f"📊 回测统计报告（{strategy}）\n"
    text += f"历史总天数: {len(results_list)}  总平均准确率: {sum(accuracies)/len(accuracies):.1f}%\n"
    if len(accuracies) >= 1:
        text += f"昨日准确率: {accuracies[-1]:.1f}%\n"
    if len(accuracies) >= 5:
        text += f"近5日平均: {sum(accuracies[-5:])/5:.1f}%\n"
    if len(accuracies) >= 30:
        text += f"近30日平均: {sum(accuracies[-30:])/30:.1f}%\n"
    return jsonify({"result": text})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
