# -*- coding: utf-8 -*-
"""
finance_toolkit —— A股量化工具箱(零依赖集成版)

来源: ClawHub 技能包 `@dnaxxx-hub/finance-toolkit` v1.1.0 (A股量化工具包)
本文件把其中三项能力移植进「股票盯盘」桌面版, 作为独立标签页使用:

1. 一键回测  <- backtest_v3.py / strategy_v4.py
   10 个策略一次跑完 + 参数网格搜索(敏感性分析) + 权益曲线 + 交易明细
2. 策略引擎  <- strategy_v4.py (多指标融合: 布林带 + RSI + KDJ + MACD)
   实时多指标共振评分 + 买入建议 + 当前应否持仓
3. 日报生成  <- daily_report.py / monitor_v3.py
   大盘指数 + 涨跌分布 + 热点行业 + 热门个股 + 自选股 60 分评分 + 策略信号

移植说明(与原包的差异, 均为有意为之):
- 原包 backtest_v3.py / kds_strategy.py 在发布时换行符丢失, 整个文件被 shebang 行注释掉
  (ast.parse 后模块为空), 无法使用; 故按 strategy_v4.py 中 10 个策略的定义重写。
- 移除原包里的 `curl.exe` 调用、`C:/Python314/python.exe` 解释器桥接与 akshare/pandas/numpy
  依赖, 行情统一走本项目已有的新浪/腾讯接口; 这样可直接打包进单文件 EXE, 体积不变大。
- 指标口径与原包 pandas 实现严格对齐(EMA 用 pandas 默认 adjust=True, 布林标准差用样本
  标准差 ddof=1, RSI 用滚动均值口径, KDJ 用 ewm(span=3)), 已逐一对拍验证。
- 回测成交口径沿用本项目「策略回测」页: 收盘出信号、次日开盘价成交、单边费率 0.12%。
  原包是「当日收盘价成交、不计费用」, 同一组信号下两者数值会有差异, 这里以本项目口径为准。

仅供研究学习, 不构成投资建议。
"""
from __future__ import annotations

import math
from datetime import datetime

FEE = 0.0012        # 单边费率, 与「策略回测」页一致
INIT_CASH = 100000.0
RF_DAILY = 0.02 / 252   # 夏普比率无风险利率(年化2%)

STRATEGY_NOTE = ("收盘出信号 · 次日开盘成交 · 单边费率0.12% · 期初资金10万 · "
                 "指标口径与原包 finance_toolkit(strategy_v4) 对齐；历史回测不代表未来收益")


# ==================== 指标: 基础运算(纯标准库) ====================

def _num(v):
    return v if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)) else None


def _gt(a, b):
    """与 pandas 一致: 任一为 NaN(None) 时比较结果为 False"""
    return a is not None and b is not None and a > b


def _lt(a, b):
    return a is not None and b is not None and a < b


def _ge(a, b):
    return a is not None and b is not None and a >= b


def _le(a, b):
    return a is not None and b is not None and a <= b


def sma(vals, n):
    """简单移动平均, 不足 n 根为 None"""
    out = [None] * len(vals)
    if n <= 0:
        return out
    s = 0.0
    for i, v in enumerate(vals):
        v = _num(v)
        if v is None:
            s = 0.0
            continue
        s += v
        if i >= n:
            s -= _num(vals[i - n]) or 0.0
        if i >= n - 1:
            out[i] = s / n
    return out


def rolling_std(vals, n):
    """滚动样本标准差(ddof=1), 与 pandas Series.rolling(n).std() 一致"""
    out = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        win = [_num(v) for v in vals[i - n + 1:i + 1]]
        if any(v is None for v in win) or n < 2:
            continue
        m = sum(win) / n
        out[i] = math.sqrt(sum((v - m) ** 2 for v in win) / (n - 1))
    return out


def rolling_max(vals, n):
    out = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        win = [_num(v) for v in vals[i - n + 1:i + 1]]
        if any(v is None for v in win):
            continue
        out[i] = max(win)
    return out


def rolling_min(vals, n):
    out = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        win = [_num(v) for v in vals[i - n + 1:i + 1]]
        if any(v is None for v in win):
            continue
        out[i] = min(win)
    return out


def ewm(vals, span):
    """指数加权平均, 与 pandas Series.ewm(span=span).mean() 默认口径(adjust=True)一致"""
    a = 2.0 / (span + 1.0)
    out, num, den = [], 0.0, 0.0
    for v in vals:
        v = _num(v)
        if v is None:            # 与 pandas 一致: 前导 NaN 保持空, 不推进状态
            out.append(None)
            continue
        num = v + (1 - a) * num
        den = 1.0 + (1 - a) * den
        out.append(num / den)
    return out


def ewm_af(vals, span):
    """指数加权平均(adjust=False), 与 pandas Series.ewm(span=span, adjust=False).mean() 一致

    原包 Indicators.EMA 与 mini_realtime.calc_macd 用此口径; 注意与 ewm(adjust=True) 不同。
    """
    a = 2.0 / (span + 1.0)
    out, prev = [], None
    for v in vals:
        v = _num(v)
        if v is None:
            out.append(None)
            continue
        prev = v if prev is None else a * v + (1 - a) * prev
        out.append(prev)
    return out


# ==================== 指标: 技术指标 ====================

