# -*- coding: utf-8 -*-
"""
股票盯盘 - 后端服务
数据源: 新浪财经(全市场列表/选股) + 腾讯财经(指数/分时/K线)
仅供研究参考, 不构成投资建议
"""
import json
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from fastapi import Body, FastAPI, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import finance_toolkit as ft

def _get_cst():
    """获取上海时区。

    Windows 自身不携带 IANA 时区数据库, zoneinfo 依赖 PyPI 的 tzdata 包;
    若该包缺失(或打包后未被收集), 回退为固定 UTC+8 偏移, 保证服务仍能启动。
    """
    try:
        return ZoneInfo("Asia/Shanghai")
    except Exception:
        return timezone(timedelta(hours=8), "CST")


CST = _get_cst()
BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}

SINA_LIST_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
SINA_COUNT_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount"
SINA_KLINE_URL = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData"
TENCENT_HOSTS = [
    "https://proxy.finance.qq.com/ifzqgtimg",
    "https://web.ifzq.gtimg.cn",
]
TENCENT_KLINE_PATH = "/appstock/app/fqkline/get"
TENCENT_MINUTE_PATH = "/appstock/app/minute/query"
host_lock = threading.Lock()
host_idx = 0

INDICES = [
    {"code": "sh000001", "name": "上证指数"},
    {"code": "sz399001", "name": "深证成指"},
    {"code": "sz399006", "name": "创业板指"},
    {"code": "sh000300", "name": "沪深300"},
    {"code": "sh000688", "name": "科创50"},
]

session = requests.Session()
session.headers.update(UA_HEADERS)
session.headers["Referer"] = "https://finance.sina.com.cn"


def _f(v):
    try:
        s = str(v).strip()
        if s in ("", "-", "--"):
            return None
        return float(s)
    except Exception:
        return None


def now_cst():
    return datetime.now(CST)


def market_phase():
    """返回 (状态文本, 是否快速刷新)"""
    d = now_cst()
    if d.weekday() >= 5:
        return "周末休市", False
    t = d.hour * 60 + d.minute
    if 9 * 60 + 15 <= t < 9 * 60 + 30:
        return "集合竞价", True
    if (9 * 60 + 30 <= t < 11 * 60 + 30) or (13 * 60 <= t < 15 * 60):
        return "交易中", True
    return "已收盘", False


class Cache:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = None
        self.ts = 0.0

    def get(self):
        with self.lock:
            return self.data, self.ts

    def set(self, data):
        with self.lock:
            self.data = data
            self.ts = time.time()


all_cache = Cache()      # 全市场快照
summary_cache = Cache()  # 指数
minute_cache = {}        # code -> Cache
kline_cache = {}         # (code, period, lmt) -> Cache
meta_lock = threading.Lock()


def get_cache(store, key):
    with meta_lock:
        if key not in store:
            store[key] = Cache()
        return store[key]


# ---------------- 新浪全市场 ----------------

def fetch_sina_page(page: int, num: int = 100, node: str = "hs_a"):
    r = session.get(
        SINA_LIST_URL,
        params={"page": page, "num": num, "sort": "amount", "asc": 0, "node": node},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def fetch_market_count() -> int:
    r = session.get(SINA_COUNT_URL, params={"node": "hs_a"}, timeout=10)
    r.raise_for_status()
    return int(str(r.text).strip().strip('"'))


def normalize_row(raw: dict) -> dict:
    return {
        "c": str(raw.get("code", "")),
        "sym": str(raw.get("symbol", "")),
        "n": raw.get("name", ""),
        "p": _f(raw.get("trade")),
        "pc": _f(raw.get("pricechange")),
        "chg": _f(raw.get("changepercent")),
        "o": _f(raw.get("open")),
        "h": _f(raw.get("high")),
        "l": _f(raw.get("low")),
        "pre": _f(raw.get("settlement")),
        "v": _f(raw.get("volume")),
        "a": _f(raw.get("amount")),
        "t": raw.get("ticktime", ""),
        "pe": _f(raw.get("per")),
        "pb": _f(raw.get("pb")),
        "mc": _f(raw.get("mktcap")),
        "fmc": _f(raw.get("nmc")),
        "tr": _f(raw.get("turnoverratio")),
    }


def build_all_snapshot():
    count = fetch_market_count()
    count = min(count, 6500)
    pages = max(1, math.ceil(count / 100))
    pages = min(pages, 70)
    rows = [None] * pages

    def work(i):
        for attempt in range(2):
            try:
                rows[i] = fetch_sina_page(i + 1)
                return
            except Exception:
                time.sleep(0.6)
        rows[i] = []

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(work, range(pages)))

    flat = []
    for chunk in rows:
        if chunk:
            flat.extend(normalize_row(x) for x in chunk if isinstance(x, dict))
    by_code = {}
    for r in flat:
        if r["c"] and r["c"] not in by_code:
            by_code[r["c"]] = r
    return {"count": len(by_code), "rows": list(by_code.values()), "by_code": by_code}


def all_refresher():
    while True:
        phase, fast = market_phase()
        ttl = 50 if fast else 600
        _, ts = all_cache.get()
        try:
            if all_cache.data is None or time.time() - ts > ttl:
                snap = build_all_snapshot()
                if snap["count"] > 0:
                    all_cache.set(snap)
        except Exception:
            pass
        time.sleep(5 if all_cache.data is None else min(ttl, 30))


# ---------------- 腾讯 指数/K线/分时 ----------------

def parse_qt(qt: list):
    def g(i):
        return qt[i] if i < len(qt) else ""

    return {
        "name": g(1),
        "price": _f(g(3)),
        "prev": _f(g(4)),
        "open": _f(g(5)),
        "time": g(30),
        "pc": _f(g(31)),
        "chg": _f(g(32)),
        "high": _f(g(33)),
        "low": _f(g(34)),
        "volume": _f(g(36)),      # 手
        "amount": _f(g(37)),      # 万元
        "turnover": _f(g(38)),
        "pe": _f(g(39)),
        "fmc": _f(g(44)),         # 亿
        "mc": _f(g(45)),          # 亿
        "pb": _f(g(46)),
        "limit_up": _f(g(47)),
        "limit_down": _f(g(48)),
    }


def norm_code(code: str) -> str:
    """6位代码自动补交易所前缀: 6/9开头→sh, 0/2/3→sz, 4/8→bj"""
    code = str(code).strip().lower()
    if code.startswith(("sh", "sz", "bj")):
        return code
    if re.fullmatch(r"\d{6}", code):
        h = code[0]
        if h in "69":
            return "sh" + code
        if h in "48":
            return "bj" + code
        return "sz" + code
    return code


def _next_host():
    global host_idx
    with host_lock:
        host_idx += 1
        return TENCENT_HOSTS[host_idx % len(TENCENT_HOSTS)]


def _current_host():
    return TENCENT_HOSTS[host_idx % len(TENCENT_HOSTS)]


def tencent_get(path_and_query: str, tries: int = 2):
    """腾讯接口请求: 多域名轮换, 501视为限流自动切换"""
    last_exc = None
    for _ in range(tries):
        url = _current_host() + path_and_query
        try:
            r = session.get(url, timeout=10, headers={"Referer": "https://gu.qq.com/"})
            if r.status_code == 200:
                j = r.json()
                if j.get("code") == 0:
                    return j
                raise RuntimeError(j.get("msg", "tencent error"))
            last_exc = RuntimeError(f"tencent {r.status_code}")
            _next_host()
        except Exception as e:
            last_exc = e
            _next_host()
    raise last_exc


def fetch_sina_kline(code: str, datalen: int):
    """新浪日K兜底: 不复权, 成交量单位为股, 统一转换为手"""
    url = SINA_KLINE_URL + f"?symbol={code}&scale=240&ma=no&datalen={datalen}"
    r = session.get(url, timeout=10,
                    headers={"Referer": "https://finance.sina.com.cn"})
    r.raise_for_status()
    # jsonp: var _=([...])
    txt = r.text
    arr = json.loads(txt[txt.find("["): txt.rfind("]") + 1])
    return [[a["day"], a["open"], a["close"], a["high"], a["low"],
             str(round(float(a["volume"]) / 100))] for a in arr[-datalen:]]


