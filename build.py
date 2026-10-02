"""ดึงแท่งเทียนหลาย TF + ประวัติไม้ + ฝาก/ถอน + MFE/MAE จาก MT5 -> trade_chart.html
Pull multi-TF candles, trade history, cash flows and MFE/MAE from MT5 -> trade_chart.html

ตั้งค่าใน config.json (ก๊อปจาก config.example.json) / settings live in config.json
"""
import MetaTrader5 as m, datetime as dt, json, collections, pathlib, time

HERE = pathlib.Path(__file__).parent
CFG_FILE = HERE / 'config.json'
cfg = json.loads(CFG_FILE.read_text(encoding='utf-8')) if CFG_FILE.exists() else {}
LOGIN = int(cfg.get('login') or 0)        # 0 = ใช้บัญชีที่ login อยู่ใน MT5 ตอนนี้ / use the account currently logged in
SERVER = cfg.get('server') or ''
SYM = cfg.get('symbol') or ''             # ว่าง = หาเองจากไม้ทอง / empty = auto-detect gold symbol
TZ_OUT = float(cfg.get('utc_offset', 7))  # เวลาที่แสดงบนกราฟ / display timezone
MAX_LOT = float(cfg.get('max_lot', 0.01)) # lot เกินนี้ติดธง / flag trades above this lot


def fail(msg):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, 'MT5 Trade Chart', 0x10)
    except Exception:
        pass
    raise SystemExit(msg)


if not m.initialize():
    fail(f'เปิด MT5 ไม่ได้ / cannot start MT5: {m.last_error()}')

# ถ้าตั้ง login ไว้แต่ MT5 ค้างบัญชีอื่น -> สลับไปก่อน (ใช้รหัสที่ MT5 จำไว้) แล้วสลับกลับตอนจบ
orig = m.account_info()
switched = bool(LOGIN) and (orig is None or orig.login != LOGIN)
if switched and not m.login(LOGIN, server=SERVER):
    fail(f'สลับเข้าบัญชี {LOGIN} ไม่ได้: {m.last_error()}\n'
         f'login บัญชีนี้ใน MT5 แล้วติ๊ก Save password ก่อน\n'
         f'Cannot switch to {LOGIN}: log in once in MT5 with "Save password" ticked.')

