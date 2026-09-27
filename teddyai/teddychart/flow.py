"""Deterministic observation-only futures features; no I/O or trading decisions.

Windows use completed event-time seconds. Unknown aggressors remain unknown.
Live and replay trades enter through Tape.append. OFI requires raw ordered BBO
events; sampled replay depth deliberately does not claim OFI equivalence.
"""
import collections
import math
import threading


class Flow:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset(None)

    def reset(self, anchor):
        with self.lock:
            self.anchor = anchor
            self.seconds = collections.OrderedDict()
            self.cvd = 0
            self.last = None
            self.first = None
            self.dropped = 0
            self.bbo = None
            self.ofi = collections.OrderedDict()
            self.depth = None

    def trade(self, ts, price, size, side):
        if not all(math.isfinite(x) for x in (ts, price, size)) or size <= 0 or price <= 0:
            return
        with self.lock:
            if self.last is not None and ts < self.last:
                self.dropped += 1
                return
            self.last = ts
            if self.first is None: self.first = ts
            sec = int(ts // 1000)
            b = self.seconds.setdefault(sec, dict(buy=0, sell=0, unknown=0, count=0,
                                                 first=price, last=price, hi=price, lo=price))
            b['buy' if side == 1 else 'sell' if side == 2 else 'unknown'] += size
            b['count'] += 1
            b['last'] = price
            b['hi'], b['lo'] = max(b['hi'], price), min(b['lo'], price)
            self.cvd += size if side == 1 else -size if side == 2 else 0
            while self.seconds and next(iter(self.seconds)) < sec - 301:
                self.seconds.popitem(last=False)

    def book(self, ts, bids, asks):
        """Call only before UI coalescing, for raw ordered complete BBO updates."""
        with self.lock:
            if not bids or not asks:
                self.bbo = None
                self.depth = None
                return
            bp, bs = bids[0]; ap, az = asks[0]
            if bp <= 0 or ap <= bp or bs <= 0 or az <= 0:
                self.bbo = None
                self.depth = None
                return
            if self.bbo and ts < self.bbo[0]:
                self.dropped += 1
                return
            if self.bbo and ts - self.bbo[0] <= 30000:
                _, p, q, a, z = self.bbo
                e = (bs if bp >= p else 0) - (q if bp <= p else 0)
                e -= (az if ap <= a else 0) - (z if ap >= a else 0)
                sec = int(ts // 1000)
                self.ofi[sec] = self.ofi.get(sec, 0) + e
            self.bbo = ts, bp, bs, ap, az
            self.depth = dict(ts_ms=ts, spread_ticks=(ap-bp)/0.25,
                              bid_size=sum(s for _, s in bids), ask_size=sum(s for _, s in asks))
            while self.ofi and next(iter(self.ofi)) < int(ts // 1000) - 301:
                self.ofi.popitem(last=False)

    def snapshot(self, now):
        with self.lock:
            end = int(now // 1000)
            windows = {}
            for length in (10, 60, 300):
                rows = [v for s, v in self.seconds.items() if end-length <= s < end]
                buy, sell, unknown, count = (sum(r[k] for r in rows) for k in ('buy','sell','unknown','count'))
                classified, total = buy+sell, buy+sell+unknown
                windows[str(length)] = dict(buy=buy, sell=sell, unknown=unknown, delta=buy-sell,
                    count=count, coverage=classified/total if total else None,
                    delta_fraction=(buy-sell)/classified if classified else None,
                    displacement=rows[-1]['last']-rows[0]['first'] if rows else None,
                    range=max(r['hi'] for r in rows)-min(r['lo'] for r in rows) if rows else None,
                    ofi=sum(v for s,v in self.ofi.items() if end-length <= s < end) if self.bbo and now-self.bbo[0] <= 30000 else None)
            return dict(t='flow', version=1, end_ms=end*1000, anchor_ms=self.anchor,
                        first_trade_ms=self.first, last_trade_ms=self.last, age_ms=max(0,now-self.last) if self.last else None,
                        cvd=self.cvd, out_of_order_dropped=self.dropped, windows=windows,
                        depth=self.depth, observation_only=True)
