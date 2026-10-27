"""ดึงแท่งเทียนหลาย TF + ประวัติไม้ + ฝาก/ถอน + MFE/MAE จาก MT5 -> trade_chart.html
Pull multi-TF candles, trade history, cash flows and MFE/MAE from MT5 -> trade_chart.html

ตั้งค่าใน config.json (ก๊อปจาก config.example.json) / settings live in config.json
หลายบัญชี: ใส่ "accounts": [{"login":..,"server":"..","name":".."}] -> หน้าเว็บมี dropdown เลือกบัญชี / รวมทั้งหมด
Multiple accounts: list them under "accounts" -> the page gets an account picker with an "All" option
บัญชีที่ปิดแล้ว (เงิน 0 ไม่มีไม้เปิด) เก็บ cache ไว้ใน cache/ ไม่ต้อง login ใหม่ — `python build.py --full` = ดึงใหม่หมด
Closed accounts (zero equity, no open trades) are cached in cache/; `--full` forces a fresh pull
"""
import MetaTrader5 as m, datetime as dt, json, collections, pathlib, time, sys, atexit

HERE = pathlib.Path(__file__).parent
CACHE = HERE / 'cache'
FULL = '--full' in sys.argv
CFG_FILE = HERE / 'config.json'
cfg = json.loads(CFG_FILE.read_text(encoding='utf-8')) if CFG_FILE.exists() else {}
# 0 = ใช้บัญชีที่ login อยู่ใน MT5 ตอนนี้ / use the account currently logged in
ACCOUNTS = cfg.get('accounts') or [{'login': int(cfg.get('login') or 0), 'server': cfg.get('server') or '',
                                    'symbol': cfg.get('symbol') or ''}]
TZ_OUT = float(cfg.get('utc_offset', 7))  # เวลาที่แสดงบนกราฟ / display timezone
MAX_LOT = float(cfg.get('max_lot', 0.01)) # lot เกินนี้ติดธง / flag trades above this lot
TFS = {'M15': m.TIMEFRAME_M15, 'H1': m.TIMEFRAME_H1, 'H4': m.TIMEFRAME_H4, 'D1': m.TIMEFRAME_D1}
utc = lambda ts: dt.datetime.fromtimestamp(ts, dt.UTC)  # ต้อง tz-aware ไม่งั้น lib ตีเป็นเวลาเครื่อง


def fail(msg):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, 'MT5 Trade Chart', 0x10)
    except Exception:
        pass
    raise SystemExit(msg)


class Skip(Exception):
    pass


def srv_offset(sym):
    """offset เวลาเซิร์ฟเวอร์ (ชม.) จาก tick ล่าสุด, None ถ้า tick ค้างเกินจะเชื่อได้"""
    tick = m.symbol_info_tick(sym)
    off = (tick.time - time.time()) / 3600 if tick else 99
    return round(off) if abs(off) <= 14 else None