def fetch_tencent_kline_raw(code: str, period: str, lmt: int, fq: str):
    # 注意: 逗号不能被URL编码, 直接拼query
    j = tencent_get(f"{TENCENT_KLINE_PATH}?param={code},{period},,,{lmt},{fq}")
    node = j["data"][code]
    key = f"{fq}{period}" if fq else period
    bars = node.get(key) or node.get(period) or []
    qt = (node.get("qt") or {}).get(code) or []
    return bars, qt


def fetch_kline_with_fallback(code: str, period: str, lmt: int, fq: str):
    """优先腾讯(复权/周月/实时qt), 失败则新浪日K兜底(不复权,无qt)"""
    try:
        return fetch_tencent_kline_raw(code, period, lmt, fq)
    except Exception:
        if period != "day":
            raise
        bars = fetch_sina_kline(code, lmt)
        return bars, []


def fetch_summary():
    out = []
    for idx in INDICES:
        try:
            bars, qt = fetch_tencent_kline_raw(idx["code"], "day", 2, "")
            q = parse_qt(qt) if qt else {}
            out.append({
                "code": idx["code"], "name": idx["name"],
                "price": q.get("price"), "pc": q.get("pc"), "chg": q.get("chg"),
                "open": q.get("open"), "high": q.get("high"), "low": q.get("low"),
                "prev": q.get("prev"), "amount": q.get("amount"),
                "time": q.get("time", ""),
            })
        except Exception:
            # 新浪日K兜底: 用最近两根日K算收盘与涨跌幅
            try:
                bars = fetch_sina_kline(idx["code"], 2)
                if len(bars) >= 2:
                    pre_c = float(bars[-2][2])
                    cur = float(bars[-1][2])
                    out.append({
                        "code": idx["code"], "name": idx["name"],
                        "price": cur, "pc": round(cur - pre_c, 2),
                        "chg": round((cur - pre_c) / pre_c * 100, 2),
                        "open": _f(bars[-1][1]), "high": _f(bars[-1][3]),
                        "low": _f(bars[-1][4]), "prev": pre_c,
                        "amount": None, "time": bars[-1][0].replace("-", ""),
                    })
                    continue
            except Exception:
                pass
            out.append({"code": idx["code"], "name": idx["name"], "price": None})
    return out


def fetch_minute(code: str):
    j = tencent_get(f"{TENCENT_MINUTE_PATH}?code={code}")
    node = j["data"][code]
    items = node.get("data", {}).get("data", [])
    date = node.get("data", {}).get("date", "")
    qt = (node.get("qt") or {}).get(code) or []
    rows = []
    for s in items:
        parts = s.split()
        # 只保留 09:30-15:00 连续竞价时段, 过滤盘后定价成交
        if len(parts) >= 4 and parts[0] <= "1500":
            rows.append({"tm": parts[0], "price": _f(parts[1]),
                         "cvol": _f(parts[2]), "camount": _f(parts[3])})
    q = parse_qt(qt) if qt else {}
    return {"date": date, "points": rows, "prev": q.get("prev"),
            "name": q.get("name"), "qt": q}


# ---------------- 资讯/政策信号 ----------------

SINA_ZHIBO_URL = "https://zhibo.sina.com.cn/api/zhibo/feed"
SINA_HY_URL = "http://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"

# 政策来源关键词
POLICY_SRC = [
    "国务院", "国常会", "政治局", "中央会议", "央行", "中国人民银行", "证监会",
    "金融监管总局", "财政部", "发改委", "工信部", "住建部", "商务部",
    "农业农村部", "国资委", "税务总局", "能源局", "教育部", "国资委",
]
# 情绪词
POS_WORDS = [
    "利好", "支持", "补贴", "扶持", "加快", "促进", "推进", "扩大", "超预期",
    "增长", "回暖", "复苏", "扩产", "中标", "签约", "获批", "批准", "出台",
    "印发", "减税", "降费", "降准", "降息", "宽松", "涨价", "提价", "旺季",
    "需求旺盛", "突破", "创新高", "增持", "回购", "回暖", "加码", "密集",
]
NEG_WORDS = [
    "利空", "下调", "下降", "下滑", "回落", "下跌", "处罚", "立案", "调查",
    "警示", "退市", "亏损", "预亏", "爆雷", "违约", "减持", "质押", "限制",
    "禁止", "禁令", "制裁", "加征关税", "关税", "约谈", "召回", "整顿",
    "收紧", "出清", "滞销", "过剩",
]
# 新闻关键词 -> 新浪行业名(与 newSinaHy.php 的行业名对应)
INDUSTRY_KEYWORDS = {
    "金融行业": ["银行", "降准", "降息", "LPR", "信贷", "货币政策", "证券", "券商",
                "保险", "险资", "IPO", "并购重组", "资本市场", "金融监管", "汇率"],
    "电子器件": ["半导体", "芯片", "晶圆", "光刻", "集成电路", "存储", "封测",
                "面板", "OLED", "显示", "功率器件"],
    "电子信息": ["软件", "信创", "操作系统", "网络安全", "数据要素", "数字经济",
                "人工智能", "AI", "大模型", "算力", "智能体", "通信", "5G", "6G",
                "光模块", "云计算", "卫星互联网"],
    "传媒娱乐": ["游戏", "影视", "传媒", "短剧", "出版", "票房", "演出", "文创"],
    "发电设备": ["光伏", "风电", "储能", "锂电", "电池", "固态电池", "核电",
                "特高压", "电力设备", "氢能", "新能源装机"],
    "飞机制造": ["军工", "国防", "航空航天", "大飞机", "低空经济", "无人机",
                "火箭", "导弹", "商业航天"],
    "酿酒行业": ["白酒", "啤酒", "葡萄酒", "黄酒", "酒企", "酒类"],
    "房地产": ["房地产", "楼市", "住房", "房贷", "地产", "保障房", "城中村", "物业"],
    "钢铁行业": ["钢铁", "粗钢", "铁矿石", "钢材"],
    "煤炭行业": ["煤炭", "动力煤", "焦煤", "焦炭", "电煤"],
    "有色金属": ["有色", "稀土", "黄金", "贵金属", "白银", "镍", "钨", "铜价", "铝价", "锂价"],
    "化工行业": ["化工", "纯碱", "尿素", "磷化工", "氟化工", "钛白粉", "涂料"],
    "石油行业": ["石油", "原油", "油气", "油价", "页岩气"],
    "电力行业": ["电力", "电价", "用电负荷", "电力供应", "绿电交易", "电煤储备"],
    "汽车制造": ["汽车", "新能源车", "智能驾驶", "自动驾驶", "车企", "整车",
                "汽车零部件", "汽车消费"],
    "家电行业": ["家电", "白电", "空调", "冰箱", "洗衣机", "彩电", "智能家居"],
    "生物制药": ["医药", "创新药", "集采", "医保", "疫苗", "中药", "药品", "临床试验"],
    "医疗器械": ["医疗器械", "医疗设备", "体外诊断", "骨科耗材"],
    "农林牧渔": ["农业", "种业", "粮食", "生猪", "养殖", "转基因", "饲料", "水产", "猪肉"],
    "农药化肥": ["农药", "化肥", "磷肥", "钾肥"],
    "建筑建材": ["基建", "专项债", "水利", "建筑", "一带一路", "城市更新", "建材"],
    "水泥行业": ["水泥", "熟料"],
    "玻璃行业": ["玻璃", "光伏玻璃", "浮法玻璃"],
    "交通运输": ["航运", "港口", "物流", "快递", "铁路", "机场", "航空出行", "集运"],
    "酒店旅游": ["旅游", "酒店", "免税", "景区", "度假", "文旅"],
    "食品行业": ["食品", "饮料", "乳业", "预制菜", "调味品", "零食"],
    "商业百货": ["零售", "电商", "消费券", "商贸", "百货", "超市"],
    "环保行业": ["环保", "水务", "固废", "垃圾处理", "碳排放", "双碳", "节能"],
    "机械行业": ["机械", "工程机械", "机床", "机器人", "人形机器人", "自动化", "盾构"],
    "仪器仪表": ["仪器仪表", "科学仪器", "检测认证"],
    "供水供气": ["供水", "供气", "燃气"],
    "纺织行业": ["纺织", "棉花"],
    "服装鞋类": ["服装", "鞋服", "品牌服饰"],
    "造纸行业": ["造纸", "纸价", "纸浆"],
    "塑料制品": ["塑料", "改性塑料"],
    "船舶制造": ["船舶", "造船", "海工装备"],
    "陶瓷行业": ["陶瓷"],
    "家具行业": ["家具", "家居", "地板"],
    "摩托车": ["摩托车", "电动两轮车"],
    "化纤行业": ["化纤", "涤纶", "粘胶"],
    "电器行业": ["电器", "低压电器", "配电设备"],
}