def bollinger(closes, period=20, k=2.0):
    """布林带, 返回 (中轨, 上轨, 下轨)"""
    mid = sma(closes, period)
    std = rolling_std(closes, period)
    up = [None if (m is None or s is None) else m + k * s for m, s in zip(mid, std)]
    low = [None if (m is None or s is None) else m - k * s for m, s in zip(mid, std)]
    return mid, up, low


def kdj(highs, lows, closes, n=9, m1=3, m2=3):
    """KDJ, 返回 (K, D, J)"""
    low_n, high_n = rolling_min(lows, n), rolling_max(highs, n)
    rsv = []
    for c, lo, hi in zip(closes, low_n, high_n):
        if lo is None or hi is None or hi == lo:
            rsv.append(None)          # pandas: (hi-lo).replace(0, NaN) => NaN
        else:
            rsv.append(((_num(c) or 0.0) - lo) / (hi - lo) * 100)
    k = ewm(rsv, m1)
    d = ewm(k, m2)
    j = [None if (a is None or b is None) else 3 * a - 2 * b for a, b in zip(k, d)]
    return k, d, j


def macd(closes, fast=12, slow=26, signal=9):
    """MACD, 返回 (DIF, DEA, MACD柱=(DIF-DEA)*2)"""
    ef, es = ewm(closes, fast), ewm(closes, slow)
    dif = [None if (a is None or b is None) else a - b for a, b in zip(ef, es)]
    dea = ewm(dif, signal)
    bar = [None if (a is None or b is None) else (a - b) * 2 for a, b in zip(dif, dea)]
    return dif, dea, bar


def rsi(closes, period=14):
    """RSI(滚动均值口径, 与原包 Indicators.RSI 一致), 不足周期为 None"""
    out = [None] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = (_num(closes[i]) or 0.0) - (_num(closes[i - 1]) or 0.0)
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    for i in range(period, len(closes)):
        g = sum(gains[i - period:i]) / period
        l = sum(losses[i - period:i]) / period
        out[i] = None if l == 0 else 100 - 100 / (1 + g / l)   # pandas: 除零 => NaN
    return out


def atr(highs, lows, closes, period=14):
    """平均真实波幅, 不足周期为 None"""
    tr = []
    for i in range(len(closes)):
        pc = _num(closes[i - 1]) if i else None
        h, l = _num(highs[i]), _num(lows[i])
        if h is None or l is None:
            tr.append(None)
            continue
        cand = [h - l]
        if pc is not None:
            cand.extend([abs(h - pc), abs(l - pc)])
        tr.append(max(cand))
    return sma(tr, period)


def volume_ratio(volumes, period=20):
    """量比 = 当日量 / 近 period 日均量"""
    base = sma(volumes, period)
    return [None if (v is None or b is None or b == 0) else v / b for v, b in zip(volumes, base)]


# ==================== 策略库(与原包 strategy_v4.Strategies 逐行对应) ====================

def _ohlc(d):
    return d["open"], d["high"], d["low"], d["close"], d["volume"]


def _stop_sell(entry, price, stop_loss):
    """固定比例止损"""
    if not stop_loss or not entry:
        return False
    return (price - entry) / entry < -stop_loss


def s_ma_cross(d, fast=5, slow=20, stop_loss=None):
    """MA 双均线金叉买 / 死叉卖"""
    _, _, _, closes, _ = _ohlc(d)
    mf, ms = sma(closes, fast), sma(closes, slow)
    pos, out = False, []
    entry = 0.0
    for i, price in enumerate(closes):
        if not pos and _gt(mf[i], ms[i]):
            pos, entry = True, price
        elif pos:
            sell = _lt(mf[i], ms[i]) or _stop_sell(entry, price, stop_loss)
            if sell:
                pos = False
        out.append(1 if pos else 0)
    return out, max(fast, slow)


def s_boll_reversal(d, period=20, k=2.0, stop_loss=None):
    """布林带反转: 最低价触及下轨且带宽>3% 买入, 回到中轨卖出"""
    _, _, lows, closes, _ = _ohlc(d)
    mid, up, low = bollinger(closes, period, k)
    pos, out = False, []
    entry = 0.0
    for i, price in enumerate(closes):
        bw = None if (up[i] is None or low[i] is None or not mid[i]) else (up[i] - low[i]) / mid[i] * 100
        if not pos:
            if _le(lows[i], low[i]) and _gt(bw, 3):
                pos, entry = True, price
        else:
            sell = _ge(price, mid[i]) or _stop_sell(entry, price, stop_loss)
            if sell:
                pos = False
        out.append(1 if pos else 0)
    return out, period + 1


def s_kdj_overtrade(d, n=9, stop_loss=None):
    """KDJ 超买超卖: K<20 且 J<0 买入, K>80 或 J>100 卖出"""
    _, highs, lows, closes, _ = _ohlc(d)
    k, _, j = kdj(highs, lows, closes, n)
    pos, out = False, []
    entry = 0.0
    for i, price in enumerate(closes):
        if not pos:
            if _lt(k[i], 20) and _lt(j[i], 0):
                pos, entry = True, price
        else:
            sell = _gt(k[i], 80) or _gt(j[i], 100) or _stop_sell(entry, price, stop_loss)
            if sell:
                pos = False
        out.append(1 if pos else 0)
    return out, n


