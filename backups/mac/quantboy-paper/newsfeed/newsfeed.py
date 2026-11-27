#!/usr/bin/python3
"""quantboy newsfeed (rebuilt 2026-10-31; v2.3 2026-11-27 官方日程/已公布值/ETF 擴充). Public RSS / public JSON only; stdlib only.
Outputs ~/quantboy-paper/newsfeed/out/latest.md, latest.json, YYYY-MM-DD.{md,json}; FLAG.json only when flagged."""
from zoneinfo import ZoneInfo
import urllib.error, urllib.parse, json, os, re, sys, time, math, urllib.request, datetime as dt, email.utils, xml.etree.ElementTree as ET, html
BASE = os.path.dirname(os.path.abspath(__file__)); OUT = os.path.join(BASE, 'out'); os.makedirs(OUT, exist_ok=True)
TPE = dt.timezone(dt.timedelta(hours=8)); NOW = dt.datetime.now(TPE)
UA = {'User-Agent': 'quantboy-newsfeed/2.3 (personal research; contact ［已遮蔽：聯絡信箱］)'}
RSS = {'CoinDesk': 'https://www.coindesk.com/arc/outboundfeeds/rss/', 'Cointelegraph': 'https://cointelegraph.com/rss',
       'Decrypt': 'https://decrypt.co/feed', 'Bitcoin Magazine': 'https://bitcoinmagazine.com/.rss/full/',
       'The Block': 'https://www.theblock.co/rss.xml', 'Fed press': 'https://www.federalreserve.gov/feeds/press_all.xml'}
CAL_DAYS = 14   # 2026-11-27: 7 天窗會漏掉下週五非農、FOMC；ForexFactory(faireconomy) 已移除
RECENT_DAYS = 8 # 已公布實際值回看天數
SYMS = ['BTCUSDT', 'ETHUSDT', 'XAUTUSDT']
FLAG_RE = re.compile(r'\b(hack|hacked|hacker|hackers|exploit|exploited|drained|delist|delisting|bankrupt|bankruptcy|insolvent|insolvency|halts? withdrawals|withdrawals (halted|paused|suspended)|depeg|depegged|sec sues|etf (approved|rejected)|rate (hike|cut))\b', re.I)
FLAG_CTX = re.compile(r'\b(crypto|bitcoin|btc|ether|ethereum|eth|solana|stablecoin|usdt|usdc|tether|defi|dex|exchange|binance|coinbase|okx|bybit|kraken|bitget|ourbit|token|wallet|bridge|protocol|blockchain|etf|fed|fomc)\b', re.I)
FLAG_EXCL = re.compile(r'reward hack|growth hack|life ?hack|hackathon|hack ?day|ethical hack|php hack|prompt hack', re.I)
MACRO_RE = re.compile(r'\b(rate (hike|cut)|etf (approved|rejected))\b', re.I)
def flag_of(title):
    if FLAG_EXCL.search(title): return None
    m = FLAG_RE.search(title)
    if not m: return None
    if not MACRO_RE.match(m.group(0)) and not FLAG_CTX.search(title): return None
    return m.group(0).lower()
errors = []
def get(url, timeout=20, hops=3):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return r.read()
    except urllib.error.HTTPError as e:
        loc = e.headers.get('Location')
        if e.code in (307, 308) and loc and hops: return get(urllib.parse.urljoin(url, loc), timeout, hops - 1)
        raise
class Challenge(Exception): pass
def get_polite(url, timeout=25, tries=3):
    """429 時禮貌退避重試（尊重 Retry-After，上限 60 秒）；若是 bot 挑戰頁（Vercel/Cloudflare）則直接停止，不繞過。"""
    for i in range(tries):
        try: return get(url, timeout)
        except urllib.error.HTTPError as e:
            if e.headers.get('x-vercel-mitigated') == 'challenge' or e.headers.get('cf-mitigated') == 'challenge':
                raise Challenge(f'HTTP {e.code} bot 挑戰頁（{"Vercel Security Checkpoint" if e.headers.get("x-vercel-mitigated") else "Cloudflare challenge"}），依規定不繞過')
            if e.code != 429 or i == tries - 1: raise
            ra = e.headers.get('Retry-After'); wait = min(60, int(ra)) if ra and ra.isdigit() else 5 * 3 ** i
            time.sleep(wait)
def news():
    items, seen = [], set(); cutoff = NOW - dt.timedelta(hours=36)
    for src, url in RSS.items():
        try: root = ET.fromstring(get(url))
        except Exception as e: errors.append(f'rss {src}: {e}'); continue
        for it in root.iter('item'):
            t = html.unescape((it.findtext('title') or '').strip()); link = (it.findtext('link') or '').strip()
            try: pub = email.utils.parsedate_to_datetime(it.findtext('pubDate')).astimezone(TPE)
            except Exception: pub = None
            if not t or not link or (pub and pub < cutoff): continue
            key = re.sub(r'\W+', '', t.lower())[:80]
            if key in seen or link in seen: continue
            seen.update((key, link)); fl = flag_of(t)
            items.append(dict(source=src, title=t, url=link, published=pub.isoformat() if pub else None, flag=fl))
    items.sort(key=lambda x: x['published'] or '', reverse=True); return items
