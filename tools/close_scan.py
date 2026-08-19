#!/opt/homebrew/bin/python3
"""Scan harness with the production fill model.

The harness applies five fill rules:
1. The entry fills at the trigger bar close.
2. An exit fills at the first minute close at or beyond the target or the
   stop, at that close.
3. The unit goes flat at the close of the first bar with session offset
   >= 385 (total 390 minus 5).
4. The drive or leg measure needs the exact bar at the session open and at
   minute confirm-1.
5. The ATR is the mean gap-inclusive true range over the 14 prior complete
   sessions; with fewer than 14 it is None.

The harness drops bars with volume <= 0 and excludes sessions on the current
date. It reads bars and live results only through the dashboard API. It never
opens the database file. The reported net subtracts a 0.10 percentage-point
round-trip cost. Calibrate against the live replays before you trust a new
configuration.
"""

import argparse
import json
import statistics
import sys
import urllib.request
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
REPO_ROOT = Path(__file__).resolve().parent.parent
COST = 0.10
DEFAULT_SPLIT = date(2026, 7, 31)
DEFAULT_PORT = 8790


def fetch_json(url):
    """Read one JSON document from the dashboard API."""
    with urllib.request.urlopen(url) as resp:
        return json.load(resp)


def load_tickers():
    """Read the configured ticker list from config/config.json."""
    config = json.loads((REPO_ROOT / "config" / "config.json").read_text())
    return config["tickers"]


def fetch_bars(tickers, port):
    """Fetch the HISTORY bar range for each ticker."""
    bars_by_ticker = {}
    for t in tickers:
        url = f"http://127.0.0.1:{port}/api/bars?ticker={t}&range=HISTORY"
        bars_by_ticker[t] = fetch_json(url)["bars"]
    return bars_by_ticker


def build_sessions(bars_by_ticker, today):
    """Group regular-session bars into (ticker, date) -> offset -> bar."""
    sessions = {}
    for t, bars in bars_by_ticker.items():
        for b in bars:
            if b["volume"] <= 0:
                continue
            dt = datetime.fromtimestamp(b["ts"], ET)
            m = dt.hour * 60 + dt.minute
            if m < 570 or m >= 960 or dt.date() >= today:
                continue
            sessions.setdefault((t, dt.date()), {})[m - 570] = b
    return sessions


def compute_atr(sessions):
    """Mean gap-inclusive true range percent over the 14 prior sessions."""
    dates_by_ticker = defaultdict(list)
    for (t, d) in sorted(sessions):
        dates_by_ticker[t].append(d)
    atr_pct = {}
    for t, ds in dates_by_ticker.items():
        closes = {}
        for d in ds:
            offs = sessions[(t, d)]
            closes[d] = offs[max(offs)]["close"]
        for i, d in enumerate(ds):
            vals = []
            for j in range(max(0, i - 14), i):
                pd = ds[j]
                if j == 0:
                    continue
                prev_close = closes[ds[j - 1]]
                offs = sessions[(t, pd)]
                if len(offs) < 100 or prev_close <= 0:
                    continue
                hi = max(b["high"] for b in offs.values())
                lo = min(b["low"] for b in offs.values())
                tr = max(hi, prev_close) - min(lo, prev_close)
                vals.append(tr / prev_close * 100.0)
            atr_pct[(t, d)] = statistics.mean(vals) if len(vals) >= 14 else None
    return atr_pct


def drive_pct(offs, confirm_minutes):
    """Exact-bar open-to-confirm move in percent, or None."""
    if 0 not in offs or (confirm_minutes - 1) not in offs:
        return None
    op = offs[0]["open"]
    if op <= 0:
        return None
    return (offs[confirm_minutes - 1]["close"] / op - 1.0) * 100.0


def close_sim(offs, entry_off, direction, entry, target, stop):
    """Close-based exits, filled at the crossing close. Returns percent."""
    last = entry
    for off in sorted(o for o in offs if o > entry_off):
        c = offs[off]["close"]
        flat = off >= 385
        if direction == 1 and (c <= stop or c >= target or flat):
            return (c - entry) / entry * 100.0
        if direction == -1 and (c >= stop or c <= target or flat):
            return (c - entry) / entry * -100.0
        last = c
    return direction * (last - entry) / entry * 100.0


def gates_pass(dv, a, min_drive_pct, drive_atr_frac):
    """Apply the absolute floor and the ATR-scaled floor to the drive."""
    return (dv is not None and dv != 0.0
            and abs(dv) >= max(min_drive_pct, drive_atr_frac * a))


def run_immediate(sessions, atr_pct, cm, mmin, mmax, min_drive_pct,
                  atr_frac, tf, sf):
    """Enter at the first window bar close, in the drive direction."""
    out = []
    for (t, d), offs in sessions.items():
        a = atr_pct.get((t, d))
        if a is None or len(offs) < 200:
            continue
        dv = drive_pct(offs, cm)
        if not gates_pass(dv, a, min_drive_pct, atr_frac):
            continue
        direction = 1 if dv > 0 else -1
        for off in sorted(o for o in offs if mmin <= o < mmax):
            e = offs[off]["close"]
            move = abs(dv) / 100.0 * e
            out.append((close_sim(offs, off, direction, e,
                                  e + direction * tf * move,
                                  e - direction * sf * move), d, t))
            break
    return out