def s_rsi_reversion(d, period=14, oversold=30, overbought=70, stop_loss=None):
    """RSI 均值回归: 超卖买入, 回到 50 上方或超买卖出"""
    _, _, _, closes, _ = _ohlc(d)
    r = rsi(closes, period)
    pos, out = False, []
    entry = 0.0
    for i, price in enumerate(closes):
        if not pos:
            if _lt(r[i], oversold):
                pos, entry = True, price
        else:
            sell = _gt(r[i], overbought) or _gt(r[i], 50) or _stop_sell(entry, price, stop_loss)
            if sell:
                pos = False
        out.append(1 if pos else 0)
    return out, period + 1


def s_macd_cross(d, stop_loss=None):
    """MACD 金叉买 / 死叉卖"""
    _, _, _, closes, _ = _ohlc(d)
    dif, dea, bar = macd(closes)
    pos, out = False, [0]
    entry = 0.0
    for i in range(1, len(closes)):
        if not pos:
            buy = (_lt(bar[i - 1], 0) and _gt(bar[i], 0)) or \
                  (_lt(dif[i - 1], dea[i - 1]) and _gt(dif[i], dea[i]))
            if buy:
                pos, entry = True, closes[i]
        else:
            sell = (_gt(bar[i - 1], 0) and _lt(bar[i], 0)) or \
                   (_gt(dif[i - 1], dea[i - 1]) and _lt(dif[i], dea[i]))
            if sell or _stop_sell(entry, closes[i], stop_loss):
                pos = False
        out.append(1 if pos else 0)
    return out, 35


def s_fusion_ma_bb(d, fast=14, slow=18, bb_period=20, stop_loss=0.05):
    """融合策略一: MA金叉 + RSI不超买 + 不远离布林中轨"""
    _, _, _, closes, _ = _ohlc(d)
    mf, ms = sma(closes, fast), sma(closes, slow)
    mid, _, _ = bollinger(closes, bb_period, 2)
    r = rsi(closes, 14)
    pos, out = False, []
    entry = 0.0
    for i, price in enumerate(closes):
        if not pos:
            if _gt(mf[i], ms[i]) and _lt(r[i], 70) and _le(price, mid[i] * 1.02 if mid[i] else None):
                pos, entry = True, price
        else:
            sell = _lt(mf[i], ms[i]) or _gt(r[i], 80) or _stop_sell(entry, price, stop_loss)
            if sell:
                pos = False
        out.append(1 if pos else 0)
    return out, max(fast, slow, bb_period) + 1


def s_fusion_bb_kdj(d, bb_period=20, kdj_n=9, stop_loss=0.05):
    """融合策略二: 布林触底 + KDJ 超卖 + RSI 低位 共振"""
    _, highs, lows, closes, _ = _ohlc(d)
    mid, _, low = bollinger(closes, bb_period, 2)
    k, _, j = kdj(highs, lows, closes, kdj_n)
    r = rsi(closes, 14)
    pos, out = False, []
    entry = 0.0
    for i, price in enumerate(closes):
        if not pos:
            if _le(lows[i], low[i]) and _lt(k[i], 25) and _lt(r[i], 40):
                pos, entry = True, price
        else:
            sell = _ge(price, mid[i]) or _gt(j[i], 100) or _stop_sell(entry, price, stop_loss)
            if sell:
                pos = False
        out.append(1 if pos else 0)
    return out, max(bb_period, kdj_n) + 1


def s_fusion_triple(d, fast=14, slow=18):
    """融合策略三(三确认): MA金叉 + RSI>50 + 站上布林中轨 + MACD多头, 止损5%"""
    _, _, _, closes, _ = _ohlc(d)
    mf, ms = sma(closes, fast), sma(closes, slow)
    mid, _, _ = bollinger(closes, 20, 2)
    r = rsi(closes, 14)
    dif, dea, _ = macd(closes)
    pos, out = False, []
    entry = 0.0
    for i, price in enumerate(closes):
        if not pos:
            buy = _gt(mf[i], ms[i]) and _gt(r[i], 50) and _gt(price, mid[i]) and _gt(dif[i], dea[i])
            if buy:
                pos, entry = True, price
        else:
            sell = _lt(mf[i], ms[i]) or _gt(r[i], 85) or _lt(price, mid[i]) or _stop_sell(entry, price, 0.05)
            if sell:
                pos = False
        out.append(1 if pos else 0)
    return out, max(fast, slow, 20) + 1