ET_TZ = ZoneInfo('America/New_York')
OFFICIAL_ICS = {'BLS': 'https://www.bls.gov/schedule/news_release/bls.ics',
                'BEA': 'https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics'}
FOMC_URL = 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'
DOL_SCHED_URL = 'https://oui.doleta.gov/unemploy/claims_arch.asp'
DOL_RELEASE_URL = 'https://www.dol.gov/ui/data.pdf'
# 只收使用者指定的美國關鍵數據：非農、CPI、PPI、JOLTS、GDP、PCE（+ FOMC、初領失業金、ISM 日程）；不收 Census、非美國
KEY_RELEASES = [('NFP', r'^Employment Situation\b'), ('CPI', r'^Consumer Price Index\b'), ('PPI', r'^Producer Price Index\b'),
                ('JOLTS', r'^Job Openings and Labor Turnover'), ('GDP', r'^GDP\b|Gross Domestic Product'), ('PCE', r'^Personal Income and Outlays')]
KEY_EXCL = re.compile(r'by County|by State|by Metropolitan|State Employment|Metropolitan|Puerto Rico|Territor', re.I)
def key_of(title):
    if KEY_EXCL.search(title): return None
    for k, pat in KEY_RELEASES:
        if re.search(pat, title, re.I): return k
    return None
_ics_cache = {}
def ics_events(src, url, lo, hi):
    if url not in _ics_cache: _ics_cache[url] = get(url).decode('utf-8', 'replace').replace('\r\n ', '').replace('\n ', '').replace('\r', '')
    raw = _ics_cache[url]; out = []
    for blk in raw.split('BEGIN:VEVENT')[1:]:
        f = {}
        for line in blk.split('\n'):
            if ':' in line: k, v = line.split(':', 1); f.setdefault(k.split(';')[0], (k, v))
        if 'DTSTART' not in f or 'SUMMARY' not in f: continue
        k, v = f['DTSTART']
        try:
            if v.endswith('Z'): t = dt.datetime.strptime(v, '%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc)
            elif 'T' in v: t = dt.datetime.strptime(v, '%Y%m%dT%H%M%S').replace(tzinfo=ET_TZ)
            else: continue
        except Exception: continue
        t = t.astimezone(TPE)
        if not (lo <= t <= hi): continue
        title = f['SUMMARY'][1].replace('\\,', ',').replace('\\;', ';').strip(); kk = key_of(title)
        if not kk: continue
        out.append(dict(time_taipei=t.isoformat(), country='USD', title=title, key=kk, impact='High',
                        forecast=None, previous=None, source=src, source_url=url, time_confirmed=True))
    return out
def fomc_events(lo, hi):
    h = get(FOMC_URL).decode('utf-8', 'replace'); out = []
    for m in re.finditer(r'<h4><a[^>]*>(\d{4}) FOMC Meetings</a></h4>', h):
        yr = int(m.group(1)); nxt = h.find('FOMC Meetings</a></h4>', m.end()); sec = h[m.end(): nxt if nxt > 0 else len(h)]
        months = re.findall(r'fomc-meeting__month[^>]*><strong>([A-Za-z/]+)</strong>', sec)
        dates = re.findall(r'fomc-meeting__date[^>]*>([^<]+)<', sec)
        for mon, d in zip(months, dates):
            d = d.strip(); sep = '*' in d; d = d.replace('*', '').strip()
            mm = re.match(r'(\d+)\s*-\s*(\d+)$', d)
            if not mm: continue
            last_mon = mon.split('/')[-1]
            try: day = dt.datetime.strptime(f'{yr} {last_mon} {mm.group(2)} 14:00', '%Y %B %d %H:%M').replace(tzinfo=ET_TZ).astimezone(TPE)
            except Exception: continue
            if lo <= day <= hi:
                out.append(dict(time_taipei=day.isoformat(), country='USD', title='FOMC 利率決議（會議 ' + mon + ' ' + d + ('，含 SEP 點陣圖' if sep else '') + '）', key='FOMC',
                                impact='High', forecast=None, previous=None, source='Fed', source_url=FOMC_URL, time_confirmed=False,
                                note='Fed 官方頁只列日期；14:00 ET 為聲明慣例時間，未經頁面確認'))
    return out
def claims_events(lo, hi):
    """DOL 官方規則：每週四 08:30 ET 公布；週四逢聯邦假日時改期，改期日以 DOL 頁面表格為準。"""
    exc = []
    try:
        s = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' | ', html.unescape(get(DOL_SCHED_URL).decode('utf-8', 'replace'))))
        for m in re.finditer(r'(Monday|Tuesday|Wednesday|Thursday|Friday), ([A-Z][a-z]+ \d{1,2}, \d{4})', s):
            exc.append(dt.datetime.strptime(m.group(2), '%B %d, %Y').date())
        conf, note = True, 'DOL 官方公布規則（週四 08:30 ET）＋官方改期表'
    except Exception as e:
        errors.append(f'calendar DOL: {e}'); conf, note = False, 'DOL 改期表讀取失敗，僅依週四 08:30 ET 規則，可能未反映假日改期'
    out = []; d = lo.astimezone(ET_TZ).date() - dt.timedelta(days=7)
    while d <= hi.astimezone(ET_TZ).date() + dt.timedelta(days=7):
        if d.weekday() == 3:
            day = next((x for x in exc if x.isocalendar()[:2] == d.isocalendar()[:2]), d)
            t = dt.datetime.combine(day, dt.time(8, 30), ET_TZ).astimezone(TPE)
            if lo <= t <= hi:
                out.append(dict(time_taipei=t.isoformat(), country='USD', title='Unemployment Insurance Weekly Claims（初領失業金）', key='CLAIMS', impact='High',
                                forecast=None, previous=None, source='DOL', source_url=DOL_SCHED_URL, time_confirmed=conf, note=note))
        d += dt.timedelta(days=1)
    return out