news_cache = Cache()
hy_list_cache = Cache()
hy_members_cache = {}
signal_busy = threading.Lock()


def fetch_industry_list():
    """新浪行业名 -> 节点码, 缓存12小时"""
    data, ts = hy_list_cache.get()
    if data and time.time() - ts < 43200:
        return data
    try:
        r = session.get(SINA_HY_URL, timeout=10)
        raw = r.content.decode("gbk", errors="ignore")
        m = re.search(r"\{(.*)\}", raw, re.S)
        d = json.loads("{" + m.group(1) + "}".replace("'", '"'))
        out = {}
        for k, v in d.items():
            parts = str(v).split(",")
            if len(parts) >= 2:
                out[parts[1]] = parts[0]
        if out:
            hy_list_cache.set(out)
            return out
    except Exception:
        pass
    return data or {}


def fetch_node_members(node: str):
    """行业成分股代码列表, 缓存6小时"""
    cache = get_cache(hy_members_cache, node)
    data, ts = cache.get()
    if data and time.time() - ts < 21600:
        return data
    try:
        count = int(str(session.get(
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount",
            params={"node": node}, timeout=10).text).strip().strip('"'))
        pages = max(1, min(math.ceil(count / 100), 12))
        codes = []
        for p in range(1, pages + 1):
            try:
                rows = fetch_sina_page(p, node=node)
            except Exception:
                continue
            codes.extend(r.get("code", "") for r in rows if r.get("code"))
        if codes:
            cache.set(codes)
            return codes
    except Exception:
        pass
    return data or []


def fetch_zhibo_news():
    """新浪7x24快讯, 拉前3页"""
    items = []
    for page in (1, 2, 3):
        try:
            r = session.get(SINA_ZHIBO_URL,
                            params={"zhibo_id": 152, "page": page, "page_size": 100},
                            timeout=10, headers={"Referer": "https://finance.sina.com.cn"})
            lst = r.json()["result"]["data"]["feed"]["list"]
            items.extend(lst)
        except Exception:
            continue
    return items


def analyze_news_items(raw_items, name2code: dict):
    """规则引擎: 提取标题/情绪/政策属性/行业/直接提及个股"""
    seen, out = set(), []
    for it in raw_items:
        text = str(it.get("rich_text") or "").strip()
        if not text:
            continue
        key = text[:40]
        if key in seen:
            continue
        seen.add(key)
        tt = text[:220]
        m = re.match(r"【(.+?)】", text)
        title = m.group(1) if m else text[:26]
        policy = any(w in tt for w in POLICY_SRC)
        pos = sum(1 for w in POS_WORDS if w in tt)
        neg = sum(1 for w in NEG_WORDS if w in tt)
        sentiment = 1 if pos > neg else (-1 if neg > pos else 0)
        industries = []
        for name in fetch_industry_list():
            base = name.replace("行业", "")
            if base in tt or name in tt or any(k in tt for k in INDUSTRY_KEYWORDS.get(name, [])):
                industries.append(name)
        stocks = []
        for n, c in name2code.items():
            if n in tt:
                stocks.append({"c": c, "n": n})
                if len(stocks) >= 8:
                    break
        if not (policy or industries or stocks or sentiment != 0):
            continue
        out.append({
            "id": it.get("item_id") or f"{tm_hash(text)}",
            "time": str(it.get("create_time", ""))[5:16],
            "title": title,
            "text": text[:110] + ("…" if len(text) > 110 else ""),
            "sent": sentiment, "policy": policy,
            "industries": industries, "stocks": stocks,
        })
    out.sort(key=lambda x: x["time"], reverse=True)
    return out[:60]


def tm_hash(text: str) -> str:
    import hashlib
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:10]


def build_news_signal():
    data, _ = all_cache.get()
    if data is None:
        return {"ready": False}
    name2code = {r["n"]: r["c"] for r in data["rows"]
                 if r["n"] and "ST" not in r["n"] and "退" not in r["n"]}
    items = analyze_news_items(fetch_zhibo_news(), name2code)
    hy = fetch_industry_list()
    impact = {}
    ind_summary = {}
    market_wide = []
    hit_industries = set()
    for it in items:
        hit_industries.update(it["industries"])
    fetched = 0
    for it in items:
        w = 2 if it["policy"] else 1
        if it["policy"] and not it["industries"] and not it["stocks"]:
            market_wide.append({"time": it["time"], "title": it["title"], "sent": it["sent"]})
        for nm in it["industries"]:
            s = ind_summary.setdefault(nm, {"score": 0, "count": 0})
            s["score"] += w * it["sent"]
            s["count"] += 1
            if fetched >= 10:
                continue
            node = hy.get(nm)
            if not node:
                continue
            codes = fetch_node_members(node)
            if not codes:
                continue
            fetched += 1
            for c in codes:
                imp = impact.setdefault(c, {"s": 0, "n": 0, "tags": set(), "pol": False})
                imp["s"] += w * it["sent"]
                imp["n"] += 1
                imp["tags"].add(nm)
                imp["pol"] = imp["pol"] or it["policy"]
        for st in it["stocks"]:
            imp = impact.setdefault(st["c"], {"s": 0, "n": 0, "tags": set(), "pol": False})
            imp["s"] += w * it["sent"]
            imp["n"] += 1
            imp["pol"] = imp["pol"] or it["policy"]
    stock_impact = {c: {"s": v["s"], "n": v["n"], "tags": sorted(v["tags"]), "pol": v["pol"]}
                    for c, v in impact.items()}
    industries = sorted(
        ({"name": nm, "score": v["score"], "count": v["count"]} for nm, v in ind_summary.items()),
        key=lambda x: (abs(x["score"]), x["count"]), reverse=True)[:15]
    return {
        "ready": True,
        "updated": now_cst().strftime("%H:%M:%S"),
        "total": len(items),
        "news": items[:40],
        "industries": industries,
        "market_wide": market_wide[:5],
        "stock_impact": stock_impact,
    }


def get_news_signal(force=False):
    data, ts = news_cache.get()
    ttl = 240
    if force or data is None or time.time() - ts > ttl:
        if not signal_busy.acquire(blocking=False):
            return data or {"ready": False}
        try:
            sig = build_news_signal()
            if sig.get("ready"):
                news_cache.set(sig)
                data = sig
        finally:
            signal_busy.release()
    return data or {"ready": False}


# ---------------- 东财数据中心: 龙虎榜 / 北向资金 ----------------

EM_DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
lhb_cache = Cache()
hsgt_cache = Cache()


def dc_get(params: dict):
    r = session.get(EM_DC_URL, params=params, timeout=12,
                    headers={"Referer": "https://data.eastmoney.com/"})
    r.raise_for_status()
    j = r.json()
    if not j.get("success") or not j.get("result"):
        raise RuntimeError(j.get("message", "datacenter error"))
    return j["result"]


def latest_trade_date():
    try:
        bars, _ = fetch_kline_with_fallback("sh000001", "day", 5, "")
        if bars:
            return str(bars[-1][0])[:10]
    except Exception:
        pass
    return None