# 与原包 StrategyComparer.run_all 的 10 个策略一一对应
STRATEGIES = [
    {"key": "ma5_20", "name": "MA金叉/死叉 MA5/20", "group": "ma",
     "desc": "5日均线上穿20日买入，下穿卖出", "params": {"fast": 5, "slow": 20}},
    {"key": "ma14_18", "name": "MA金叉/死叉 MA14/18", "group": "ma",
     "desc": "原包调优参数(中国宝安)", "params": {"fast": 14, "slow": 18}},
    {"key": "boll20_2", "name": "布林带反转 20/2", "group": "boll",
     "desc": "触下轨且带宽>3%买入，回中轨卖出", "params": {"period": 20, "k": 2.0}},
    {"key": "boll20_25", "name": "布林带反转 20/2.5", "group": "boll",
     "desc": "更宽的下轨，信号更少", "params": {"period": 20, "k": 2.5}},
    {"key": "kdj9", "name": "KDJ超买超卖 9", "group": "kdj",
     "desc": "K<20且J<0买入，K>80或J>100卖出", "params": {"n": 9}},
    {"key": "rsi14", "name": "RSI均值回归 14/30/70", "group": "rsi",
     "desc": "RSI<30买入，>50或>70卖出", "params": {"period": 14, "oversold": 30, "overbought": 70}},
    {"key": "macd", "name": "MACD金叉死叉", "group": "macd",
     "desc": "MACD柱/DIF-DEA 金叉买、死叉卖", "params": {}},
    {"key": "fusion_ma_bb", "name": "融合: MA14/18+布林中轨+RSI", "group": "fusion",
     "desc": "MA金叉且RSI<70且不远离中轨，止损5%", "params": {"fast": 14, "slow": 18, "bb_period": 20, "stop_loss": 0.05}},
    {"key": "fusion_bb_kdj", "name": "融合: 布林触底+KDJ共振", "group": "fusion",
     "desc": "触下轨+K<25+RSI<40 三重共振，止损5%", "params": {"bb_period": 20, "kdj_n": 9, "stop_loss": 0.05}},
    {"key": "fusion_triple", "name": "融合: 三确认(MA+RSI+BB+MACD)", "group": "fusion",
     "desc": "四项全多头才买入，跌破中轨离场，止损5%", "params": {"fast": 14, "slow": 18}},
]

STRATEGY_MAP = {s["key"]: s for s in STRATEGIES}

_FN_MAP = {
    "ma5_20": s_ma_cross, "ma14_18": s_ma_cross,
    "boll20_2": s_boll_reversal, "boll20_25": s_boll_reversal,
    "kdj9": s_kdj_overtrade, "rsi14": s_rsi_reversion, "macd": s_macd_cross,
    "fusion_ma_bb": s_fusion_ma_bb, "fusion_bb_kdj": s_fusion_bb_kdj,
    "fusion_triple": s_fusion_triple,
}


def generate(key: str, d: dict, **override):
    """按策略 key 生成持仓序列, 返回 (pos, 预热根数 warmup)"""
    meta = STRATEGY_MAP.get(key)
    if meta is None:
        raise KeyError(f"未知策略: {key}")
    params = dict(meta["params"])
    params.update({k: v for k, v in override.items() if v is not None and k in params})
    pos, warm = _FN_MAP[key](d, **params)
    return pos, warm


# ==================== 一键回测: 成交撮合与绩效指标 ====================