FRED = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id='
# key -> list of (series, label, transform)  transform: level|diff|pct_mom|pct_yoy
PREV_MAP = {'NFP': [('PAYEMS', '非農就業月增（千人）', 'diff'), ('UNRATE', '失業率 %', 'level')],
            'CPI': [('CPIAUCSL', 'CPI 月增 %', 'pct_mom'), ('CPIAUCSL', 'CPI 年增 %', 'pct_yoy'), ('CPILFESL', '核心 CPI 月增 %', 'pct_mom')],
            'PPI': [('PPIFIS', 'PPI 最終需求月增 %', 'pct_mom')],
            'JOLTS': [('JTSJOL', 'JOLTS 職缺（千）', 'level')],
            'GDP': [('A191RL1Q225SBEA', '實質 GDP 季增年率 %', 'level')],
            'PCE': [('PCEPI', 'PCE 物價月增 %', 'pct_mom'), ('PCEPI', 'PCE 物價年增 %', 'pct_yoy'), ('PCEPILFE', '核心 PCE 月增 %', 'pct_mom'), ('PCEPILFE', '核心 PCE 年增 %', 'pct_yoy')],
            'CLAIMS': [('ICSA', '初領失業金（人，季調）', 'level'), ('CCSA', '續領失業金（人，季調）', 'level')]}
_fred_cache = {}
def fred_series(sid):
    if sid not in _fred_cache:
        time.sleep(1.1)  # robots.txt Crawl-delay: 1
        rows = [l.split(',') for l in get(FRED + sid).decode().strip().splitlines()[1:]]
        _fred_cache[sid] = [(d, float(v)) for d, v in rows if v not in ('.', '')]
    return _fred_cache[sid]
def _shift_month(d, n):
    d = dt.date.fromisoformat(d); y, m = d.year, d.month + n
    while m < 1: y, m = y - 1, m + 12
    while m > 12: y, m = y + 1, m - 12
    return f'{y}-{m:02d}-01'
def _tf(s_, i, tf):
    """以日期對齊前期（序列有缺期時，例如 2025 年 10 月停擺未公布，就回 None 而不是錯配）。"""
    v = s_[i][1]; at = dict(s_)
    if tf == 'level': return v
    if tf == 'diff': base = at.get(_shift_month(s_[i][0], -1)); return None if base is None else round(v - base, 1)
    base = at.get(_shift_month(s_[i][0], -1 if tf == 'pct_mom' else -12))
    return None if base is None else round((v / base - 1) * 100, 2)
SERIES_LAG_DAYS = {'CCSA': 7}  # 續領比初領晚一週
def fred_values(key, idx_fn):
    out = []
    for sid, label, tf in PREV_MAP.get(key, []):
        try:
            s_ = fred_series(sid); i = idx_fn(s_, SERIES_LAG_DAYS.get(sid, 0))
            if i is None: out.append(dict(series=sid, label=label, value=None, period=None, status='未取得（FRED 尚未收錄該期）', source='FRED (St. Louis Fed)', source_url=FRED + sid)); continue
            out.append(dict(series=sid, label=label, value=_tf(s_, i, tf), period=s_[i][0], transform=tf, source='FRED (St. Louis Fed)', source_url=FRED + sid))
        except Exception as e: errors.append(f'fred {sid}: {e}')
    return out or None