def fetch_lhb(date: str = None):
    """龙虎榜: 最近交易日个股榜(按龙虎榜净买额排序)"""
    data, ts = lhb_cache.get()
    if data and time.time() - ts < 900:
        return data
    d = date or latest_trade_date()
    if not d:
        raise RuntimeError("无法确定最近交易日")
    res = dc_get({
        "reportName": "RPT_DAILYBILLBOARD_DETAILS", "columns": "ALL",
        "pageSize": 60, "pageNumber": 1,
        "filter": f"(TRADE_DATE='{d}')",
        "sortColumns": "BILLBOARD_NET_AMT", "sortTypes": "-1",
    })
    rows = []
    for r in res.get("data", []):
        rows.append({
            "c": r.get("SECURITY_CODE"), "n": r.get("SECURITY_NAME_ABBR"),
            "reason": r.get("EXPLANATION") or "",
            "explain": r.get("EXPLAIN") or "",
            "close": _f(r.get("CLOSE_PRICE")), "chg": _f(r.get("CHANGE_RATE")),
            "net": _f(r.get("BILLBOARD_NET_AMT")),
            "buy": _f(r.get("BILLBOARD_BUY_AMT")),
            "sell": _f(r.get("BILLBOARD_SELL_AMT")),
            "turn": _f(r.get("TURNOVERRATE")),
        })
    data = {"date": d, "rows": rows}
    lhb_cache.set(data)
    return data


def fetch_hsgt(days: int = 90):
    """北向资金(沪股通001+深股通003): 成交额/成交笔数/领涨股; 净买入已停止披露"""
    data, ts = hsgt_cache.get()
    if data and time.time() - ts < 900 and data.get("days") == days:
        return data
    by_date = {}
    for mt in ("001", "003"):
        res = dc_get({
            "reportName": "RPT_MUTUAL_DEAL_HISTORY", "columns": "ALL",
            # 同一交易日同通道存在多条盘中披露记录(每日~6条), pageSize 需放大
            "pageSize": min(max(days * 12, 120), 500), "pageNumber": 1,
            "sortColumns": "TRADE_DATE", "sortTypes": "-1",
            "filter": f"(MUTUAL_TYPE='{mt}')",
        })
        # (日期, 通道) -> 当日全天口径记录: 同日多条时取成交额最大一条
        best = {}
        for r in res.get("data", []):
            d = str(r.get("TRADE_DATE", ""))[:10]
            amt = _f(r.get("DEAL_AMT")) or 0
            old = best.get(d)
            if old is None or amt > old[0]:
                best[d] = (amt, _f(r.get("DEAL_NUM")) or 0,
                           r.get("LEAD_STOCKS_NAME"), _f(r.get("LS_CHANGE_RATE")))
        for d, (amt, num, lsn, lsc) in best.items():
            e = by_date.setdefault(d, {"date": d, "amt": 0.0, "num": 0, "leads": []})
            e["amt"] += amt
            e["num"] += num
            if lsn:
                e["leads"].append(f"{lsn}({lsc}%)")
    rows = sorted(by_date.values(), key=lambda x: x["date"])
    for e in rows:
        e["amt"] = round(e["amt"] / 100, 2)  # 百万元 -> 亿元
        # 领涨股按名称去重(沪深通道常见同一领涨股)
        seen, dedup = set(), []
        for s in e["leads"]:
            k = s.split("(")[0]
            if k not in seen:
                seen.add(k)
                dedup.append(s)
        e["leads"] = dedup[:2]
    data = {"days": days, "rows": rows[-days:],
            "note": "按披露规则, 北向资金自2024年8月起不再披露当日净买入额, 此处展示成交金额与成交笔数"}
    hsgt_cache.set(data)
    return data


# ---------------- 资金流向 ----------------

SINA_FLOW_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs"
flow_rank_cache = Cache()
flow_stock_store = {}


def fetch_flow_stock(code: str):
    """个股近60日主力资金流(新浪)"""
    code = norm_code(code)
    cache = get_cache(flow_stock_store, code)
    data, ts = cache.get()
    if data and time.time() - ts < 600:
        return data
    r = session.get(SINA_FLOW_URL, params={"page": 1, "num": 60, "sort": "opendate",
                                           "asc": 0, "daima": code}, timeout=10)
    rows = r.json()
    out = []
    for x in rows:
        out.append({
            "date": x.get("opendate"),
            "net": _f(x.get("netamount")),
            "r0": _f(x.get("r0_net")),
            "ratio": _f(x.get("ratioamount")),
            "chg": round((_f(x.get("changeratio")) or 0) * 100, 2),
            "close": _f(x.get("trade")),
        })
    data = {"code": code, "rows": out}
    if out:
        cache.set(data)
    return data


def fetch_flow_rank():
    """主力净流入排行: 按成交额TOP120采样(新浪逐股查询)"""
    data, ts = flow_rank_cache.get()
    if data and time.time() - ts < 300:
        return data
    snap, _ = all_cache.get()
    if not snap:
        return {"ready": False}
    top = sorted(snap["rows"], key=lambda r: (r["a"] or 0), reverse=True)[:120]

    def work(item):
        c, full = item
        try:
            r = session.get(SINA_FLOW_URL, params={"page": 1, "num": 1, "sort": "opendate",
                                                   "asc": 0, "daima": full}, timeout=8)
            j = r.json()
            if isinstance(j, list) and j:
                x = j[0]
                return {"c": c, "net": _f(x.get("netamount")), "r0": _f(x.get("r0_net")),
                        "ratio": _f(x.get("ratioamount")), "date": x.get("opendate")}
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = [x for x in ex.map(work, [(r["c"], norm_code(r["c"])) for r in top]) if x]
    by_code = {r["c"]: r for r in snap["rows"]}
    for r in results:
        base = by_code.get(r["c"], {})
        r["n"] = base.get("n")
        r["p"] = base.get("p")
        r["chg"] = base.get("chg")
        r["a"] = base.get("a")
    results = [r for r in results if r.get("net") is not None]
    results.sort(key=lambda x: x["net"], reverse=True)
    data = {"ready": True, "date": results[0]["date"] if results else "",
            "sample": len(results), "rows": results}
    flow_rank_cache.set(data)
    return data


# ---------------- 资讯·政策归档 ----------------

SINA_ROLL_URL = "https://feed.mix.sina.com.cn/api/roll/get"
news_archive_store = {}


def fetch_roll_pages(max_pages: int = 45):
    """新浪财经即时流(lid=2516), 免费归档约8天"""
    items = []
    for p in range(1, max_pages + 1):
        try:
            r = session.get(SINA_ROLL_URL, params={"pageid": 153, "lid": 2516,
                                                   "num": 50, "page": p}, timeout=10)
            data = (r.json().get("result") or {}).get("data") or []
            if not data:
                break
            items.extend(data)
        except Exception:
            break
    return items


def analyze_roll_items(raw_items, name2code: dict):
    seen, out = set(), []
    for it in raw_items:
        title = str(it.get("title") or "").strip()
        intro = str(it.get("intro") or "").strip()
        if not title:
            continue
        key = title[:30]
        if key in seen:
            continue
        seen.add(key)
        try:
            ts = int(it.get("ctime"))
            dt = datetime.fromtimestamp(ts, CST)
        except Exception:
            continue
        text = (title + " " + intro)[:240]
        policy = any(w in text for w in POLICY_SRC)
        pos = sum(1 for w in POS_WORDS if w in text)
        neg = sum(1 for w in NEG_WORDS if w in text)
        sent = 1 if pos > neg else (-1 if neg > pos else 0)
        industries = []
        for name in fetch_industry_list():
            base = name.replace("行业", "")
            if base in text or name in text or any(k in text for k in INDUSTRY_KEYWORDS.get(name, [])):
                industries.append(name)
        stocks = []
        for n, c in name2code.items():
            if n in text:
                stocks.append({"c": c, "n": n})
                if len(stocks) >= 6:
                    break
        word = "利好" if sent > 0 else ("利空" if sent < 0 else "中性")
        parts = [f"规则判定：{word}（正面词{pos}个/负面词{neg}个）"]
        if policy:
            parts.append("涉及官方部门或政策表述")
        if industries:
            parts.append("涉及行业：" + "、".join(i.replace("行业", "") for i in industries[:4]))
        if stocks:
            parts.append("直接关联个股：" + "、".join(x["n"] for x in stocks[:5]))
        if not stocks and industries:
            parts.append("可在条件选股中按行业标签查看板块成分股")
        out.append({
            "time": dt.strftime("%m-%d %H:%M"), "date": dt.strftime("%Y-%m-%d"),
            "title": title[:60], "text": intro[:90] if intro else title[:60],
            "sent": sent, "policy": policy, "industries": industries, "stocks": stocks,
            "interp": "；".join(parts),
        })
    out.sort(key=lambda x: (x["date"], x["time"]), reverse=True)
    return out