try:
    acct = m.account_info()
    if acct is None or (LOGIN and acct.login != LOGIN):
        fail(f'ยังไม่ได้อยู่บัญชี {LOGIN} — ไม่เขียนทับหน้าเว็บ / not on account {LOGIN}, aborting')

    H0, H1 = dt.datetime(2000, 1, 1, tzinfo=dt.UTC), dt.datetime.now(dt.UTC) + dt.timedelta(days=2)
    # หลัง login ประวัติ deal ซิงก์ตามมาทีหลัง (~1 วิ) -> รอจนไม้ที่เปิดอยู่มี deal ตอนเปิดครบ
    open_ids = {p.identifier for p in m.positions_get() or []}
    for _ in range(30):
        all_deals = m.history_deals_get(H0, H1) or ()
        if open_ids <= {d.position_id for d in all_deals if d.entry == 0}:
            break
        time.sleep(0.5)
    if not SYM:  # หา symbol ทองที่เทรดเยอะสุด
        cnt = collections.Counter(d.symbol for d in all_deals if d.symbol and ('XAU' in d.symbol.upper() or 'GOLD' in d.symbol.upper()))
        if not cnt:
            fail('ไม่เจอไม้ทองในบัญชีนี้ ใส่ "symbol" ใน config.json / no gold trades found, set "symbol" in config.json')
        SYM = cnt.most_common(1)[0][0]
    if not m.symbol_select(SYM, True) or m.symbol_info_tick(SYM) is None:
        fail(f'ไม่เจอ symbol {SYM} ใน MT5 / symbol {SYM} not found')

    # หา offset เวลาเซิร์ฟเวอร์ (tick.time เป็นเวลาเซิร์ฟเวอร์)
    srv_off_h = round((m.symbol_info_tick(SYM).time - time.time()) / 3600)
    shift = int((TZ_OUT - srv_off_h) * 3600)

    deals = [d for d in all_deals if d.symbol == SYM]
    # ฝาก/ถอน/โอน (deal type 2 = balance)
    cash = [{'t': d.time + shift, 'amt': d.profit, 'note': d.comment} for d in all_deals if d.type == 2]
    csize = m.symbol_info(SYM).trade_contract_size
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
        t = {'id': pid, 'side': side, 'vol': round(vol, 2), 'entry': ep,
             't0': min(x.time for x in p['in']) + shift,
             'sl': (o.sl or None) if o else None, 'tp': (o.tp or None) if o else None}
        if p['out']:
            xp, _ = vwap(p['out'])
            t.update(exit=xp, t1=max(x.time for x in p['out']) + shift, open=False,
                     profit=round(sum(x.profit + x.swap + x.commission + x.fee for x in p['out'] + p['in']), 2),
                     comment=p['out'][-1].comment)
        trades.append(t)

    for op in m.positions_get(symbol=SYM) or []:
        if not any(t['id'] == op.identifier for t in trades):  # ประวัติยังไม่มา -> สร้างจาก position ตรงๆ
            trades.append({'id': op.identifier, 'side': 'buy' if op.type == 0 else 'sell', 'vol': op.volume,
                           'entry': op.price_open, 't0': op.time + shift, 'sl': None, 'tp': None})
        for t in trades:
            if t['id'] == op.identifier:
                t.update(exit=op.price_current, t1=int(time.time()) + srv_off_h*3600 + shift, open=True,
                         profit=round(op.profit + op.swap, 2), sl=op.sl or None, tp=op.tp or None, comment='OPEN')
    trades = [t for t in trades if 'exit' in t]
    if not trades:
        fail(f'ไม่มีไม้ {SYM} ในบัญชีนี้ / no {SYM} trades in this account')
    for t in trades:
        d = 1 if t['side'] == 'buy' else -1
        t['pct'] = round((t['exit'] - t['entry']) / t['entry'] * 100 * d, 2)

    # MFE/MAE จากแท่ง M1 ระหว่างถือ (หน่วยราคา, บวก = ไปทางที่ได้เปรียบ)
    utc = lambda ts: dt.datetime.fromtimestamp(ts, dt.UTC)  # ต้อง tz-aware ไม่งั้น lib ตีเป็นเวลาเครื่อง
    for t in trades:
        a, b = t['t0'] - shift, t['t1'] - shift
        r = m.copy_rates_range(SYM, m.TIMEFRAME_M1, utc(a // 60 * 60), utc(b))
        if r is None or len(r) == 0:
            r = m.copy_rates_range(SYM, m.TIMEFRAME_M15, utc(a // 900 * 900), utc(b))
        if r is None or len(r) == 0:
            t['mfe'] = t['mae'] = None; continue
        hi, lo = float(r['high'].max()), float(r['low'].min())
        hi, lo = max(hi, t['entry'], t['exit']), min(lo, t['entry'], t['exit'])
        t['mfe'] = round(hi - t['entry'] if t['side'] == 'buy' else t['entry'] - lo, 2)
        t['mae'] = round(t['entry'] - lo if t['side'] == 'buy' else hi - t['entry'], 2)

    first = min(t['t0'] for t in trades) - shift - 5*86400
    TFS = {'M15': m.TIMEFRAME_M15, 'H1': m.TIMEFRAME_H1, 'H4': m.TIMEFRAME_H4, 'D1': m.TIMEFRAME_D1}
    bars = {}
    for k, tf in TFS.items():
        rates = m.copy_rates_range(SYM, tf, utc(first), utc(time.time() + 86400))
        bars[k] = [[int(r['time']) + shift, float(r['open']), float(r['high']), float(r['low']), float(r['close'])] for r in (rates if rates is not None else [])]

    trades.sort(key=lambda t: t['t0'])
    html = (HERE / 'template.html').read_text(encoding='utf-8')
    html = html.replace('/*DATA*/', 'const BARS=%s;const TRADES=%s;const CASH=%s;const ACCT=%s;const BUILT=%s;' % (
        json.dumps(bars, separators=(',', ':')), json.dumps(trades), json.dumps(cash),
        json.dumps({'login': acct.login, 'balance': acct.balance, 'equity': acct.equity, 'csize': csize,
                    'maxLot': MAX_LOT, 'sym': SYM, 'tz': TZ_OUT}),
        json.dumps(dt.datetime.now().strftime('%Y-%m-%d %H:%M'))))
    out = HERE / 'trade_chart.html'
    out.write_text(html, encoding='utf-8-sig')
    print(f'{SYM} bars={ {k: len(v) for k, v in bars.items()} } trades={len(trades)} srv_off=UTC{srv_off_h:+d} -> {out}')
finally:
    if switched and orig is not None:
        m.login(orig.login, server=orig.server)
        print(f'switched back to {orig.login}')
