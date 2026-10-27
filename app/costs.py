"""Versioned estimates for intraday NSE options; not a contract-note calculator.

angel_nse_2026_10: Angel tariff checked 15 October 2026; NSE STT effective 1 April 2026.
upstox_nse_2026_10: Upstox tariff page checked 27 October 2026 (INR 20/order, same NSE statutory
rates; GST on brokerage, exchange and IPFT charges).
No expiry exercise, broker auto-square-off, subscriptions or funding charges.
Per-order rounding may differ from broker daily aggregation.
"""
from decimal import Decimal, ROUND_HALF_UP

# Itemized fee profile -> the data provider (broker) it models.
ITEMIZED = {"angel_nse_2026_10": "angel", "upstox_nse_2026_10": "upstox"}


def order_costs(price, qty, config, side="BUY"):
    if side not in {"BUY", "SELL"}:
        raise ValueError("Order side must be BUY or SELL")
    if qty <= 0:
        return {"model": config.paper_fee_model, "total": 0.0}
    turnover = Decimal(str(price)) * Decimal(str(qty))
    brokerage = Decimal(str(config.paper_fee_per_order))
    if config.paper_fee_model == "legacy":
        parts = {"brokerage": brokerage,
                 "estimated_other_charges": turnover * Decimal(str(config.paper_fee_bps)) / 10000}
    elif config.paper_fee_model in ITEMIZED:
        # Rates are fractions of premium turnover, not percentages.
        exchange = turnover * Decimal("0.000355299")
        sebi = turnover * Decimal("0.000001")
        ipft = turnover * Decimal("0.000000001")
        parts = {"brokerage": brokerage, "exchange": exchange, "sebi": sebi, "ipft": ipft,
                 "stt": turnover * Decimal("0.0015") if side == "SELL" else Decimal(0),
                 "stamp_duty": turnover * Decimal("0.00003") if side == "BUY" else Decimal(0),
                 "gst": (brokerage + exchange + ipft + (sebi if ITEMIZED[config.paper_fee_model] == "angel" else 0))
                        * Decimal("0.18")}
    else:
        raise ValueError("Unknown fee model")
    rounded = {k: v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for k, v in parts.items()}
    total = sum(rounded.values())
    # Preserve legacy rounding for existing recorded experiments.
    if config.paper_fee_model == "legacy":
        total = Decimal(str(round(config.paper_fee_per_order + float(turnover) * config.paper_fee_bps / 10000, 2)))
    return {"model": config.paper_fee_model, "side": side, "premium_turnover": float(turnover),
            **{k: float(v) for k, v in rounded.items()}, "total": float(total)}