def build_news_archive(range_key: str):
    snap, _ = all_cache.get()
    if not snap:
        return {"ready": False}
    name2code = {r["n"]: r["c"] for r in snap["rows"]
                 if r["n"] and "ST" not in r["n"] and "退" not in r["n"]}
    if range_key == "day":
        raw = fetch_zhibo_news()
        conv = []
        for it in raw:
            conv.append({"title": str(it.get("rich_text", ""))[:60].replace("【", "").replace("】", ""),
                         "intro": it.get("rich_text", ""), "ctime": it.get("create_time", "")})
        # 7x24 create_time 非时间戳, 统一走 analyze_roll 的规则但时间解析不同
        items = analyze_news_items_full(conv, name2code)
    else:
        raw = fetch_roll_pages(45)
        items = analyze_roll_items(raw, name2code)
        if range_key == "week":
            pass
        else:
            # 近一月/近一年: 只保留有信号(政策/行业/个股/情绪)的条目
            items = [x for x in items if x["policy"] or x["industries"] or x["stocks"] or x["sent"] != 0]
    items = items[:500]
    coverage_from = items[-1]["date"] if items else ""
    extras = {}
    if range_key in ("month", "year"):
        try:
            period = "month" if range_key == "year" else "day"
            lmt = 13 if range_key == "year" else 30
            bars, _ = fetch_kline_with_fallback("sh000001", period, lmt, "")
            idx = [{"date": b[0][:7] if period == "month" else b[0][:10],
                    "open": _f(b[1]), "close": _f(b[2]), "high": _f(b[3]), "low": _f(b[4])}
                   for b in bars]
            for i, x in enumerate(idx):
                prev = idx[i - 1]["close"] if i > 0 else x["open"]
                x["chg"] = round((x["close"] - prev) / prev * 100, 2) if prev else None
            extras["index"] = idx
        except Exception:
            extras["index"] = []
        try:
            hg = fetch_hsgt(250 if range_key == "year" else 30)
            if range_key == "year":
                m = {}
                for e in hg["rows"]:
                    k = e["date"][:7]
                    v = m.setdefault(k, {"month": k, "amt": 0.0, "days": 0})
                    v["amt"] = round(v["amt"] + e["amt"], 2)
                    v["days"] += 1
                extras["hsgt"] = sorted(m.values(), key=lambda x: x["month"])
            else:
                extras["hsgt"] = hg["rows"][-30:]
        except Exception:
            extras["hsgt"] = []
    notes = {
        "day": "来源：新浪7x24快讯（实时）",
        "week": "来源：新浪财经即时流，免费归档约8天，已尽力回溯",
        "month": "逐条归档仅覆盖近8天（免费源上限），此处仅展示有信号的资讯；另附近30个交易日行情与北向数据",
        "year": "逐条资讯无法免费获取全年数据，此处展示近12个月月度行情、北向月度活跃度与近期政策信号；不编造历史资讯",
    }
    return {"ready": True, "range": range_key, "items": items,
            "coverage": {"from": coverage_from, "to": items[0]["date"] if items else ""},
            **extras, "note": notes.get(range_key, "")}


def analyze_news_items_full(conv_items, name2code):
    """7x24快讯分析(create_time为字符串时间)"""
    seen, out = set(), []
    for it in conv_items:
        text = str(it.get("intro") or "").strip()
        title = str(it.get("title") or "").strip()
        if not text:
            continue
        key = text[:40]
        if key in seen:
            continue
        seen.add(key)
        tstr = str(it.get("ctime", ""))
        tt = text[:220]
        m = re.match(r"【(.+?)】", text)
        if not m and title:
            title = title[:30]
        policy = any(w in tt for w in POLICY_SRC)
        pos = sum(1 for w in POS_WORDS if w in tt)
        neg = sum(1 for w in NEG_WORDS if w in tt)
        sent = 1 if pos > neg else (-1 if neg > pos else 0)
        industries = []
        for name in fetch_industry_list():
            base = name.replace("行业", "")
            if base in tt or name in tt or any(k in tt for k in INDUSTRY_KEYWORDS.get(name, [])):
                industries.append(name)
        stocks = []
        for n, c in name2code.items():
            if n in tt:
                stocks.append({"c": c, "n": n})
                if len(stocks) >= 6:
                    break
        if not (policy or industries or stocks or sent != 0):
            continue
        word = "利好" if sent > 0 else ("利空" if sent < 0 else "中性")
        parts = [f"规则判定：{word}（正面词{pos}个/负面词{neg}个）"]
        if policy:
            parts.append("涉及官方部门或政策表述")
        if industries:
            parts.append("涉及行业：" + "、".join(i.replace("行业", "") for i in industries[:4]))
        if stocks:
            parts.append("直接关联个股：" + "、".join(x["n"] for x in stocks[:5]))
        out.append({
            "time": tstr[5:16], "date": tstr[:10],
            "title": (m.group(1) if m else title)[:60],
            "text": text[:110] + ("…" if len(text) > 110 else ""),
            "sent": sent, "policy": policy, "industries": industries, "stocks": stocks,
            "interp": "；".join(parts),
        })
    out.sort(key=lambda x: (x["date"], x["time"]), reverse=True)
    return out


def get_news_archive(range_key: str, force: bool = False):
    ttl = {"day": 300, "week": 1800, "month": 1800, "year": 3600}.get(range_key, 600)
    cache = get_cache(news_archive_store, range_key)
    data, ts = cache.get()
    if force or data is None or time.time() - ts > ttl:
        try:
            sig = build_news_archive(range_key)
            if sig.get("ready"):
                cache.set(sig)
                data = sig
        except Exception:
            pass
    return data


# ---------------- 策略回测 ----------------

def _sma(vals, n):
    out = []
    for i in range(len(vals)):
        if i < n - 1:
            out.append(None)
        else:
            out.append(sum(vals[i - n + 1:i + 1]) / n)
    return out


def _ema(vals, n):
    out, k = [], 2 / (n + 1)
    e = None
    for i, v in enumerate(vals):
        e = v if e is None else v * k + e * (1 - k)
        out.append(e if i >= n - 1 else None)
    return out


def _macd(vals):
    e12, e26 = _ema(vals, 12), _ema(vals, 26)
    dif = [None if a is None or b is None else a - b for a, b in zip(e12, e26)]
    valid = [v for v in dif if v is not None]
    dea_valid = _ema(valid, 9)
    dea = [None] * (len(dif) - len(dea_valid)) + dea_valid
    return dif, dea


def _rsi(vals, n=14):
    out = [None] * len(vals)
    gains = losses = 0.0
    for i in range(1, len(vals)):
        ch = vals[i] - vals[i - 1]
        g, l = max(ch, 0), max(-ch, 0)
        if i <= n:
            gains += g
            losses += l
            if i == n:
                ag, al = gains / n, losses / n
                out[i] = 100 - 100 / (1 + (ag / al if al else 999))
        else:
            ag = (ag * (n - 1) + g) / n
            al = (al * (n - 1) + l) / n
            out[i] = 100 - 100 / (1 + (ag / al if al else 999))
    return out


STRATEGY_META = {
    "ma_cross": {"name": "双均线择时", "params": "快线/慢线"},
    "macd": {"name": "MACD金叉死叉", "params": "12/26/9固定"},
    "rsi": {"name": "RSI超卖买超买卖", "params": "超卖/超买阈值"},
    "breakout": {"name": "唐奇安突破", "params": "突破窗口/跌破窗口"},
}