MONTHS = {m: i for i, m in enumerate(['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'], 1)}
def expected_period(e):
    """推該次公布對應的資料期（FRED 日期格式）。無法確定就回 None。"""
    t = dt.datetime.fromisoformat(e['time_taipei']).astimezone(ET_TZ).date(); title = e['title']
    m = re.search(r'(\d)(?:st|nd|rd|th) Quarter (\d{4})', title)
    if m: return f'{m.group(2)}-{(int(m.group(1)) - 1) * 3 + 1:02d}-01'
    m = re.search(r'(' + '|'.join(MONTHS) + r') (\d{4})', title)
    if m: return f'{m.group(2)}-{MONTHS[m.group(1)]:02d}-01'
    lag = {'NFP': 1, 'CPI': 1, 'PPI': 1, 'JOLTS': 2}.get(e['key'])
    if lag:
        y, mo = t.year, t.month - lag
        while mo < 1: y, mo = y - 1, mo + 12
        return f'{y}-{mo:02d}-01'
    if e['key'] == 'CLAIMS':
        d = t - dt.timedelta(days=1)
        while d.weekday() != 5: d -= dt.timedelta(days=1)
        return d.isoformat()
    return None
def us_business_days(y, m):
    d = dt.date(y, m, 1); out = []
    hol = {dt.date(y, 1, 1), dt.date(y, 7, 4), dt.date(y, 12, 25)}
    while d.month == m:
        if d.weekday() < 5 and d not in hol: out.append(d)
        d += dt.timedelta(days=1)
    return out
def ism_events(lo, hi):
    out = []
    for y, m in sorted({(lo.year, lo.month), (hi.year, hi.month)}):
        bd = us_business_days(y, m)
        for idx, name in ((0, 'ISM 製造業 PMI'), (2, 'ISM 服務業 PMI')):
            t = dt.datetime.combine(bd[idx], dt.time(10, 0), ET_TZ).astimezone(TPE)
            if lo <= t <= hi:
                out.append(dict(time_taipei=t.isoformat(), country='USD', title=name + '（只列日程，數值未取得）', key='ISM', impact='High', forecast=None, previous=None,
                                source='ISM 慣例推算（第 1／第 3 個營業日 10:00 ET）', source_url='https://www.ismworld.org/supply-management-news-and-reports/reports/rob-report-calendar/',
                                time_confirmed=False, note='ISM 日程/報告頁導向登入頁（含 reCAPTCHA），不繞過；日期依 ISM 公開慣例推算，假日調整只含元旦/獨立紀念日/聖誕；PMI 數值未取得'))
    return out
def official_events(lo, hi):
    ev, srcs = [], []
    for src, url in OFFICIAL_ICS.items():
        try: ev += ics_events(src, url, lo, hi); srcs.append(url)
        except Exception as e: errors.append(f'calendar {src}: {e}')
    for name, fn, url in (('Fed', fomc_events, FOMC_URL), ('DOL', claims_events, DOL_SCHED_URL), ('ISM', ism_events, None)):
        try: ev += fn(lo, hi); url and srcs.append(url)
        except Exception as e: errors.append(f'calendar {name}: {e}')
    return ev, srcs
def calendar():
    ev, srcs = official_events(NOW - dt.timedelta(hours=12), NOW + dt.timedelta(days=CAL_DAYS))
    for x in ev:
        if x['key'] in PREV_MAP: x['previous_official'] = fred_values(x['key'], lambda s_, lag: len(s_) - 1)
    ev.sort(key=lambda x: (x['time_taipei'], x['source'])); return ev, srcs
def released_recent():
    """近 RECENT_DAYS 天已公布的關鍵數據，附官方實際值（FRED 收錄的 BLS/BEA/DOL 原始數列），對不到該期就標未取得。"""
    ev, _ = official_events(NOW - dt.timedelta(days=RECENT_DAYS), NOW)
    out = []
    for x in ev:
        if x['key'] not in PREV_MAP: continue
        if dt.datetime.fromisoformat(x['time_taipei']) > NOW - dt.timedelta(hours=3): continue
        per = expected_period(x); x['expected_period'] = per
        x['actual_official'] = fred_values(x['key'], lambda s_, lag: next((i for i in range(len(s_) - 1, -1, -1) if per and s_[i][0] == (dt.date.fromisoformat(per) - dt.timedelta(days=lag)).isoformat()), None))
        if x['key'] == 'GDP': x['note'] = 'FRED 只存最新估計值；同季的初值／修正值無法從此來源區分版本'
        out.append(x)
    out.sort(key=lambda x: x['time_taipei']); return out