def evaluate(d: dict, pos: list, warm: int = 0, fee: float = FEE, cash0: float = INIT_CASH):
    """单策略回测(沿用本项目「策略回测」页口径)。

    收盘出信号 -> 次日开盘价全额买入/清仓, 单边费率 0.12%, 期末持仓按收盘价折算。
    d: {"date":[], "open":[], "high":[], "low":[], "close":[], "volume":[]}
    返回 dict: 权益曲线 / 交易明细 / 收益率 / 年化 / 回撤 / 夏普 / 胜率 / 换手等。
    """
    dates, opens, closes = d["date"], d["open"], d["close"]
    n = len(dates)
    if n == 0:
        raise ValueError("没有K线数据")
    start = max(0, min(int(warm or 0), n - 1))
    cash, shares, pend = cash0, 0.0, None
    in_date = in_px = None
    equity, trades = [], []
    hold_bars = 0
    for i in range(n):
        px_o = _num(opens[i])
        if pend == "buy" and shares == 0 and px_o:
            shares = cash * (1 - fee) / px_o
            cash, in_date, in_px = 0.0, dates[i], px_o
        elif pend == "sell" and shares > 0 and px_o:
            cash = shares * px_o * (1 - fee)
            trades.append({
                "in_date": in_date, "out_date": dates[i],
                "in_px": round(in_px, 3), "out_px": round(px_o, 3),
                "ret": round((px_o * (1 - fee)) / in_px - 1, 6),
                "days": max(1, _trade_days(dates, in_date, dates[i])),
            })
            shares = 0.0
        pend = None
        if i < n - 1:
            if pos[i] == 1 and shares == 0:
                pend = "buy"
            elif pos[i] == 0 and shares > 0:
                pend = "sell"
        if shares > 0:
            hold_bars += 1
        equity.append({
            "date": dates[i],
            "v": round(cash + shares * (closes[i] or 0), 2),
            "sig": pos[i],
            # 基准(买入持有): 从预热点起以期初资金折算, 供权益图对照
            "b": (round((_num(closes[i]) or 0) / (_num(closes[start]) or 1) * cash0, 2)
                  if i >= start and (_num(closes[start]) or 0) > 0 else None),
        })
    # 期末仍持仓: 按最后收盘价折算收益(不计平仓费)
    if shares > 0:
        trades.append({
            "in_date": in_date, "out_date": dates[-1],
            "in_px": round(in_px, 3), "out_px": round(_num(closes[-1]) or in_px, 3),
            "ret": round((_num(closes[-1]) or in_px) / in_px - 1, 6),
            "days": max(1, _trade_days(dates, in_date, dates[-1])),
            "open_pos": True,
        })

    final = equity[-1]["v"]
    total_ret = final / cash0 - 1
    span = max(n - start, 1)
    annual = (final / cash0) ** (252.0 / span) - 1 if final > 0 else -1.0
    peak, maxdd = cash0, 0.0
    for e in equity:
        peak = max(peak, e["v"])
        maxdd = max(maxdd, 1 - e["v"] / peak)
    bench = (_num(closes[-1]) or 0) / (_num(closes[start]) or 1) - 1 if n - start > 0 else 0.0
    # 日收益率 -> 夏普(超额 rf=2%/年) / 索提诺 / 卡玛
    rets = []
    for i in range(max(start, 1), n):
        a, b = equity[i - 1]["v"], equity[i]["v"]
        if a > 0:
            rets.append(b / a - 1)
    mu = sum(rets) / len(rets) if rets else 0.0
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
    sd = math.sqrt(var)
    sharpe = ((mu - RF_DAILY) / sd * math.sqrt(252)) if sd > 1e-12 else 0.0
    downside = [r for r in rets if r < 0]
    dsd = math.sqrt(sum(r ** 2 for r in downside) / len(downside)) if downside else 0.0
    sortino = ((mu - RF_DAILY) / dsd * math.sqrt(252)) if dsd > 1e-12 else 0.0
    calmar = (annual / abs(maxdd)) if maxdd > 1e-9 else 0.0
    wins = [t for t in trades if t["ret"] > 0]
    return {
        "equity": equity,
        "trades": trades,
        "total_ret": round(total_ret * 100, 2),
        "annual": round(annual * 100, 2),
        "maxdd": round(maxdd * 100, 2),
        "bench": round(bench * 100, 2),
        "excess": round((total_ret - bench) * 100, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "calmar": round(calmar, 2),
        "trades_n": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "exposure": round(hold_bars / span * 100, 1),
        "final": round(final, 2),
        "start_date": dates[start],
        "bars": span,
    }


def _trade_days(dates, d1, d2):
    """粗略持仓天数(自然日), 用于明细展示"""
    try:
        a = datetime.strptime(d1, "%Y-%m-%d")
        b = datetime.strptime(d2, "%Y-%m-%d")
        return (b - a).days
    except Exception:
        return 1


# ==================== 一键回测: 10策略横评 + 参数网格搜索 ====================

def run_one(d: dict, key: str, **override) -> dict:
    """单策略一键回测: 生成信号 -> 撮合 -> 绩效"""
    meta = STRATEGY_MAP[key]
    pos, warm = generate(key, d, **override)
    res = evaluate(d, pos, warm)
    res.update({
        "key": key, "name": meta["name"], "group": meta["group"],
        "desc": meta["desc"],
        "params": {**meta["params"], **{k: v for k, v in override.items() if v is not None}},
    })
    return res


def run_all(d: dict) -> dict:
    """一键回测: 10 个策略全部跑一遍, 按夏普排序(对应原包 StrategyComparer.run_all)"""
    rows = []
    for meta in STRATEGIES:
        try:
            rows.append(run_one(d, meta["key"]))
        except Exception as exc:                      # noqa: BLE001
            rows.append({"key": meta["key"], "name": meta["name"], "group": meta["group"],
                         "error": str(exc)})
    ok = [r for r in rows if not r.get("error")]
    ok.sort(key=lambda r: r.get("sharpe", 0), reverse=True)
    err = [r for r in rows if r.get("error")]
    best = ok[0] if ok else None
    # 中位数与赢家, 对应原包报告的"中位数策略/最佳策略/敏感维度"
    sharpes = sorted(r["sharpe"] for r in ok)
    median = sharpes[len(sharpes) // 2] if sharpes else 0.0
    return {"rows": ok + err, "best": best, "n_ok": len(ok), "median_sharpe": median}


# 参数网格(对应原包 StrategyTuner 的调参思路)
SCAN_GRIDS = {
    "ma": {
        "axes": [("fast", "快线周期", [3, 5, 8, 10, 14]), ("slow", "慢线周期", [15, 18, 20, 25, 30, 40])],
        "note": "快慢线组合敏感性",
    },
    "boll": {
        "axes": [("period", "布林周期", [14, 16, 20, 25, 30]), ("k", "带宽倍数", [1.5, 2.0, 2.5, 3.0])],
        "note": "周期与带宽敏感性",
    },
    "rsi": {
        "axes": [("period", "RSI周期", [6, 9, 14, 21]),
                 ("oversold", "超卖阈值", [20, 25, 30, 35]), ("overbought", "超买阈值", [65, 70, 75, 80])],
        "note": "周期与超买超卖阈值敏感性",
    },
    "kdj": {
        "axes": [("n", "KDJ周期", [7, 9, 13, 19, 25])],
        "note": "KDJ周期敏感性",
    },
    "macd": {"axes": [], "note": "MACD无自由参数(12/26/9固定)"},
    "fusion": {
        "axes": [("fast", "快线周期", [10, 14, 18, 20]), ("slow", "慢线周期", [16, 18, 20, 30])],
        "note": "融合策略均线敏感性",
    },
}


def scan(d: dict, key: str) -> dict:
    """参数网格搜索: 遍历该策略维度, 找最优参数并评估敏感性"""
    meta = STRATEGY_MAP.get(key)
    if meta is None:
        raise KeyError(f"未知策略: {key}")
    grid = SCAN_GRIDS.get(meta["group"], {"axes": [], "note": ""})
    base = dict(meta["params"])
    combos = [{}]
    for axis_key, axis_name, vals in grid["axes"]:
        combos = [dict(c, **{axis_key: v}) for c in combos for v in vals]
    allow = _param_names(key)
    rows, seen = [], set()
    for combo in combos:
        fn_params = {k: v for k, v in {**base, **combo}.items() if k in allow}
        tag = tuple(sorted(fn_params.items()))
        if tag in seen:            # 该维度与本策略无关时去重
            continue
        seen.add(tag)
        try:
            pos, warm = _FN_MAP[key](d, **fn_params)
            res = evaluate(d, pos, warm)
            shown = {k: v for k, v in combo.items() if k in allow} or dict(base)
            row = {"params": shown, "total_ret": res["total_ret"],
                   "annual": res["annual"], "maxdd": res["maxdd"], "sharpe": res["sharpe"],
                   "trades_n": res["trades_n"], "win_rate": res["win_rate"]}
            rows.append(row)
        except Exception:                              # noqa: BLE001
            continue
    rows.sort(key=lambda r: r["sharpe"], reverse=True)
    best = rows[0] if rows else None
    sharpes = sorted(r["sharpe"] for r in rows)
    lo = sharpes[:max(1, len(sharpes) // 4)]
    hi = sharpes[-max(1, len(sharpes) // 4):]
    gap = (sum(hi) / len(hi)) - (sum(lo) / len(lo)) if len(rows) >= 4 else 0.0
    # 敏感性: 按每个维度统计收益极差, 极差最大者为敏感维度
    sens = []
    for axis_key, axis_name, vals in grid["axes"]:
        vals_seen = {}
        for r in rows:
            if axis_key in r["params"]:
                vals_seen.setdefault(r["params"][axis_key], []).append(r["total_ret"])
        if len(vals_seen) >= 2:
            means = [sum(v) / len(v) for v in vals_seen.values()]
            sens.append({"dim": axis_name, "spread": round(max(means) - min(means), 2)})
    sens.sort(key=lambda x: x["spread"], reverse=True)
    return {
        "key": key, "name": meta["name"], "note": grid["note"],
        "rows": rows, "best": best, "n": len(rows),
        "median_sharpe": sharpes[len(sharpes) // 2] if sharpes else 0.0,
        "sharpe_gap": round(gap, 2),
        "sensitivity": sens,
        "sensitive_dim": sens[0]["dim"] if sens else "无(固定参数)",
    }


def _param_names(key):
    meta = STRATEGY_MAP[key]
    import inspect
    return set(inspect.signature(_FN_MAP[key]).parameters) - {"d"}


# ==================== 策略引擎: 实时多指标共振(原包 RealTimeAnalyst) ====================

def _last(seq):
    for v in reversed(seq):
        if v is not None:
            return v
    return None


def live_analysis(d: dict) -> dict:
    """实时多指标分析(移植 strategy_v4.RealTimeAnalyst.live_analysis)。

    需要 d 至少 20 根日K; 返回价格 / MA14·18·20·60 / KDJ / RSI / MACD /
    布林位置 / 支撑压力 / ATR / 文本信号。
    """
    if not d or len(d.get("close", [])) < 20:
        return {"error": "数据不足(需要>=20根日K)"}
    closes, highs, lows = d["close"], d["high"], d["low"]
    price = _last(closes)
    ma14, ma18, ma20 = _last(sma(closes, 14)), _last(sma(closes, 18)), _last(sma(closes, 20))
    ma60 = _last(sma(closes, 60)) if len(closes) >= 60 else price
    r = _last(rsi(closes, 14))
    k_seq, dj_seq, j_seq = kdj(highs, lows, closes, 9)
    k, kd, j = _last(k_seq), _last(dj_seq), _last(j_seq)
    mid, up, low = bollinger(closes, 20, 2)
    mid_v, up_v, low_v = _last(mid), _last(up), _last(low)
    dif, dea, bar = macd(closes)
    dif_v, dea_v, bar_v = _last(dif), _last(dea), _last(bar)
    atr_v = _last(atr(highs, lows, closes, 14))

    if None in (price, ma14, ma18, r, k, j, mid_v, up_v, low_v, dif_v, dea_v):
        return {"error": "指标数据缺失"}

    signals = []
    signals.append("🟢 MA金叉" if _gt(ma14, ma18) else "🔴 MA死叉")
    if r < 30:
        signals.append("⚠️ RSI超卖")
    elif r > 70:
        signals.append("⚠️ RSI超买")
    else:
        signals.append(f"RSI中性({r:.0f})")
    span = (up_v - low_v) or 1e-9
    bb_pos = (price - low_v) / span * 100
    if price <= low_v:
        signals.append("📊 触下轨")
    elif price >= up_v:
        signals.append("📊 触上轨")
    else:
        signals.append(f"布林{bb_pos:.0f}%位置")
    if k < 20 and j < 0:
        signals.append("📈 KDJ超卖")
    elif k > 80 and j > 100:
        signals.append("📉 KDJ超买")
    if dif_v > dea_v and (bar_v or 0) > 0:
        signals.append("💡 MACD多头")
    elif dif_v < dea_v:
        signals.append("💡 MACD空头")

    return {
        "price": round(price, 2), "signals": " | ".join(signals),
        "ma14": round(ma14, 2), "ma18": round(ma18, 2),
        "ma20": round(ma20, 2), "ma60": round(ma60, 2),
        "rsi": round(r, 1),
        "k": round(k, 1), "d": round(kd, 1), "j": round(j, 1),
        "bb_pos": round(bb_pos, 0),
        "macd": "多头✔️" if dif_v > dea_v else "空头❌",
        "atr": round(atr_v, 3) if atr_v else None,
        "support": round(low_v, 2), "resistance": round(up_v, 2),
        "bb_lower": round(low_v, 2), "bb_upper": round(up_v, 2),
        "bars": len(closes),
    }


def buy_suggest(ta: dict) -> dict:
    """综合买入建议(移植 strategy_v4 主流程的 60 分评分与推荐话术)"""
    if not ta or "error" in ta:
        return {"error": (ta or {}).get("error", "无数据")}
    score, reasons = 0, []
    if _gt(ta.get("ma14"), ta.get("ma18")):
        score += 10; reasons.append("MA金叉")
    else:
        reasons.append("MA死叉")
    if ta.get("rsi", 100) < 70:
        score += 10; reasons.append("RSI正常")
    else:
        reasons.append("RSI超买⚠️")
    if ta.get("rsi", 100) < 30:
        score += 15; reasons.append("RSI超卖⚡")
    bb = ta.get("bb_pos")
    if bb is not None:
        if bb < 20:
            score += 15; reasons.append("布林低位⚡")
        elif bb < 40:
            score += 10; reasons.append("布林中低位")
        elif bb > 80:
            score -= 10; reasons.append("布林高位⚠️")
    k = ta.get("k")
    if k is not None:
        if k < 20:
            score += 10; reasons.append("KDJ超卖")
        elif k > 80:
            score -= 5; reasons.append("KDJ超买")
    if "多头" in (ta.get("macd") or ""):
        score += 10; reasons.append("MACD多头")
    else:
        reasons.append("MACD空头")
    if score >= 45:
        rec, level = "✅ 强烈买入信号", "strong_buy"
    elif score >= 30:
        rec, level = "🟢 可考虑买入", "buy"
    elif score >= 15:
        rec, level = "⚪ 观望", "hold"
    else:
        rec, level = "🔴 回避", "avoid"
    return {"score": score, "max_score": 60, "recommend": rec, "level": level,
            "reasons": reasons}


# ==================== 日报: 自选股60分评分(原包 monitor_v3.score_stock) ====================

def score_stock(d: dict, price: float, change_pct: float, code: str = "", name: str = "") -> dict:
    """60分评分体系(移植 monitor_v3.score_stock, 6个维度各10分)。

    MA位置10 + 布林位置10 + RSI10 + KDJ10 + MACD10 + 涨跌幅趋势10。
    需要 d >= 30 根日K(原包取60根)。
    """
    if not d or len(d.get("close", [])) < 30:
        return {"error": "数据不足", "code": code, "name": name}
    closes, highs, lows = d["close"], d["high"], d["low"]
    score, signals = 0, []

    # 1. MA位置(10分)
    ma5, ma10, ma20 = _last(sma(closes, 5)), _last(sma(closes, 10)), _last(sma(closes, 20))
    if None not in (ma5, ma10, ma20):
        if price > ma20:
            score += 5; signals.append("↑MA20")
        elif price > ma10:
            score += 3
        if price > ma5:
            score += 3; signals.append("↑MA5")
        if ma5 > ma10 > ma20:
            score += 2; signals.append("多头排列")

    # 2. 布林带位置(10分) — 原包用总体标准差(除以20), 此处保持一致
    mid20 = _last(sma(closes, 20))
    if mid20:
        std = math.sqrt(sum((c - mid20) ** 2 for c in closes[-20:]) / 20)
        upper, lower = mid20 + 2 * std, mid20 - 2 * std
        if price <= lower:
            score += 8; signals.append("触下轨⚡")
        elif price <= mid20:
            score += 4; signals.append("中轨下方")
        elif price > upper:
            score += 2

    # 3. RSI(10分)
    r = _last(rsi(closes, 14))
    if r is not None:
        if r < 25:
            score += 10; signals.append("RSI超卖")
        elif r < 35:
            score += 6; signals.append("RSI偏低")
        elif 35 <= r <= 65:
            score += 5; signals.append("RSI中性")
        elif r > 75:
            score += 2; signals.append("RSI超买")

    # 4. KDJ(10分) — 原包 J>100 分支写漏了 append, 此处补上信号文案
    k_seq, d_seq, j_seq = kdj(highs, lows, closes, 9)
    jv = _last(j_seq)
    if jv is not None:
        if jv < 0:
            score += 10; signals.append("J<0超卖")
        elif jv < 20:
            score += 6; signals.append("J值偏低")
        elif jv > 100:
            signals.append("J>100超买")

    # 5. MACD(10分)
    dif, dea, _ = macd(closes)
    if len(dif) >= 2 and None not in (dif[-1], dea[-1], dif[-2], dea[-2]):
        if dif[-1] > dea[-1]:
            score += 6; signals.append("MACD金叉")
            if dif[-2] <= dea[-2]:
                score += 4; signals.append("金叉刚形成")
        elif dif[-2] >= dea[-2]:
            score -= 2; signals.append("死叉刚形成")

    # 6. 涨跌幅趋势(10分)
    if change_pct > 0:
        score += 3
    if change_pct > 2:
        score += 3; signals.append("涨幅>2%")
    if len(closes) > 5 and _num(closes[-1]) is not None and _num(closes[-6]) is not None:
        if closes[-1] - closes[-6] > 0:
            score += 4; signals.append("5日上涨趋势")

    if score >= 40:
        direction = "强烈看多 ✅✅"
    elif score >= 30:
        direction = "看多 ✅"
    elif score >= 20:
        direction = "中性 ➖"
    elif score >= 10:
        direction = "看空 ❌"
    else:
        direction = "强烈看空 ❌❌"
    k_last, d_last = _last(k_seq), _last(d_seq)
    return {
        "code": code, "name": name, "price": price, "change_pct": change_pct,
        "score": score, "max_score": 60, "direction": direction, "signals": signals,
        "ma5": round(ma5, 2) if ma5 else None, "ma10": round(ma10, 2) if ma10 else None,
        "ma20": round(ma20, 2) if ma20 else None,
        "rsi": round(r, 1) if r is not None else None,
        "kdj": (f"K={k_last:.0f} D={d_last:.0f} J={jv:.0f}"
                if None not in (k_last, d_last, jv) else ""),
        "macd": (f"DIF={dif[-1]:.3f} DEA={dea[-1]:.3f}"
                 if dif and dif[-1] is not None and dea[-1] is not None else ""),
    }


# ==================== 日报: 融合策略当前持仓状态 ====================

def fusion_status(d: dict) -> list:
    """三个融合策略的当前持仓与最近一次买卖信号日期(日报用)"""
    out = []
    for key in ("fusion_ma_bb", "fusion_bb_kdj", "fusion_triple"):
        meta = STRATEGY_MAP[key]
        try:
            pos, warm = generate(key, d)
        except Exception as exc:                          # noqa: BLE001
            out.append({"key": key, "name": meta["name"], "error": str(exc)})
            continue
        holding = bool(pos[-1])
        last_chg = next((i for i in range(len(pos) - 1, 0, -1) if pos[i] != pos[i - 1]), None)
        out.append({
            "key": key, "name": meta["name"], "holding": holding,
            "last_action": ("买入" if pos[last_chg] == 1 else "卖出") if last_chg else None,
            "last_action_date": d["date"][last_chg] if last_chg else None,
        })
    return out


# ==================== 日报: Markdown 渲染(原包 format_report_for_fei) ====================

def render_report_md(report: dict) -> str:
    """把日报 JSON 渲染成 Markdown 文本(可复制/可推送), 结构沿用原包飞书格式"""
    lines = ["📊 **A股市场日报**", f"🕐 {report.get('report_time', '')}", ""]

    ov = report.get("market_overview") or []
    if ov:
        lines.append("**📈 大盘指数**")
        for it in ov:
            g = it.get("change_pct")
            arrow = "🟢" if (g is not None and g >= 0) else "🔴"
            if g is None:
                lines.append(f"{arrow} {it['name']}: {it.get('latest') or '--'}")
            else:
                lines.append(f"{arrow} {it['name']}: {it['latest']} ({g:+.2f}%)")
        lines.append("")
    br = report.get("breadth")
    if br:
        lines.append("**📊 涨跌分布**")
        lines.append(f"上涨 {br.get('up', 0)} · 平盘 {br.get('flat', 0)} · 下跌 {br.get('down', 0)}"
                     f" · 涨停 {br.get('limit_up', 0)} · 跌停 {br.get('limit_dn', 0)}")
        lines.append("")
    hc = report.get("hot_concepts") or []
    if hc:
        lines.append("**🏷️ 热门概念**")
        for b in hc[:5]:
            g = b.get("chg")
            arrow = "🟢" if (g is not None and g >= 0) else "🔴"
            chg_s = f"{g:+.2f}%" if g is not None else "--"
            lines.append(f"{arrow} {b['name']}: {chg_s} (涨{b['up']}/跌{b['down']})")
        lines.append("")
    hs = report.get("hot_stocks") or []
    if hs:
        lines.append("**🔥 热门个股 Top10**")
        for s in hs[:10]:
            arrow = "🟢" if s["change_pct"] >= 0 else "🔴"
            lines.append(f"{arrow} {s['name']}({s['code']}): {s['price']:.2f} ({s['change_pct']:+.2f}%)")
        lines.append("")
    ws = report.get("watch_scores") or []
    if ws:
        lines.append("**🎯 自选股60分评分**")
        for s in ws:
            arrow = "🟢" if s.get("change_pct", 0) >= 0 else "🔴"
            sig = " ".join(s.get("signals") or [])
            lines.append(f"{arrow} {s.get('name', '')}({s.get('code', '')}) "
                         f"{s.get('score', '-')}/60 {s.get('direction', '')} · {sig}")
        lines.append("")
    sg = report.get("strategy_signals") or []
    if sg:
        lines.append("**🔍 技术信号**")
        for s in sg[:10]:
            lines.append(f"📌 {s.get('name', '')}({s.get('code', '')}): {s.get('price', '')}")
            lines.append(f"   {s.get('signals', '')}")
        lines.append("")
    fs = report.get("fusion") or []
    if fs:
        lines.append("**⚙️ 融合策略状态**")
        for f in fs:
            state = "持仓中📈" if f.get("holding") else "空仓⬜"
            when = (f" · 最近{f.get('last_action')} {f.get('last_action_date')}"
                    if f.get("last_action") else "")
            lines.append(f"- {f.get('name', '')}: {state}{when}")
        lines.append("")
    lines.append(f"*{report.get('note', STRATEGY_NOTE)}*")
    return "\n".join(lines)