def run_backtest(bars, strategy, fast, slow, n1, n2, rsi_low, rsi_high):
    closes = [b["close"] or 0 for b in bars]
    opens = [b["open"] or 0 for b in bars]
    highs = [b["high"] or 0 for b in bars]
    lows = [b["low"] or 0 for b in bars]
    dates = [b["date"] for b in bars]
    n = len(bars)
    sig = [0] * n
    start = 30
    if strategy == "ma_cross":
        fa, sa = _sma(closes, max(2, fast)), _sma(closes, max(3, slow))
        start = max(fast, slow) + 1
        for i in range(start, n):
            if fa[i] and sa[i]:
                sig[i] = 1 if fa[i] > sa[i] else 0
    elif strategy == "macd":
        dif, dea = _macd(closes)
        start = 35
        for i in range(start, n):
            if dif[i] is not None and dea[i] is not None:
                sig[i] = 1 if dif[i] > dea[i] else 0
    elif strategy == "rsi":
        r = _rsi(closes)
        state = 0
        for i in range(start, n):
            if r[i] is None:
                sig[i] = state
                continue
            if state == 0 and r[i] < rsi_low:
                state = 1
            elif state == 1 and r[i] > rsi_high:
                state = 0
            sig[i] = state
    elif strategy == "breakout":
        state = 0
        w1, w2 = max(5, n1), max(3, n2)
        start = max(w1, w2) + 1
        for i in range(start, n):
            hh = max(highs[i - w1:i]) if i >= w1 else 0
            ll = min(lows[i - w2:i]) if i >= w2 else closes[i]
            if state == 0 and closes[i] > hh:
                state = 1
            elif state == 1 and closes[i] < ll:
                state = 0
            sig[i] = state
    fee = 0.0012
    cash, pos, pend = 100000.0, 0.0, None
    in_date = in_px = None
    equity, trades = [], []
    hold_bars = 0
    for i in range(n):
        if pend == "buy" and pos == 0 and opens[i] > 0:
            pos = cash * (1 - fee) / opens[i]
            cash = 0.0
            in_date, in_px = dates[i], opens[i]
        elif pend == "sell" and pos > 0 and opens[i] > 0:
            cash = pos * opens[i] * (1 - fee)
            trades.append({"in_date": in_date, "in_px": round(in_px, 2),
                           "out_date": dates[i], "out_px": round(opens[i], 2),
                           "ret": round((opens[i] * (1 - fee)) / (in_px * (1 - fee)) * 100 - 100, 2)})
            pos = 0.0
        pend = None
        if i < n - 1:
            if sig[i] == 1 and pos == 0:
                pend = "buy"
            elif sig[i] == 0 and pos > 0:
                pend = "sell"
        if pos > 0:
            hold_bars += 1
        equity.append({"date": dates[i], "v": round(cash + pos * closes[i], 2), "sig": sig[i],
                       "b": round(closes[i] / closes[start] * 100, 2) if closes[start] else 100.0})
    if pos > 0:
        cash = pos * closes[-1] * (1 - fee)
        trades.append({"in_date": in_date, "in_px": round(in_px, 2),
                       "out_date": dates[-1] + "(期末持有)", "out_px": round(closes[-1], 2),
                       "ret": round((closes[-1] * (1 - fee)) / (in_px * (1 - fee)) * 100 - 100, 2)})
        pos = 0.0
    initial = 100000.0
    final = equity[-1]["v"] if equity else initial
    total_ret = final / initial * 100 - 100
    annual = ((final / initial) ** (252 / max(n - start, 1)) - 1) * 100 if final > 0 else -100
    peak, maxdd = initial, 0.0
    for e in equity:
        peak = max(peak, e["v"])
        maxdd = min(maxdd, (e["v"] / peak - 1) * 100)
    wins = sum(1 for t in trades if t["ret"] > 0)
    bench = closes[-1] / closes[start] * 100 - 100
    return {
        "code": bars[0].get("code", ""), "strategy": strategy,
        "strategy_name": STRATEGY_META.get(strategy, {}).get("name", strategy),
        "days": n - start, "from": dates[start], "to": dates[-1],
        "total_ret": round(total_ret, 2), "annual": round(annual, 2),
        "maxdd": round(maxdd, 2), "bench": round(bench, 2),
        "trades_n": len(trades),
        "win_rate": round(wins / len(trades) * 100, 1) if trades else None,
        "exposure": round(hold_bars / max(n - start, 1) * 100, 1),
        "final": round(final, 0),
        "equity": equity[start:], "trades": trades[-30:],
        "note": "收盘出信号、次日开盘价成交，单边费率0.12%；历史回测不代表未来收益",
    }


# ---------------- 自选股(JSON 文件持久化) ----------------