def prices():
    out = {}
    for s in SYMS:
        try:
            k = json.loads(get(f'https://api.binance.com/api/v3/klines?symbol={s}&interval=1d&limit=31'))
            c = [float(x[4]) for x in k]; h = [float(x[2]) for x in k]; l = [float(x[3]) for x in k]
            last = float(json.loads(get(f'https://api.binance.com/api/v3/ticker/price?symbol={s}'))['price'])
            r = [math.log(c[i] / c[i - 1]) for i in range(len(c) - 7, len(c))]
            mu = sum(r) / len(r); vol = math.sqrt(sum((x - mu) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(365) * 100
            out[s] = dict(last=last, range_7d=[min(l[-7:]), max(h[-7:])], low_30d=min(l[-30:]), high_30d=max(h[-30:]), realized_vol_7d_pct=round(vol, 1), source='https://api.binance.com/api/v3/klines')
        except Exception as e: errors.append(f'price {s}: {e}')
    return out
# ── 美國現貨 ETF（發行商官網公開持倉；推算值）──────────────────────────────
ISHARES = {'IBIT': ('BTC', 'https://www.ishares.com/us/products/333011/ishares-bitcoin-trust-etf'),
           'ETHA': ('ETH', 'https://www.ishares.com/us/products/337614/ishares-ethereum-trust-etf')}
BITWISE = {'BITB': ('BTC', 'https://www.bitwiseetfs.com/single-asset/bitb'), 'ETHW': ('ETH', 'https://www.bitwiseetfs.com/single-asset/ethw')}
SHARES21 = {'ARKB': ('BTC', 'https://www.21shares.com/en-us/product/arkb')}
GRAYSCALE = {'GBTC': ('BTC', 'https://etfs.grayscale.com/gbtc'), 'BTC': ('BTC', 'https://etfs.grayscale.com/btc'),
             'ETHE': ('ETH', 'https://etfs.grayscale.com/ethe'), 'ETH': ('ETH', 'https://etfs.grayscale.com/eth')}
GRAYSCALE_ROBOTS = 'https://etfs.grayscale.com/robots.txt'
ETF_SKIP = {'FBTC': ('BTC', 'Fidelity 官方頁 fidelity.com/etfs/fidelity-wise-origin-bitcoin-fund 回 HTTP 400（內容為「Service Unavailable」頁，box 與 Mac 皆同）；digital.fidelity.com 為前端渲染，靜態 HTML 無股數／持倉，未找到免登入公開檔'),
            'FETH': ('ETH', 'Fidelity 同上（HTTP 400 / 前端渲染），未找到免登入公開檔')}
ETF_NOTE = {'ARKB': 'ark-funds.com 回 403 Cloudflare；ARK 持倉 CSV 所在 assets.ark-funds.com 的 robots.txt 為「User-agent: * Disallow: /」，不抓。改用 21shares.com 官方產品頁（robots 允許）的 NAV 與 AUM，股數＝AUM÷NAV（NAV 只到小數 2 位，股數誤差約 ±0.03%）'}
def _num(x): return float(x.replace(',', '').replace('$', '').strip())
def etf_ishares(t, asset):
    t = html.unescape(t)
    def f(name):
        m = re.search(r'"formattedValue":"([^"]*)"[^{}]{0,600}?"formattedAsOfDate":"([^"]*)","name":"' + name + '"', t)
        return (_num(m.group(1)), m.group(2)) if m else (None, None)
    so, d = f('sharesOutstanding'); na, _ = f('totalNetAssetsFundLevel'); bk, _ = f('basketAmt')
    nav = re.search(r'"value":([0-9.]+),"returnType":false,"associatedReturnType":false,"fullName":"fundHeader.fundNav.navAmount"', t)
    if so is None: raise ValueError('找不到 sharesOutstanding')
    held = so * bk / 40000 if bk else None
    return dict(as_of=dt.datetime.strptime(d, '%b %d, %Y').date().isoformat(), shares_outstanding=so, held=held, nav=float(nav.group(1)) if nav else None, net_assets_usd=na,
                held_method=f'shares_outstanding × Basket {asset} Amount ÷ 40,000（官網每籃 40,000 股），推算值' if held else None,
                alignment='iShares「資料日 D」的流通股數反映前一交易日的申贖；流量記在前一資料日（已用 Farside 11/20–11/24 IBIT 驗證，NAV 取前一資料日）')
def etf_bitwise(t, asset):
    s = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' | ', html.unescape(t)))
    pd_ = re.search(r'Product Details [| ]*Data as of: [| ]*(\d\d/\d\d/\d{4}) [| ]*NAV [| ]*\$([0-9.,]+) .{0,80}?Shares Outstanding [| ]*([0-9,]+)', s)
    hd = re.search(r'Holdings [| ]*Data as of: [| ]*(\d\d/\d\d/\d{4}).{0,800}?\| ' + asset + r' [| ]*([0-9,]+\.\d+) ', s)
    if not pd_: raise ValueError('找不到 Product Details（NAV／Shares Outstanding）')
    f = lambda x: dt.datetime.strptime(x, '%m/%d/%Y').date().isoformat()
    return dict(as_of=f(pd_.group(1)), shares_outstanding=_num(pd_.group(3)), nav=_num(pd_.group(2)), held=_num(hd.group(2)) if hd else None,
                holdings_as_of=f(hd.group(1)) if hd else None, held_method=f'官網 Holdings「{asset} Asset in Trust」（資料日見 holdings_as_of）',
                alignment='股數與 NAV 取官網 Product Details 同一資料日；與 Farside 交易日的對齊尚未驗證')