def pull(login, server, sym):
    """ดึงข้อมูลบัญชีที่ login อยู่ตอนนี้ / pull everything from the currently logged-in account"""
    acct = m.account_info()
    if acct is None or (login and acct.login != login):
        raise Skip(f'ยังไม่ได้อยู่บัญชี {login} / not on account {login}')

    H0, H1 = dt.datetime(2000, 1, 1, tzinfo=dt.UTC), dt.datetime.now(dt.UTC) + dt.timedelta(days=2)
    # หลัง login ประวัติ deal ซิงก์ตามมาทีละก้อน (บัญชีเก่าหลายวิ) -> รอจนไม้ที่เปิดอยู่มี deal ครบ และจำนวน deal นิ่ง 3 รอบติด
    open_ids = {p.identifier for p in m.positions_get() or []}
    prev, same = -1, 0
    for _ in range(60):
        all_deals = m.history_deals_get(H0, H1) or ()
        same = same + 1 if len(all_deals) == prev else 0
        prev = len(all_deals)
        if all_deals and same >= 3 and open_ids <= {d.position_id for d in all_deals if d.entry == 0}:
            break
        time.sleep(1)
    # ไม้ทองทุก symbol ในบัญชี (เช่น GOLD# + GOLD24-7#), แท่งเทียนใช้ตัวที่เทรดเยอะสุด
    cnt = collections.Counter(d.symbol for d in all_deals if d.symbol and ('XAU' in d.symbol.upper() or 'GOLD' in d.symbol.upper()))
    syms = [sym] if sym else [s for s, _ in cnt.most_common()]
    if not syms:
        raise Skip('ไม่เจอไม้ทองในบัญชีนี้ / no gold trades found')
    sym = syms[0]
    for s in syms:
        if not m.symbol_select(s, True) or m.symbol_info_tick(s) is None:
            raise Skip(f'ไม่เจอ symbol {s} ใน MT5 / symbol {s} not found')

    # หา offset เวลาเซิร์ฟเวอร์ (tick.time เป็นเวลาเซิร์ฟเวอร์) — tick ค้างนาน (บัญชีเก่า) ใช้ค่าสำรอง
    srv_off_h = srv_offset(sym)
    if srv_off_h is None:
        srv_off_h = FALLBACK_OFF
    shift = int((TZ_OUT - srv_off_h) * 3600)

    deals = [d for d in all_deals if d.symbol in syms]
    # ฝาก/ถอน/โอน (deal type 2 = balance)
    cash = [{'t': d.time + shift, 'amt': d.profit, 'note': d.comment, 'acct': acct.login} for d in all_deals if d.type == 2]
    csizes = {s: m.symbol_info(s).trade_contract_size for s in syms}
    csize = csizes[sym]
    orders = {o.ticket: o for o in m.history_orders_get(H0, H1) or []}

    pos = collections.defaultdict(lambda: {'in': [], 'out': []})
    for d in deals:
        pos[d.position_id]['in' if d.entry == 0 else 'out'].append(d)

    def vwap(ds):
        v = sum(x.volume for x in ds); return sum(x.price * x.volume for x in ds) / v, v

    trades = []
    for pid, p in pos.items():
        if not p['in']: continue
        ep, vol = vwap(p['in']); side = 'buy' if p['in'][0].type == 0 else 'sell'
        o = orders.get(p['in'][0].order)
        t = {'id': pid, 'acct': acct.login, 'sym': p['in'][0].symbol, 'cs': csizes[p['in'][0].symbol], 'side': side, 'vol': round(vol, 2), 'entry': ep,
             't0': min(x.time for x in p['in']) + shift,
             'sl': (o.sl or None) if o else None, 'tp': (o.tp or None) if o else None}
        if p['out']:
            xp, _ = vwap(p['out'])
            t.update(exit=xp, t1=max(x.time for x in p['out']) + shift, open=False,
                     profit=round(sum(x.profit + x.swap + x.commission + x.fee for x in p['out'] + p['in']), 2),
                     comment=p['out'][-1].comment)
        trades.append(t)

    for op in [p for p in m.positions_get() or [] if p.symbol in syms]:
        if not any(t['id'] == op.identifier for t in trades):  # ประวัติยังไม่มา -> สร้างจาก position ตรงๆ
            trades.append({'id': op.identifier, 'acct': acct.login, 'sym': op.symbol, 'cs': csizes[op.symbol], 'side': 'buy' if op.type == 0 else 'sell',
                           'vol': op.volume, 'entry': op.price_open, 't0': op.time + shift, 'sl': None, 'tp': None})
        for t in trades:
            if t['id'] == op.identifier:
                t.update(exit=op.price_current, t1=int(time.time()) + srv_off_h*3600 + shift, open=True,
                         profit=round(op.profit + op.swap, 2), sl=op.sl or None, tp=op.tp or None, comment='OPEN')
    trades = [t for t in trades if 'exit' in t]
    if not trades:
        raise Skip(f'ไม่มีไม้ {sym} ในบัญชีนี้ / no {sym} trades in this account')
    for t in trades:
        d = 1 if t['side'] == 'buy' else -1
        t['pct'] = round((t['exit'] - t['entry']) / t['entry'] * 100 * d, 2)

    # MFE/MAE จากแท่ง M1 ระหว่างถือ (หน่วยราคา, บวก = ไปทางที่ได้เปรียบ)
    for t in trades:
        a, b = t['t0'] - shift, t['t1'] - shift
        # ช่วงเก่าเกินที่เซิร์ฟเวอร์เก็บแท่ง M1 -> MT5 คืนแท่งช่วงล่าสุดมาแทน (MFE เพี้ยนเป็นพันดอลล์) -> กรองเวลาให้อยู่ในช่วงถือจริง แล้วถอยไป TF ใหญ่
        r = None
        for tf, sec in ((m.TIMEFRAME_M1, 60), (m.TIMEFRAME_M15, 900), (m.TIMEFRAME_H1, 3600)):
            x = m.copy_rates_range(t['sym'], tf, utc(a // sec * sec), utc(b))
            if x is not None and len(x):
                x = x[(x['time'] >= a // sec * sec) & (x['time'] <= b)]
            if x is not None and len(x):
                r = x; break
        if r is None:
            t['mfe'] = t['mae'] = None; continue
        hi, lo = float(r['high'].max()), float(r['low'].min())
        hi, lo = max(hi, t['entry'], t['exit']), min(lo, t['entry'], t['exit'])
        t['mfe'] = round(hi - t['entry'] if t['side'] == 'buy' else t['entry'] - lo, 2)
        t['mae'] = round(t['entry'] - lo if t['side'] == 'buy' else hi - t['entry'], 2)

    first = min(t['t0'] for t in trades) - shift - 5*86400
    bars = {}
    for k, tf in TFS.items():
        rates = m.copy_rates_range(sym, tf, utc(first), utc(time.time() + 86400))
        bars[k] = [[int(r['time']) + shift, float(r['open']), float(r['high']), float(r['low']), float(r['close'])] for r in (rates if rates is not None else [])]

    info = {'login': acct.login, 'server': acct.server, 'balance': acct.balance, 'equity': acct.equity,
            'csize': csize, 'sym': sym, 'first': min(t['t0'] for t in trades)}
    print(f'{acct.login} {sym} bars={ {k: len(v) for k, v in bars.items()} } trades={len(trades)} srv_off=UTC{srv_off_h:+d}')
    return info, trades, cash, bars


def stamp(state, built=None):
    """หน้าเว็บ poll ไฟล์นี้ -> โชว์ 'กำลังอัปเดต' และ reload เองเมื่อข้อมูลใหม่ (built เปลี่ยน)"""
    if built is None:
        f = HERE / 'stamp.json'
        built = json.loads(f.read_text(encoding='utf-8'))['built'] if f.exists() else ''
    (HERE / 'stamp.js').write_text('window.STAMP=%s;' % json.dumps({'state': state, 'built': built}), encoding='utf-8')
    return built


def load_cache(login):
    f = CACHE / f'{login}.json'
    if FULL or not login or not f.exists():
        return None
    info, trades, cash, bars = json.loads(f.read_text(encoding='utf-8'))
    closed = info['balance'] == 0 and info['equity'] == 0 and not any(t.get('open') for t in trades)
    return (info, trades, cash, bars) if closed else None


stamp('building')
DONE = []
atexit.register(lambda: DONE or stamp('error'))  # พังกลางทาง -> หน้าเว็บเลิกโชว์ 'กำลังอัปเดต'
if not m.initialize():
    stamp('error')
    fail(f'เปิด MT5 ไม่ได้ / cannot start MT5: {m.last_error()}')

# สลับไปทีละบัญชี (ใช้รหัสที่ MT5 จำไว้) แล้วสลับกลับบัญชีเดิมตอนจบ
orig = m.account_info()
# offset สำรองสำหรับบัญชีที่ tick ค้าง: วัดจาก symbol ใน Market Watch ของบัญชีที่เปิดอยู่ตอนนี้
FALLBACK_OFF = next((o for s in m.symbols_get() or [] if s.visible for o in [srv_offset(s.name)] if o is not None),
                    int(cfg.get('server_utc', 2)))
results, skipped = [], []
try:
    for a in ACCOUNTS:
        login, server = int(a.get('login') or 0), a.get('server') or ''
        hit = load_cache(login)
        if hit:
            hit[0]['name'] = a.get('name') or ''
            results.append(hit); print(f'{login} cached (closed account)'); continue
        cur = m.account_info()
        if login and (cur is None or cur.login != login) and not m.login(login, server=server):
            skipped.append(f'{login}: สลับบัญชีไม่ได้ {m.last_error()} — login ใน MT5 แล้วติ๊ก Save password ก่อน'); continue
        try:
            info, trades, cash, bars = pull(login, server, a.get('symbol') or '')
        except Skip as e:
            skipped.append(f'{login}: {e}'); continue
        info['name'] = a.get('name') or ''
        results.append((info, trades, cash, bars))
        if login:
            CACHE.mkdir(exist_ok=True)
            (CACHE / f'{login}.json').write_text(json.dumps([info, trades, cash, bars], separators=(',', ':')), encoding='utf-8')
finally:
    if orig is not None and (m.account_info() is None or m.account_info().login != orig.login):
        m.login(orig.login, server=orig.server)
        print(f'switched back to {orig.login}')

if not results:
    stamp('error')
    fail('ดึงไม่ได้สักบัญชี — ไม่เขียนทับหน้าเว็บ / no account could be read, aborting\n' + '\n'.join(skipped))

# บัญชีเรียงเก่า -> ใหม่, แท่งเทียนช่วงที่ทับกันใช้ของบัญชีที่ใหม่กว่า (symbol ต่างกันเล็กน้อย เช่น Gold# vs GOLD24-7#)
results.sort(key=lambda r: r[0]['first'])
bars = {}
for k in TFS:
    merged = {}
    for _, _, _, b in results:
        merged.update({row[0]: row for row in b[k]})
    bars[k] = [merged[t] for t in sorted(merged)]
trades = sorted((t for r in results for t in r[1]), key=lambda t: t['t0'])
cash = sorted((c for r in results for c in r[2]), key=lambda c: c['t'])
accts = [r[0] for r in results]

# ข้อมูลแยกไฟล์ data.js -> หน้าเว็บเปิดได้ทันทีด้วยข้อมูลรอบก่อน แล้ว reload เองเมื่อรอบนี้เสร็จ
built = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
(HERE / 'data.js').write_text('const BARS=%s;const RAW_TRADES=%s;const RAW_CASH=%s;const ACCTS=%s;const META=%s;const BUILT=%s;' % (
    json.dumps(bars, separators=(',', ':')), json.dumps(trades), json.dumps(cash), json.dumps(accts),
    json.dumps({'maxLot': MAX_LOT, 'tz': TZ_OUT}), json.dumps(built)), encoding='utf-8')
out = HERE / 'trade_chart.html'
out.write_text((HERE / 'template.html').read_text(encoding='utf-8'), encoding='utf-8-sig')
(HERE / 'stamp.json').write_text(json.dumps({'built': built}), encoding='utf-8')
stamp('done', built); DONE.append(1)
print(f'{len(accts)} accounts, {len(trades)} trades -> {out}')
if skipped:
    msg = 'ข้ามบางบัญชี / skipped:\n' + '\n'.join(skipped)
    print(msg)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, 'MT5 Trade Chart', 0x30)
    except Exception:
        pass