def _primary_watch_file() -> Path:
    """自选股文件位置: 打包成 EXE 后放 exe 同级目录(便于备份/查看), 源码运行放项目目录"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "watchlist.json"
    return BASE / "watchlist.json"


def _fallback_watch_file() -> Path:
    """首选目录不可写时(例如 exe 放在 Program Files)回退到用户目录"""
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return root / "StockDashboard" / "watchlist.json"


WATCH_FILE = _primary_watch_file()
WATCH_LOCK = threading.Lock()
CODE_RE = re.compile(r"^\d{6}$")


def clean_codes(value) -> list:
    """清洗代码列表: 只保留 6 位数字代码, 去重并保持原顺序"""
    if isinstance(value, dict):
        value = value.get("codes")
    if not isinstance(value, list):
        return []
    out, seen = [], set()
    for x in value:
        c = str(x).strip().upper()
        if CODE_RE.match(c) and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def read_watchlist() -> dict:
    """读取自选股文件, 返回 {"codes": [...], "exists": 文件是否存在}"""
    with WATCH_LOCK:
        try:
            raw = WATCH_FILE.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"codes": [], "exists": False}
        except OSError:
            return {"codes": [], "exists": True}
    try:
        return {"codes": clean_codes(json.loads(raw)), "exists": True}
    except (ValueError, TypeError):
        return {"codes": [], "exists": True}      # 文件损坏: 当作空列表, 不阻塞页面


def write_watchlist(codes) -> list:
    """原子写入自选股文件(先写临时文件再替换, 避免写一半损坏), 返回保存后的代码列表"""
    global WATCH_FILE
    codes = clean_codes(codes)
    payload = {"codes": codes, "count": len(codes),
               "updated": now_cst().strftime("%Y-%m-%d %H:%M:%S")}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    with WATCH_LOCK:
        for target in (WATCH_FILE, _fallback_watch_file()):
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".tmp")
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(target)
                WATCH_FILE = target              # 记住实际可写的位置
                return codes
            except OSError:
                continue
    raise OSError(f"自选股文件写入失败: {WATCH_FILE}")


# ---------------- FastAPI ----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    t = threading.Thread(target=all_refresher, daemon=True)
    t.start()
    yield


app = FastAPI(title="股票盯盘", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/watchlist")
def api_watchlist_get():
    """读取自选股列表(保存在 watchlist.json)"""
    d = read_watchlist()
    return {"codes": d["codes"], "exists": d["exists"], "file": str(WATCH_FILE)}


@app.post("/api/watchlist")
def api_watchlist_save(payload: dict | None = Body(default=None)):
    """覆盖保存自选股列表, 请求体 {"codes": ["600519", ...]}"""
    codes = (payload or {}).get("codes", [])
    if not isinstance(codes, list):
        return JSONResponse({"ok": False, "error": "codes 必须是字符串数组"}, status_code=400)
    try:
        saved = write_watchlist(codes)
    except OSError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "codes": saved, "count": len(saved), "file": str(WATCH_FILE)}


@app.get("/api/health")
def health():
    phase, fast = market_phase()
    data, ts = all_cache.get()
    return {"ok": True, "phase": phase, "all_ready": data is not None,
            "all_count": data["count"] if data else 0}


@app.get("/api/summary")
def api_summary():
    phase, fast = market_phase()
    ttl = 10 if fast else 120
    data, ts = summary_cache.get()
    if data is None or time.time() - ts > ttl:
        try:
            data = fetch_summary()
            summary_cache.set(data)
        except Exception:
            if data is None:
                return JSONResponse({"error": "summary fetch failed"}, status_code=502)
    return {"indices": data, "phase": phase, "server_time": now_cst().strftime("%H:%M:%S")}


@app.get("/api/all")
def api_all():
    data, ts = all_cache.get()
    if data is None:
        try:
            snap = build_all_snapshot()
            if snap["count"] > 0:
                all_cache.set(snap)
                data, ts = snap, time.time()
        except Exception:
            pass
    if data is None:
        return JSONResponse({"ready": False, "count": 0, "rows": []}, status_code=202)
    phase, _ = market_phase()
    return {"ready": True, "count": data["count"],
            "updated": datetime.fromtimestamp(ts, CST).strftime("%H:%M:%S"),
            "phase": phase, "rows": data["rows"]}


@app.get("/api/quotes")
def api_quotes(codes: str = Query(..., description="逗号分隔的6位股票代码")):
    data, _ = all_cache.get()
    if data is None:
        return JSONResponse({"ready": False, "rows": []}, status_code=202)
    want = [c.strip() for c in codes.split(",") if c.strip()]
    rows = [data["by_code"][c] for c in want if c in data["by_code"]]
    return {"ready": True, "rows": rows, "phase": market_phase()[0]}


@app.get("/api/kline")
def api_kline(code: str, period: str = "day", lmt: int = 250):
    code = norm_code(code)
    period = period if period in ("day", "week", "month") else "day"
    lmt = max(10, min(lmt, 800))
    is_index = code.startswith(("sh000", "sz399"))
    fq = "" if is_index else "qfq"
    cache = get_cache(kline_cache, (code, period, lmt))
    ttl = 60 if market_phase()[1] else 1200
    data, ts = cache.get()
    if data is None or time.time() - ts > ttl:
        try:
            bars, qt = fetch_kline_with_fallback(code, period, lmt, fq)
            parsed = []
            for b in bars:
                if len(b) >= 6:
                    parsed.append({"date": b[0], "open": _f(b[1]), "close": _f(b[2]),
                                   "high": _f(b[3]), "low": _f(b[4]), "volume": _f(b[5])})
            data = {"code": code, "period": period, "bars": parsed,
                    "qt": parse_qt(qt) if qt else {}}
            cache.set(data)
        except Exception:
            if data is None:
                return JSONResponse({"error": "kline fetch failed"}, status_code=502)
    return data


@app.get("/api/minute")
def api_minute(code: str):
    code = norm_code(code)
    cache = get_cache(minute_cache, code)
    ttl = 12 if market_phase()[1] else 600
    data, ts = cache.get()
    if data is None or time.time() - ts > ttl:
        try:
            data = fetch_minute(code)
            cache.set(data)
        except Exception:
            if data is None:
                return JSONResponse({"error": "minute fetch failed"}, status_code=502)
    return data


@app.get("/api/news_signal")
def api_news_signal(force: int = 0):
    """资讯/政策信号: 快讯情绪 + 行业映射 + 个股影响"""
    sig = get_news_signal(force=bool(force))
    if not sig.get("ready"):
        return JSONResponse({"ready": False, "msg": "资讯信号构建中，首次约需10秒"}, status_code=202)
    return sig


@app.get("/api/news_archive")
def api_news_archive(range: str = "week", force: int = 0):
    """资讯·政策归档: day/week/month/year"""
    range = range if range in ("day", "week", "month", "year") else "week"
    data = get_news_archive(range, force=bool(force))
    if not data or not data.get("ready"):
        return JSONResponse({"ready": False, "msg": "归档构建中，首次约需10秒"}, status_code=202)
    return data


@app.get("/api/lhb")
def api_lhb(date: str = ""):
    """龙虎榜"""
    try:
        return fetch_lhb(date or None)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/hsgt")
def api_hsgt(days: int = 90):
    """北向资金(成交额口径)"""
    try:
        return fetch_hsgt(max(20, min(days, 250)))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/flow_stock")
def api_flow_stock(code: str):
    """个股资金流"""
    try:
        data = fetch_flow_stock(code)
        if not data["rows"]:
            return JSONResponse({"error": "无资金流数据"}, status_code=404)
        return data
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/flow_rank")
def api_flow_rank():
    """主力净流入排行(成交额TOP120采样)"""
    data = fetch_flow_rank()
    if not data.get("ready"):
        return JSONResponse({"ready": False}, status_code=202)
    return data


@app.get("/api/backtest")
def api_backtest(code: str, strategy: str = "ma_cross", fast: int = 5, slow: int = 20,
                 n1: int = 20, n2: int = 10, rsi_low: int = 30, rsi_high: int = 70,
                 lmt: int = 500):
    """策略回测"""
    strategy = strategy if strategy in STRATEGY_META else "ma_cross"
    try:
        code = norm_code(code)
        bars_raw, _ = fetch_kline_with_fallback(code, "day", max(120, min(lmt, 800)), "qfq")
        bars = [{"date": b[0], "open": _f(b[1]), "close": _f(b[2]),
                 "high": _f(b[3]), "low": _f(b[4])} for b in bars_raw if len(b) >= 6]
        if len(bars) < 80:
            return JSONResponse({"error": f"K线数据不足({len(bars)}根)"}, status_code=400)
        res = run_backtest(bars, strategy, fast, slow, n1, n2, rsi_low, rsi_high)
        res["code"] = code
        return res
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


# ---------------- finance_toolkit: 一键回测 / 策略引擎 / 日报 ----------------

def ft_bars(code: str, lmt: int = 500) -> dict:
    """取日K(复用 /api/kline 同一缓存)并转成 finance_toolkit 所需的列式 dict"""
    lmt = max(120, min(int(lmt or 500), 800))
    is_index = code.startswith(("sh000", "sz399"))
    fq = "" if is_index else "qfq"
    ttl = 60 if market_phase()[1] else 1200
    cache = get_cache(kline_cache, (code, "day", lmt))
    data, ts = cache.get()
    if data is None or time.time() - ts > ttl:
        bars_raw, _ = fetch_kline_with_fallback(code, "day", lmt, fq)
        parsed = []
        for b in bars_raw:
            if len(b) >= 6:
                parsed.append({"date": str(b[0])[:10], "open": _f(b[1]), "close": _f(b[2]),
                               "high": _f(b[3]), "low": _f(b[4]), "volume": _f(b[5])})
        data = {"bars": parsed}
        cache.set(data)
    out = {"date": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
    for b in data["bars"]:
        if None in (b["open"], b["close"], b["high"], b["low"]):
            continue
        out["date"].append(b["date"])
        out["open"].append(b["open"])
        out["high"].append(b["high"])
        out["low"].append(b["low"])
        out["close"].append(b["close"])
        out["volume"].append(b["volume"] or 0.0)
    return out


def ft_stock_name(code6: str) -> str:
    data, _ = all_cache.get()
    if data and code6 in data["by_code"]:
        return data["by_code"][code6].get("n") or ""
    return ""


@app.get("/api/ft_strategies")
def api_ft_strategies():
    """策略库元数据(10个策略: key/name/group/desc/params)"""
    return {"strategies": ft.STRATEGIES, "note": ft.STRATEGY_NOTE}


@app.get("/api/ft_backtest")
def api_ft_backtest(code: str, strategy: str = "ma5_20", lmt: int = 500):
    """一键回测: 单策略完整结果(权益曲线/交易明细/绩效指标)"""
    strategy = strategy if strategy in ft.STRATEGY_MAP else "ma5_20"
    try:
        code = norm_code(code)
        d = ft_bars(code, lmt)
        if len(d["close"]) < 80:
            return JSONResponse({"error": f"K线数据不足({len(d['close'])}根)"}, status_code=400)
        res = ft.run_one(d, strategy)
        res["code"] = code
        res["stock_name"] = ft_stock_name(code[2:])
        res["bars_total"] = len(d["close"])
        res["note"] = ft.STRATEGY_NOTE
        return res
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/ft_compare")
def api_ft_compare(code: str, lmt: int = 500):
    """一键回测: 10策略横评(按夏普排序, 附最优与中位数)"""
    try:
        code = norm_code(code)
        d = ft_bars(code, lmt)
        if len(d["close"]) < 80:
            return JSONResponse({"error": f"K线数据不足({len(d['close'])}根)"}, status_code=400)
        res = ft.run_all(d)
        res["code"] = code
        res["stock_name"] = ft_stock_name(code[2:])
        res["bars_total"] = len(d["close"])
        res["note"] = ft.STRATEGY_NOTE
        return res
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/ft_scan")
def api_ft_scan(code: str, strategy: str = "ma5_20", lmt: int = 500):
    """一键回测: 参数网格搜索(敏感性分析)"""
    strategy = strategy if strategy in ft.STRATEGY_MAP else "ma5_20"
    try:
        code = norm_code(code)
        d = ft_bars(code, lmt)
        if len(d["close"]) < 80:
            return JSONResponse({"error": f"K线数据不足({len(d['close'])}根)"}, status_code=400)
        res = ft.scan(d, strategy)
        res["code"] = code
        res["stock_name"] = ft_stock_name(code[2:])
        res["note"] = ft.STRATEGY_NOTE
        return res
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/ft_engine")
def api_ft_engine(code: str, lmt: int = 300):
    """策略引擎: 实时多指标共振分析 + 60分买入建议 + 融合策略持仓状态"""
    try:
        code = norm_code(code)
        d = ft_bars(code, lmt)
        ta = ft.live_analysis(d)
        if "error" in ta:
            return JSONResponse({"error": ta["error"]}, status_code=400)
        return {"code": code, "stock_name": ft_stock_name(code[2:]),
                "as_of": d["date"][-1] if d["date"] else "",
                "analysis": ta, "suggest": ft.buy_suggest(ta),
                "fusion": ft.fusion_status(d)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


# ---------------- finance_toolkit: 日报 ----------------

ft_report_cache = Cache()
FT_REPORT_TTL = 300


def _limit_pct(c6: str) -> float:
    """涨跌停幅度(近似阈值): 创业板/科创板20%, 北交所30%, 主板10% (ST用容差处理)"""
    if c6.startswith(("30", "68")):
        return 19.5
    if c6[:1] in ("4", "8"):
        return 29.5
    return 9.7


def _ft_hot_stocks(rows: list, n: int = 10):
    """热门个股: 成交额TOP, 排除ST/退市"""
    cand = [r for r in rows
            if r.get("p") is not None and r.get("a")
            and "ST" not in (r.get("n") or "") and "退" not in (r.get("n") or "")]
    cand = sorted(cand, key=lambda r: r["a"], reverse=True)[:n]
    return [{"code": r["c"], "name": r.get("n") or "", "price": r["p"],
             "change_pct": r.get("chg") or 0, "amount": r.get("a"), "turnover": r.get("tr")}
            for r in cand]


def _ft_hot_concepts(snap, sig) -> list:
    """热点行业: 资讯信号加权TOP行业 + 成分股当日均涨幅/涨跌家数"""
    hy = fetch_industry_list()
    inds = (sig.get("industries") or [])[:6]

    def row(it):
        node = hy.get(it["name"])
        codes = fetch_node_members(node) if node else []
        base = {"name": it["name"], "chg": None, "up": 0, "down": 0,
                "score": it.get("score", 0), "count": it.get("count", 0)}
        if not codes or snap is None:
            return base
        gs = [snap["by_code"][c]["chg"] for c in codes
              if c in snap["by_code"] and snap["by_code"][c].get("chg") is not None]
        if gs:
            base["chg"] = round(sum(gs) / len(gs), 2)
            base["up"] = sum(1 for g in gs if g > 0)
            base["down"] = sum(1 for g in gs if g < 0)
        return base

    try:
        with ThreadPoolExecutor(max_workers=6) as ex:
            out = list(ex.map(row, inds))
    except Exception:
        out = []
    out.sort(key=lambda x: (x["chg"] is not None, x["chg"] or -999), reverse=True)
    return out


def _ft_watch_scores(snap, watch: list) -> list:
    """自选股 60 分评分(monitor_v3 口径), 并行取K线"""
    if not watch or snap is None:
        return []

    def one(c6):
        r = snap["by_code"].get(c6)
        if not r or r.get("p") is None:
            return None
        try:
            d = ft_bars(norm_code(c6), 120)
        except Exception:
            return None
        if len(d["close"]) < 30:
            return None
        s = ft.score_stock(d, r["p"], r.get("chg") or 0, c6, r.get("n") or "")
        return None if "error" in s else s

    with ThreadPoolExecutor(max_workers=5) as ex:
        return [x for x in ex.map(one, watch) if x]


def _ft_tech_signals(hot: list) -> list:
    """热门个股技术信号(原包 daily_report 口径: MACD多空 + RSI + 布林触轨)"""
    def one(r):
        try:
            d = ft_bars(norm_code(r["code"]), 150)
        except Exception:
            return None
        if len(d["close"]) < 30:
            return None
        parts = []
        dif, dea, _ = ft.macd(d["close"])
        if dif[-1] is not None and dea[-1] is not None:
            parts.append("MACD=" + ("📈多头" if dif[-1] > dea[-1] else "📉空头"))
        rv = ft._last(ft.rsi(d["close"], 14))
        if rv is not None:
            parts.append(f"RSI={rv:.0f}")
            if rv < 30:
                parts.append("⚡超卖")
            elif rv > 70:
                parts.append("⚠️超买")
        mid, up, low = ft.bollinger(d["close"], 20, 2)
        mv, uv, lv = ft._last(mid), ft._last(up), ft._last(low)
        if None not in (mv, uv, lv):
            px = d["close"][-1]
            if px <= lv:
                parts.append("📉触下轨")
            elif px >= uv:
                parts.append("📈触上轨")
        return {"code": r["code"], "name": r["name"], "price": r["price"],
                "signals": " | ".join(parts)}

    with ThreadPoolExecutor(max_workers=4) as ex:
        return [x for x in ex.map(one, hot) if x]


def build_ft_report() -> dict:
    """日报: 大盘指数 + 涨跌分布 + 热点行业 + 热门个股 + 自选评分 + 技术信号 + 融合策略"""
    report = {"report_time": now_cst().strftime("%Y-%m-%d %H:%M"), "note": ft.STRATEGY_NOTE}
    # 1) 大盘指数
    try:
        data, ts = summary_cache.get()
        if data is None or time.time() - ts > 60:
            data = fetch_summary()
            summary_cache.set(data)
        report["market_overview"] = [
            {"name": x["name"], "latest": x.get("price"), "change_pct": x.get("chg")}
            for x in data if x.get("price") is not None]
    except Exception:
        report["market_overview"] = []
    # 2) 涨跌分布(全市场快照; 服务刚启动时主动构建一次)
    snap, _ = all_cache.get()
    if snap is None:
        try:
            snap = build_all_snapshot()
            if snap["count"] > 0:
                all_cache.set(snap)
        except Exception:
            snap = None
    rows = snap["rows"] if snap else []
    up = down = flat = lu = ld = 0
    for r in rows:
        g = r.get("chg")
        if g is None:
            continue
        if g > 0:
            up += 1
        elif g < 0:
            down += 1
        else:
            flat += 1
        lim = _limit_pct(r["c"])
        if g >= lim:
            lu += 1
        elif g <= -lim:
            ld += 1
    report["breadth"] = {"up": up, "down": down, "flat": flat,
                         "limit_up": lu, "limit_dn": ld, "total": len(rows)}
    # 3) 热门个股(成交额TOP) + 技术信号
    hot = _ft_hot_stocks(rows, 10)
    report["hot_stocks"] = hot
    report["strategy_signals"] = _ft_tech_signals(hot[:8])
    # 4) 热点行业(资讯信号口径 + 成分均涨幅)
    try:
        sig = get_news_signal()
        report["hot_concepts"] = _ft_hot_concepts(snap, sig) if sig.get("ready") else []
    except Exception:
        report["hot_concepts"] = []
    # 5) 自选股 60 分评分
    watch = read_watchlist().get("codes") or []
    report["watch_scores"] = _ft_watch_scores(snap, watch)
    # 6) 融合策略状态(自选第一只, 否则热门第一只)
    report["fusion"] = []
    focus = watch[0] if watch else (hot[0]["code"] if hot else None)
    if focus:
        try:
            report["fusion"] = ft.fusion_status(ft_bars(norm_code(focus), 400))
            report["fusion_code"] = focus
        except Exception:
            report["fusion"] = []
    return report


@app.get("/api/ft_report")
def api_ft_report(force: int = 0):
    """日报生成(结果缓存5分钟, force=1 强制刷新)"""
    data, ts = ft_report_cache.get()
    if force or data is None or time.time() - ts > FT_REPORT_TTL:
        try:
            data = build_ft_report()
            ft_report_cache.set(data)
        except Exception as e:
            if data is None:
                return JSONResponse({"error": str(e)}, status_code=502)
    out = dict(data)
    out["markdown"] = ft.render_report_md(data)
    return out


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