def etf_21shares(t, asset):
    s = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' | ', html.unescape(t)))
    m = re.search(r'\$ [| ]*([0-9.,]+) [| ]*NAV [| ]*\$ [| ]*([0-9.,]+) [| ]*AUM .{0,300}?Value as of ([A-Z][a-z]+ \d{1,2}, \d{4})', s)
    if not m: raise ValueError('找不到 NAV／AUM')
    nav, aum = _num(m.group(1)), _num(m.group(2))
    return dict(as_of=dt.datetime.strptime(m.group(3), '%B %d, %Y').date().isoformat(), shares_outstanding=round(aum / nav), nav=nav, held=None, net_assets_usd=aum,
                held_method=None, shares_method='AUM ÷ NAV（推算）', alignment='資料日為官網「Value as of」；與 Farside 交易日的對齊尚未驗證')
def prev_bday(d):
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5: d -= dt.timedelta(days=1)
    return d
def etf_flows(px):
    d = os.path.join(OUT, 'etf_holdings'); os.makedirs(d, exist_ok=True)
    snap, miss = {}, {k: v for k, v in ETF_SKIP.items()}
    jobs = [(k, a, u, etf_ishares) for k, (a, u) in ISHARES.items()] + [(k, a, u, etf_bitwise) for k, (a, u) in BITWISE.items()] + [(k, a, u, etf_21shares) for k, (a, u) in SHARES21.items()]
    for k, a, u, fn in jobs:
        try: snap[k] = dict(fn(get_polite(u, 30).decode('utf-8', 'ignore'), a), asset=a, source_url=u, fetched_at=NOW.isoformat())
        except Exception as e: miss[k] = (a, f'抓取/解析失敗：{e}'); errors.append(f'etf {k}: {e}')
    try:  # Grayscale：先確認 robots.txt，429 退避重試；遇 bot 挑戰頁即停
        get_polite(GRAYSCALE_ROBOTS, 20)
        for k, (a, u) in GRAYSCALE.items(): miss[k] = (a, 'robots.txt 可讀但尚未實作解析（需人工確認頁面格式）')
    except Exception as e:
        for k, (a, u) in GRAYSCALE.items(): miss[k] = (a, f'etfs.grayscale.com {e}（robots.txt 本身也被擋，含退避重試後仍同），跳過')
    prev = {}
    for fn in sorted(x for x in os.listdir(d) if re.match(r'\d{4}-\d\d-\d\d\.json$', x)):
        try:
            for k, v in json.load(open(os.path.join(d, fn))).items(): prev.setdefault(k, []).append(v)
        except Exception: pass
    if snap:
        fp = os.path.join(d, f'{NOW:%Y-%m-%d}.json'); old = json.load(open(fp)) if os.path.exists(fp) else {}; old.update(snap)
        tmp = fp + '.tmp'; open(tmp, 'w').write(json.dumps(old, ensure_ascii=False, indent=1)); os.replace(tmp, fp)
    funds = {}; tot = {a: dict(usd=0.0, held=0.0, n=0, covered=[]) for a in ('BTC', 'ETH')}
    for k, v in snap.items():
        a = v['asset']; held = v.get('held', v.get('btc_held')); tot[a]['covered'].append(k)
        r = dict(label='推算值', asset=a, as_of=v['as_of'], shares_outstanding=v['shares_outstanding'], held=held, nav=v['nav'], source_url=v['source_url'],
                 held_method=v.get('held_method'), alignment=v.get('alignment'), note=ETF_NOTE.get(k))
        p = [x for x in prev.get(k, []) if x.get('as_of') and x['as_of'] < v['as_of']]
        if not p: r.update(status='未取得（首日無前值）', d_shares=None, d_held=None, flow_usd_est=None, trade_date=None)
        else:
            q = max(p, key=lambda x: x['as_of']); qheld = q.get('held', q.get('btc_held'))
            if (dt.date.fromisoformat(v['as_of']) - dt.date.fromisoformat(q['as_of'])).days > 6:
                r.update(status=f"未取得（前值過舊：{q['as_of']}，不跨期相減）", d_shares=None, d_held=None, flow_usd_est=None, trade_date=None); funds[k] = r; continue
            dsh = v['shares_outstanding'] - q['shares_outstanding'] if v['shares_outstanding'] is not None and q.get('shares_outstanding') is not None else None
            dheld = held - qheld if held is not None and qheld is not None else None
            gap = prev_bday(dt.date.fromisoformat(v['as_of'])).isoformat() != q['as_of']
            if k in ISHARES:  # 已驗證：資料日 D 對 D-1 的差 = D-1 交易日的流量，用 D-1 的 NAV
                nav_used = q.get('nav') or v['nav']; trade = q['as_of']
            else: nav_used = v['nav']; trade = None
            usd = dsh * nav_used if dsh is not None and nav_used else (dheld * px[a] if dheld is not None and px.get(a) else None)
            r.update(status='推算值' + ('（跨多個交易日合計）' if gap else ''), prev_as_of=q['as_of'], trade_date=trade, d_shares=dsh, d_held=dheld, flow_usd_est=usd,
                     usd_method=('Δshares × NAV' + ('（前一資料日 NAV）' if k in ISHARES else '')) if dsh is not None and nav_used else f'Δ{a} × Binance {a}USDT')
            if usd is not None: tot[a]['usd'] += usd; tot[a]['n'] += 1
            if dheld is not None: tot[a]['held'] += dheld
        funds[k] = r
    total = {a: dict(label='推算值', funds_with_change=t['n'], funds_covered=sorted(t['covered']), flow_usd_est=t['usd'] if t['n'] else None, d_held=t['held'] if t['n'] else None,
                     status='推算值（僅含有前值的基金；各基金資料日可能不同）' if t['n'] else '未取得（首日無前值）',
                     note='非全市場總流量；未涵蓋：' + ('、'.join(sorted(x for x, (aa, _) in miss.items() if aa == a)) or '無')) for a, t in tot.items()}
    return dict(funds=funds, total=total, total_btc=total['BTC']), {k: v[1] for k, v in miss.items()}, {k: v[0] for k, v in miss.items()}
