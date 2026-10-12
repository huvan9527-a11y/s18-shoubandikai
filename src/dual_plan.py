"""S18 dual-account signal ledger. No fabricated fills or broker orders."""
import argparse
import datetime as dt
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INITIAL_CASH = 200000.0


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_plan(report, next_day, second_day):
    """Plan only; execution requires timestamped market evidence."""
    if report.get("status") != "scanned":
        raise ValueError("Incomplete scan cannot generate trades")
    day = dt.date.fromisoformat(report["date"])
    next_date = dt.date.fromisoformat(next_day)
    second_date = dt.date.fromisoformat(second_day)
    if not (day < next_date < second_date):
        raise ValueError("Require consecutive ordered trading sessions")
    selected = report.get("opening_candidates", [])[:2]
    def orders(buy_date, sell_date, mode):
        return [dict(code=c["code"], name=c["name"], component=c["component"],
                     signal_date=str(day), buy_date=str(buy_date),
                     sell_date=str(sell_date), buy_mode=mode,
                     status="pending_market_validation", shares=0,
                     executed_price=None, execution_confirmed=False)
                for c in selected]
    return dict(schema=1, source_date=str(day), generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                simulation_only=True, initial_cash_per_account=INITIAL_CASH,
                A=dict(buy_rule="T 09:31-09:35; minute-data validation required",
                       orders=orders(day, next_date, "T_intraday")),
                B=dict(buy_rule="T+1 opening; no repeated low-gap filter; fill not assumed",
                       orders=orders(next_date, second_date, "T_plus_1_open")),
                warning="Plans are not fills. Do not backfill A after its buy window; no broker orders.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--signal-date", required=True)
    parser.add_argument("--next-trading-day", required=True)
    parser.add_argument("--second-trading-day", required=True)
    args = parser.parse_args()
    report = load(ROOT / "outputs" / ("signals-" + args.signal_date + ".json"))
    plan = build_plan(report, args.next_trading_day, args.second_trading_day)
    target = ROOT / "outputs" / ("dual-plan-" + args.signal_date + ".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(str(target))


if __name__ == "__main__":
    main()