def run_break(sessions, atr_pct, cm, mmin, mmax, min_drive_pct,
              atr_frac, tf, sf):
    """Enter on the first close beyond the session extreme, leg direction."""
    out = []
    for (t, d), offs in sessions.items():
        a = atr_pct.get((t, d))
        if a is None or len(offs) < 200:
            continue
        dv = drive_pct(offs, cm)
        if not gates_pass(dv, a, min_drive_pct, atr_frac):
            continue
        direction = 1 if dv > 0 else -1
        ordered = sorted(offs)
        for i, off in enumerate(ordered):
            if off < mmin or off >= mmax or i == 0:
                continue
            prior = [offs[o] for o in ordered[:i]]
            c = offs[off]["close"]
            broke = (c > max(p["high"] for p in prior) if direction == 1
                     else c < min(p["low"] for p in prior))
            if not broke:
                continue
            e = c
            move = abs(dv) / 100.0 * e
            out.append((close_sim(offs, off, direction, e,
                                  e + direction * tf * move,
                                  e - direction * sf * move), d, t))
            break
    return out


def report(label, trades, be, split=DEFAULT_SPLIT):
    """Print one result row and return the trades."""
    if not trades:
        print(f"{label:>52}  n=0")
        return trades
    rs = [r for r, _, _ in trades]
    n = len(rs)
    w = sum(r > 0 for r in rs)
    disc = [r for r, d, _ in trades if d <= split]
    fwd = [r for r, d, _ in trades if d > split]
    dtxt = (f"{100*sum(r>0 for r in disc)/len(disc):.0f}%/{len(disc)}"
            if disc else "-")
    ftxt = (f"{100*sum(r>0 for r in fwd)/len(fwd):.0f}%/{len(fwd)}"
            if fwd else "-")
    wdc = sum(r for r, _, t in trades if t == "WDC")
    print(f"{label:>52} n={n:>3} win={100*w/n:5.1f}% (be={be:.1f}%) "
          f"gross={statistics.mean(rs):+.3f} net={statistics.mean(rs)-COST:+.3f} "
          f"wdc={wdc:+.1f} disc={dtxt} fwd={ftxt}")
    return trades


def breakeven(tf, sf):
    """Win rate in percent where the gross expectation is zero."""
    return 100.0 * sf / (tf + sf)


def load_data(port):
    """Fetch bars and build the session and ATR tables."""
    today = datetime.now(ET).date()
    sessions = build_sessions(fetch_bars(load_tickers(), port), today)
    return sessions, compute_atr(sessions)


def cmd_calibrate(args):
    """Replay the two live reference configurations next to live results."""
    sessions, atr_pct = load_data(args.dash_port)
    print("CALIBRATION against live replays")
    report("opening_drive cm15 w15-20 0.5/0.2 tf.25 sf1.0",
           run_immediate(sessions, atr_pct, 15, 15, 20, 0.5, 0.2, 0.25, 1.0),
           breakeven(0.25, 1.0))
    report("second_leg cm30 w35-90 0.5/0.2 tf.15 sf.75",
           run_break(sessions, atr_pct, 30, 35, 90, 0.5, 0.2, 0.15, 0.75),
           breakeven(0.15, 0.75))
    live = fetch_json(f"http://127.0.0.1:{args.dash_port}/api/algorithms")
    stats = {a["name"]: a.get("stats") or {}
             for a in live.get("algorithms", [])}
    for name in ("opening_drive", "second_leg"):
        s = stats.get(name)
        label = f"live {name}"
        if s and s.get("win_rate") is not None:
            print(f"{label:>52} win={s['win_rate']:5.1f}% "
                  f"closed_units={s['closed_units']}")
        else:
            print(f"{label:>52} not live")
    return 0


def cmd_run(args):
    """Scan one configuration and print one result row."""
    sessions, atr_pct = load_data(args.dash_port)
    runner = run_immediate if args.entry == "immediate" else run_break
    trades = runner(sessions, atr_pct, args.confirm_minutes,
                    args.minute_min, args.minute_max, args.min_drive_pct,
                    args.drive_atr_frac, args.target_frac, args.stop_frac)
    label = (f"{args.entry} cm{args.confirm_minutes} "
             f"w{args.minute_min}-{args.minute_max} "
             f"{args.min_drive_pct}/{args.drive_atr_frac} "
             f"tf{args.target_frac} sf{args.stop_frac}")
    report(label, trades, breakeven(args.target_frac, args.stop_frac),
           split=args.split_date)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--dash-port", type=int, default=DEFAULT_PORT,
                        help="dashboard API port (default 8790)")
    sub = parser.add_subparsers(dest="command", required=True)

    cal = sub.add_parser("calibrate", parents=[shared],
                         help="replay the live reference configurations")
    cal.set_defaults(func=cmd_calibrate)

    run = sub.add_parser("run", parents=[shared],
                         help="scan one configuration")
    run.add_argument("--entry", choices=["immediate", "break"], required=True)
    run.add_argument("--confirm-minutes", type=int, required=True)
    run.add_argument("--minute-min", type=int, required=True)
    run.add_argument("--minute-max", type=int, required=True)
    run.add_argument("--min-drive-pct", type=float, default=0.5)
    run.add_argument("--drive-atr-frac", type=float, default=0.2)
    run.add_argument("--target-frac", type=float, required=True)
    run.add_argument("--stop-frac", type=float, required=True)
    run.add_argument("--split-date", type=date.fromisoformat,
                     default=DEFAULT_SPLIT,
                     help="discovery/forward split date (default 2026-07-31)")
    run.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
