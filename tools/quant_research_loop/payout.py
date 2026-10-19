"""Deterministic Topstep 50K Standard screening replay (V1).

Reads a verified trade ledger CSV (columns: date, net_usd, ...), aggregates to
daily net PnL in chronological order, and replays:

  combine (start $50,000, MLL trailing $2,000, profit target, 55% consistency)
  -> activation (if affordable simulation budget allows; costs recorded)
  -> XFA from $0 (MLL trailing, locks $0) with 5x$150-day payout eligibility,
     50%-of-balance cap, 90/10 split, explicit withdrawal fee
  -> post-withdrawal survival to ledger end.

SCREENING ONLY. Closed-trade ledgers carry no intratrade open-PnL path, so
intraday breach certification is impossible: intraday_breach_status is always
UNKNOWN and no path may be labeled payout-feasible. Evaluation profits never
carry into the funded balance. Costs already inside ledger net_usd are never
deducted twice (only subscription/activation/withdrawal-fee layers added here).

net_cash = cash_received_after_split_and_fees
           - subscription - activation - reset_costs - operating_costs
"""
from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Rules50K:
    combine_start: float = 50000.0
    mll_buffer: float = 2000.0
    profit_target: float = 3000.0
    best_day_cap: float = 1650.0  # 55% of target
    min_pass_days: int = 2
    combine_max_minis: int = 5
    xfa_win_days: int = 5
    xfa_win_min: float = 150.0
    payout_cap: float = 2000.0  # base 50K Standard per-request cap
    payout_ratio: float = 0.5  # max 50% of balance per request
    split_trader: float = 0.9  # trader keeps 90%
    min_payout: float = 125.0
    subscription_monthly: float = 49.0
    activation_fee: float = 149.0
    withdrawal_fee: float = 0.0  # explicit scenario; Aeropay-like default
    rules_hash: str = "UNPINNED"


def ledger_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_daily_nets(ledger_csv: str) -> tuple[list[tuple[str, float, int]], int]:
    """Return [(date, net, n_trades)] sorted by date, plus total trade count."""
    daily: dict[str, list[float]] = {}
    n = 0
    with open(ledger_csv, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        if "date" not in (r.fieldnames or []) or "net_usd" not in (r.fieldnames or []):
            raise ValueError("ledger must have date,net_usd columns")
        for row in r:
            daily.setdefault(row["date"], []).append(float(row["net_usd"]))
            n += 1
    return [(d, round(sum(v), 2), len(v)) for d, v in sorted(daily.items())], n


def replay(ledger_csv: str, rules: Rules50K, *, label: str = "") -> dict:
    daily, n_trades = read_daily_nets(ledger_csv)
    res: dict = {
        "label": label,
        "ledger": ledger_csv,
        "ledger_sha256": ledger_sha256(ledger_csv),
        "rules_hash": rules.rules_hash,
        "n_trades": n_trades,
        "n_days": len(daily),
        "date_first": daily[0][0] if daily else None,
        "date_last": daily[-1][0] if daily else None,
        "screening_only": True,
        "intraday_breach_status": "UNKNOWN",
        "certified_payout_feasible": False,
    }
    if not daily:
        res.update(status="NO_TRADES", combine="NOT_EVALUABLE", xfa="NOT_RUN",
                   payouts=[], costs={}, net_cash=0.0)
        return res

    # ---- Combine ----
    bal = rules.combine_start
    mll = bal - rules.mll_buffer
    best_day = 0.0
    target = rules.profit_target
    total = 0.0
    days_traded = 0
    passed_on: str | None = None
    failed_on: str | None = None
    for d, net, _ in daily:
        bal = round(bal + net, 2)
        total = round(total + net, 2)
        days_traded += 1
        if net > best_day:
            best_day = round(net, 2)
        if best_day > rules.best_day_cap:
            target = round(best_day / 0.55, 2)  # raised target, disclosed
        mll = max(mll, bal - rules.mll_buffer)
        if mll > rules.combine_start:
            mll = rules.combine_start  # locks at starting balance
        if bal <= mll:  # EOD-certified breach only; intraday stays UNKNOWN
            failed_on = d
            break
        if (passed_on is None and days_traded >= rules.min_pass_days
                and total >= target and best_day <= 0.55 * target):
            passed_on = d
    combine = {
        "end_balance": bal, "end_mll": round(mll, 2), "total_net": total,
        "best_day": best_day, "profit_target_final": target,
        "passed_on": passed_on, "failed_on": failed_on,
    }
    res["combine"] = combine
    if failed_on is not None or passed_on is None:
        # One purchased attempt default: terminate, never auto-buy another.
        months = max(1, (len(daily) + 29) // 30)
        sub = round(months * rules.subscription_monthly, 2)
        res.update(status="COMBINE_FAIL" if failed_on else "COMBINE_NO_PASS",
                   xfa="NOT_RUN", payouts=[],
                   costs={"subscription": sub, "activation": 0.0,
                          "withdrawal_fees": 0.0, "resets": 0.0},
                   net_cash=round(-sub, 2))
        return res

    # ---- Activation + XFA ----
    months = max(1, (len(daily) + 29) // 30)
    sub = round(months * rules.subscription_monthly, 2)
    act = rules.activation_fee
    xbal = 0.0
    xmll = -rules.mll_buffer
    started = False
    win_days = 0
    window_profit = 0.0
    payouts: list[dict] = []
    first_payout = True
    xfa_failed_on: str | None = None
    started_idx = next(i for i, (d, _, _) in enumerate(daily) if d >= passed_on) + 1
    for d, net, _ in daily[started_idx:]:
        started = True
        xbal = round(xbal + net, 2)
        window_profit = round(window_profit + net, 2)
        xmll = max(xmll, xbal - rules.mll_buffer)
        if xmll > 0.0:
            xmll = 0.0  # locks at $0
        if xbal <= xmll:
            xfa_failed_on = d
            break
        if net >= rules.xfa_win_min:
            win_days += 1
        eligible = (win_days >= rules.xfa_win_days
                    and (first_payout or window_profit >= 0.01))
        if eligible and xbal >= rules.min_payout:
            requested = round(min(rules.payout_ratio * xbal, rules.payout_cap), 2)
            if requested >= rules.min_payout:
                approved = requested  # approval assumed; disclosed
                fee = rules.withdrawal_fee
                cash = round(approved * rules.split_trader - fee, 2)
                payouts.append({"date": d, "balance_before": xbal,
                                "requested": requested, "approved": approved,
                                "fee": fee, "cash_received": cash})
                xbal = round(xbal - approved, 2)
                xmll = 0.0  # MLL set to $0 after payout
                win_days = 0
                window_profit = 0.0
                first_payout = False
    wfees = round(sum(p["fee"] for p in payouts), 2)
    cash_in = round(sum(p["cash_received"] for p in payouts), 2)
    res.update(
        status="XFA_COMPLETE" if xfa_failed_on is None and started else
        ("XFA_FAILED" if xfa_failed_on else "XFA_NO_TRADING_DAYS"),
        xfa={"failed_on": xfa_failed_on,
             "remaining_balance": xbal, "remaining_mll": round(xmll, 2)},
        payouts=payouts,
        costs={"subscription": sub, "activation": act,
               "withdrawal_fees": wfees, "resets": 0.0},
        net_cash=round(cash_in - sub - act - wfees, 2),
        cash_received=cash_in,
    )
    return res