def etf_display(value, spec):
    return '未取得' if value is None else format(value, spec)
def fmt_vals(vals):
    return '、'.join(f"{p['label']} {p['value'] if p.get('value') is not None else '未取得'}" + (f"（{p['period'][:7] if not p['series'] in ('ICSA', 'CCSA') else '週止 ' + p['period']}）" if p.get('period') else '') for p in vals)
def main():
    n = news(); ev, cs = calendar(); rel = released_recent(); p = prices(); flags = [x for x in n if x['flag']]
    etf, etf_miss, miss_asset = etf_flows({'BTC': p.get('BTCUSDT', {}).get('last'), 'ETH': p.get('ETHUSDT', {}).get('last')})
    doc = dict(schema='quantboy.newsfeed/3', generated_at=NOW.isoformat(), timezone='Asia/Taipei', prices=p, calendar_7d=ev, calendar_window_days=CAL_DAYS, released_recent=rel, etf_flows_est=etf,
               calendar_sources=cs, news=n[:60], flags=flags, errors=errors,
               missing=['token_unlocks', 'etf_flows (' + '、'.join(etf_miss) + ' 未取得；其餘為推算值)', 'ism_values', 'consensus_forecast', 'non_us_calendar (使用者決定不收)', 'census_releases (使用者決定不收)'],
               missing_detail={'token_unlocks': 'Tokenomist：ToS（tokenomist.ai/terms）明文禁止 crawling/"scraping"/robot，robots.txt「Disallow: /api/」→ 不抓。CryptoRank：條款頁回 403 Cloudflare 挑戰頁，無法確認條款（不繞過），robots.txt「Disallow: /*?*」→ 不抓。DefiLlama emissions API 回 402（付費）。VYLOS（vylos.io）：無公開 API 文件；解鎖資料來自 DefiLlama（付費資料）並經其站內代理 /api/llama-unlocks 轉發，ToS 頁為前端渲染讀不到條文 → 不接入（等使用者決定是否向 team@vylos.io 申請授權）',
                               'etf_flows': {'Farside': 'Cloudflare 403 且 robots.txt 只允許 Twitterbot', **etf_miss, **{k: v for k, v in ETF_NOTE.items()}},
                               'consensus_forecast': '已依使用者要求移除 ForexFactory 預期值／前值；只保留官方前值（FRED 收錄之 BLS/BEA/DOL 數列）',
                               'ism_official_schedule': 'ISM 日程與報告頁導向 ecommerce.ismworld.org 登入頁（含 reCAPTCHA），不繞過；改用慣例推算並標 time_confirmed=false',
                               'ism_values': 'ISM 官網需登入；ISM 新聞稿轉載於 PR Newswire，但 PR Newswire 使用條款禁止 robot/spider/自動擷取 → 未取得'})
    md = [f'# 每日總經與加密新聞 {NOW:%Y-%m-%d %H:%M} (Asia/Taipei)', '', '## 價位 (Binance 公開 API)']
    for s, v in p.items(): md.append(f"- {s}: {v['last']:,.2f}｜7日 {v['range_7d'][0]:,.2f}–{v['range_7d'][1]:,.2f}｜30日低 {v['low_30d']:,.2f}｜7日實現波動 {v['realized_vol_7d_pct']}%")
    md += ['', f'## 未來 {CAL_DAYS} 天美國關鍵數據日曆（官方日程：BLS / BEA / Fed / DOL；時間為台北）']
    md += [f"- {e['time_taipei'][5:16].replace('T',' ')} {e['title']}（{e['source']}{'' if e.get('time_confirmed', True) else '；時間為慣例未確認'}）" + (('｜官方前值：' + fmt_vals(e['previous_official'])) if e.get('previous_official') else '') for e in ev] or ['- 無']
    md += ['', f'## 近 {RECENT_DAYS} 天已公布（官方實際值，FRED 收錄之 BLS/BEA/DOL 數列）']
    md += [f"- {e['time_taipei'][5:16].replace('T',' ')} {e['title']}（{e['source']}）｜實際值：" + (fmt_vals(e['actual_official']) if e.get('actual_official') else '未取得') + (f"｜註：{e['note']}" if e.get('key') == 'GDP' else '') for e in rel] or ['- 無']
    for a, title in (('BTC', '美國現貨 BTC ETF 流量'), ('ETH', '美國現貨 ETH ETF 流量')):
        md += ['', f'## {title}（推算值，發行商官網持倉前後日相減）']
        for k, r in etf['funds'].items():
            if r['asset'] != a: continue
            when = f"交易日 {r['trade_date']}" if r.get('trade_date') else f"資料日 {r['as_of']} 較 {r.get('prev_as_of')}"
            md.append(f"- {k} 推算值｜資料日 {r['as_of']}｜流通股數 {etf_display(r['shares_outstanding'], ',.0f')}｜持有 {a} {etf_display(r['held'], ',.2f')}｜" +
                      (r['status'] if r['d_shares'] is None and r['d_held'] is None else f"{when}：Δ股數 {etf_display(r['d_shares'], '+,.0f')}、Δ{a} {etf_display(r['d_held'], '+,.2f')}、估計流量 {(format(r['flow_usd_est'], '+,.0f') + ' USD') if r['flow_usd_est'] is not None else '未取得'}" + ('' if r['status'] == '推算值' else f"（{r['status']}）")) + f"｜{r['source_url']}")
        t = etf['total'][a]; md.append(f"- 合計 推算值（{'、'.join(t['funds_covered']) or '無'}）：" + (f"{etf_display(t['flow_usd_est'], '+,.0f')} USD、{etf_display(t['d_held'], '+,.2f')} {a}" if t['flow_usd_est'] is not None else t['status']) + '；非全市場總量')
        md += [f'- {k} 未取得：{v}' for k, v in etf_miss.items() if miss_asset[k] == a]
    md += ['- 口徑：iShares（IBIT/ETHA）資料日 D 的股數反映 D 前一交易日申贖，故流量記在前一資料日並用該日 NAV（與 Farside 11/20–11/24 IBIT 數字吻合）；Bitwise／21Shares 對齊待累積資料驗證']
    md += ['', '## 旗標'] + ([f"- [{x['flag']}] {x['title']} ({x['source']}) {x['url']}" for x in flags] or ['- 無'])
    md += ['', '## 新聞 (近 36 小時)'] + [f"- {x['title']} ({x['source']}) {x['url']}" for x in n[:30]]
    md += ['', '## 未取得',
           '- 代幣解鎖：Tokenomist 條款禁止爬取、CryptoRank 403、DefiLlama 付費；VYLOS 無公開 API，資料轉自 DefiLlama 付費源，不接入 → 未取得',
           '- ISM PMI 數值：官網需登入（reCAPTCHA）、PR Newswire 條款禁止自動擷取 → 未取得；日程為慣例推算',
           '- 市場共識預期值：無合法免費官方來源 → 不列（已移除 ForexFactory）'] + [f'- 錯誤：{e}' for e in errors] + ['', '來源：' + ' ｜ '.join(list(RSS.values()) + cs + [FRED + '…'])]
    def w(name, text):
        tmp = os.path.join(OUT, name + '.tmp'); open(tmp, 'w').write(text); os.replace(tmp, os.path.join(OUT, name))
    js = json.dumps(doc, ensure_ascii=False, indent=1); mdt = '\n'.join(md) + '\n'
    w('latest.json', js); w('latest.md', mdt); w(f'{NOW:%Y-%m-%d}.json', js); w(f'{NOW:%Y-%m-%d}.md', mdt)
    fp = os.path.join(OUT, 'FLAG.json')
    if flags: w('FLAG.json', json.dumps(dict(generated_at=NOW.isoformat(), flags=flags), ensure_ascii=False, indent=1))
    elif os.path.exists(fp): os.replace(fp, os.path.join(OUT, f'FLAG.cleared_{NOW:%Y%m%d_%H%M}.json'))
    print(f'{NOW.isoformat()} ok news={len(n)} cal={len(ev)} released={len(rel)} prices={len(p)} etf={len(etf["funds"])} flags={len(flags)} errors={len(errors)}', *errors, sep='\n  ')
if __name__ == '__main__': main()
