#!/usr/bin/env python3
"""
Investment Rebalancer

Rebalances a portfolio spread across 3 accounts:
  - 401k
  - brokerage_link
  - roth_brokerage_link

The brokerage_link and roth_brokerage_link accounts are treated as "linked" —
they must maintain the same internal weight proportions relative to each other,
even though their total dollar amounts may differ.

Usage:
    python rebalance.py holdings.json targets.json
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime


# Accounts that must share the same internal weight proportions.
LINKED_ACCOUNTS = {"brokerage_link", "roth_brokerage_link"}


def load_json(path: str):
    with open(path) as f:
        return json.load(f)


def build_account_holdings(raw_holdings: list[dict]) -> dict[str, dict[str, float]]:
    """
    Returns: { account_type: { investment: value, ... }, ... }
    """
    holdings = defaultdict(lambda: defaultdict(float))
    for row in raw_holdings:
        acct = row["account_type"]
        inv = row["investment"]
        val = float(row["value"])
        holdings[acct][inv] += val
    return holdings


def compute_portfolio_totals(holdings: dict[str, dict[str, float]]):
    """Aggregate across all accounts."""
    total = 0.0
    by_investment = defaultdict(float)
    for acct, invs in holdings.items():
        for inv, val in invs.items():
            total += val
            by_investment[inv] += val
    return total, dict(by_investment)


def rebalance(holdings: dict[str, dict[str, float]], targets: dict[str, float]):
    """
    Core rebalancing logic.

    Strategy:
      1. Compute portfolio total and each investment's target dollar amount.
      2. Figure out how much each investment needs to change globally (delta).
      3. For the 401k: allocate deltas to investments that exist in the 401k.
      4. For the linked brokerage accounts: redistribute their combined holdings
         so each account has the same internal proportions.

    Returns a dict of { account: { investment: change_amount } }
    where positive = buy, negative = sell.
    """
    portfolio_total, current_by_inv = compute_portfolio_totals(holdings)

    # Validate target weights sum to ~1.0
    weight_sum = sum(targets.values())
    if abs(weight_sum - 1.0) > 0.001:
        print(f"⚠️  Warning: target weights sum to {weight_sum:.4f}, not 1.0")

    # Target dollar amount for each investment across the whole portfolio
    target_dollars = {inv: portfolio_total * w for inv, w in targets.items()}

    # Which investments exist in which accounts (determines what's allowed where)
    account_investments = {}
    for acct, invs in holdings.items():
        account_investments[acct] = set(invs.keys())

    # Identify the 401k and linked accounts
    all_accounts = set(holdings.keys())
    non_linked = all_accounts - LINKED_ACCOUNTS
    linked_present = all_accounts & LINKED_ACCOUNTS

    # --- Step 1: figure out linked accounts' combined total and investments ---
    linked_total = 0.0
    linked_investments = set()
    for acct in linked_present:
        linked_total += sum(holdings[acct].values())
        linked_investments.update(holdings[acct].keys())

    # --- Step 2: figure out non-linked account totals and investments ---
    non_linked_totals = {}
    non_linked_investments = {}
    for acct in non_linked:
        non_linked_totals[acct] = sum(holdings[acct].values())
        non_linked_investments[acct] = set(holdings[acct].keys())

    # --- Step 3: Determine target dollars per investment for each "group" ---
    # Investments that ONLY exist in non-linked accounts get allocated there.
    # Investments that ONLY exist in linked accounts get allocated there.
    # Investments that exist in BOTH get be split proportionally by account group totals.

    # Determine which groups each investment belongs to
    inv_in_non_linked = defaultdict(set)  # investment -> set of non-linked accounts
    for acct in non_linked:
        for inv in holdings[acct]:
            inv_in_non_linked[inv].add(acct)

    inv_in_linked = set()
    for acct in linked_present:
        for inv in holdings[acct]:
            inv_in_linked.add(inv)

    # For each investment, determine target allocation per group
    # Group targets: how much $ of each investment goes to each group
    non_linked_group_target = defaultdict(float)  # inv -> target $ for non-linked
    linked_group_target = defaultdict(float)  # inv -> target $ for linked

    for inv, target_val in target_dollars.items():
        in_non_linked = inv in inv_in_non_linked
        in_linked = inv in inv_in_linked

        if in_non_linked and in_linked:
            # Split by current dollar proportions between groups
            non_linked_current = sum(
                holdings[acct].get(inv, 0) for acct in non_linked
            )
            linked_current = sum(
                holdings[acct].get(inv, 0) for acct in linked_present
            )
            combined = non_linked_current + linked_current
            if combined > 0:
                non_linked_group_target[inv] = target_val * (non_linked_current / combined)
                linked_group_target[inv] = target_val * (linked_current / combined)
            else:
                # Shouldn't happen, but split evenly
                non_linked_group_target[inv] = target_val / 2
                linked_group_target[inv] = target_val / 2
        elif in_non_linked:
            non_linked_group_target[inv] = target_val
        elif in_linked:
            linked_group_target[inv] = target_val
        else:
            print(f"⚠️  Warning: target investment '{inv}' not found in any account. Skipping.")

    # --- Step 4: Compute per-account changes ---
    changes = defaultdict(lambda: defaultdict(float))

    # Non-linked accounts: each gets its share
    # If an investment is in multiple non-linked accounts (unusual but possible),
    # split proportionally by current holdings in those accounts.
    for inv, group_target in non_linked_group_target.items():
        accounts_with_inv = inv_in_non_linked[inv]
        current_vals = {acct: holdings[acct].get(inv, 0) for acct in accounts_with_inv}
        current_total = sum(current_vals.values())

        if len(accounts_with_inv) == 1:
            acct = next(iter(accounts_with_inv))
            changes[acct][inv] = group_target - current_vals[acct]
        else:
            # Split target proportionally by current holdings
            for acct in accounts_with_inv:
                if current_total > 0:
                    acct_target = group_target * (current_vals[acct] / current_total)
                else:
                    acct_target = group_target / len(accounts_with_inv)
                changes[acct][inv] = acct_target - current_vals[acct]

    # Linked accounts: distribute so each has the same internal proportions,
    # AND the combined total reflects portfolio-level targets (not the old total).
    linked_new_combined = sum(linked_group_target.values())
    if linked_new_combined > 0:
        linked_proportions = {
            inv: val / linked_new_combined
            for inv, val in linked_group_target.items()
        }
    else:
        linked_proportions = {}

    # Split the new combined total across linked accounts proportionally
    # to their current dollar totals (preserves relative account sizes).
    linked_acct_totals = {acct: sum(holdings[acct].values()) for acct in linked_present}
    linked_current_combined = sum(linked_acct_totals.values())

    for acct in linked_present:
        if linked_current_combined > 0:
            acct_ratio = linked_acct_totals[acct] / linked_current_combined
        else:
            acct_ratio = 1.0 / len(linked_present)

        acct_new_total = linked_new_combined * acct_ratio
        for inv in linked_investments:
            target_val_for_inv = acct_new_total * linked_proportions.get(inv, 0)
            current_val = holdings[acct].get(inv, 0)
            changes[acct][inv] = target_val_for_inv - current_val

    return dict(changes)


def fmt_dollar(val: float) -> str:
    if val >= 0:
        return f"+${val:,.2f}"
    else:
        return f"-${abs(val):,.2f}"


def print_report(
    holdings: dict[str, dict[str, float]],
    targets: dict[str, float],
    changes: dict[str, dict[str, float]],
):
    portfolio_total, current_by_inv = compute_portfolio_totals(holdings)

    print("=" * 70)
    print("  INVESTMENT REBALANCER REPORT")
    print("=" * 70)
    print()

    # --- Portfolio Summary ---
    print(f"  Portfolio Total: ${portfolio_total:,.2f}")
    print()

    # --- Current vs Target Weights ---
    print("  CURRENT vs TARGET WEIGHTS (whole portfolio)")
    print("  " + "-" * 56)
    print(f"  {'Investment':<12} {'Current $':>12} {'Current %':>10} {'Target %':>10} {'Target $':>12}")
    print("  " + "-" * 56)

    all_investments = sorted(set(list(current_by_inv.keys()) + list(targets.keys())))
    for inv in all_investments:
        cur_val = current_by_inv.get(inv, 0)
        cur_pct = (cur_val / portfolio_total * 100) if portfolio_total else 0
        tgt_pct = targets.get(inv, 0) * 100
        tgt_val = portfolio_total * targets.get(inv, 0)
        print(f"  {inv:<12} ${cur_val:>10,.2f} {cur_pct:>9.1f}% {tgt_pct:>9.1f}% ${tgt_val:>10,.2f}")
    print()

    # --- Per-Account Breakdown ---
    for acct in sorted(changes.keys()):
        acct_holdings = holdings.get(acct, {})
        acct_total = sum(acct_holdings.values())
        acct_changes = changes[acct]

        # New account total after changes (should be roughly the same)
        new_total = acct_total + sum(acct_changes.values())

        label = acct.upper().replace("_", " ")
        is_linked = acct in LINKED_ACCOUNTS
        linked_tag = " [LINKED]" if is_linked else ""

        print(f"    {label}{linked_tag}")
        print(f"     Account Total: ${acct_total:,.2f}")
        print("  " + "-" * 56)
        print(f"  {'Investment':<12} {'Current':>10} {'Cur %':>8} {'Target':>10} {'Tgt %':>8} {'Change':>12}")
        print("  " + "-" * 56)

        acct_investments = sorted(
            set(list(acct_holdings.keys()) + [k for k in acct_changes if acct_changes[k] != 0])
        )
        for inv in acct_investments:
            cur = acct_holdings.get(inv, 0)
            cur_pct = (cur / acct_total * 100) if acct_total else 0
            chg = acct_changes.get(inv, 0)
            new_val = cur + chg
            new_pct = (new_val / new_total * 100) if new_total else 0
            print(f"  {inv:<12} ${cur:>8,.2f} {cur_pct:>7.1f}% ${new_val:>8,.2f} {new_pct:>7.1f}% {fmt_dollar(chg):>12}")

        print()

    # --- Action Summary ---
    print("    ACTION SUMMARY")
    print("  " + "-" * 56)
    for acct in sorted(changes.keys()):
        label = acct.upper().replace("_", " ")
        acct_changes = changes[acct]
        sells = {k: v for k, v in acct_changes.items() if v < -0.005}
        buys = {k: v for k, v in acct_changes.items() if v > 0.005}

        if not sells and not buys:
            print(f"    {label}: No changes needed")
            continue

        print(f"    {label}:")
        for inv in sorted(sells, key=lambda k: sells[k]):
            print(f"     SELL ${abs(sells[inv]):>10,.2f} of {inv}")
        for inv in sorted(buys, key=lambda k: buys[k], reverse=True):
            print(f"     BUY  ${buys[inv]:>10,.2f} of {inv}")
    print()

    # --- Final Portfolio State ---
    print("    FINAL PORTFOLIO STATE")
    print("  " + "-" * 56)
    print(f"  {'Account':<24} {'Investment':<10} {'Value':>12} {'Acct %':>8} {'Port %':>8}")
    print("  " + "-" * 56)

    for acct in sorted(changes.keys()):
        acct_holdings = holdings.get(acct, {})
        acct_changes = changes[acct]
        acct_total = sum(acct_holdings.values())
        new_total = acct_total + sum(acct_changes.values())

        label = acct.upper().replace("_", " ")
        is_linked = acct in LINKED_ACCOUNTS
        linked_tag = " [L]" if is_linked else ""

        acct_investments = sorted(
            set(list(acct_holdings.keys()) + [k for k in acct_changes if acct_changes[k] != 0])
        )
        for i, inv in enumerate(acct_investments):
            cur = acct_holdings.get(inv, 0)
            chg = acct_changes.get(inv, 0)
            new_val = cur + chg
            acct_pct = (new_val / new_total * 100) if new_total else 0
            port_pct = (new_val / portfolio_total * 100) if portfolio_total else 0
            acct_label = f"{label}{linked_tag}" if i == 0 else ""
            print(f"  {acct_label:<24} {inv:<10} ${new_val:>10,.2f} {acct_pct:>7.1f}% {port_pct:>7.1f}%")

        print(f"  {'':<24} {'TOTAL':<10} ${new_total:>10,.2f} {'100.0%':>8} {(new_total / portfolio_total * 100) if portfolio_total else 0:>7.1f}%")
        print("  " + "-" * 56)

    print(f"  {'PORTFOLIO TOTAL':<35} ${portfolio_total:>10,.2f} {'':>8} {'100.0%':>8}")
    print()
    print("=" * 70)


def build_trades(changes: dict[str, dict[str, float]]) -> dict:
    """Build a structured dict of trades (sells and buys) per account."""
    trades = {}
    for acct in sorted(changes.keys()):
        acct_changes = changes[acct]
        sells = [
            {"investment": inv, "amount": round(abs(val), 2)}
            for inv, val in sorted(acct_changes.items(), key=lambda x: x[1])
            if val < -0.005
        ]
        buys = [
            {"investment": inv, "amount": round(val, 2)}
            for inv, val in sorted(acct_changes.items(), key=lambda x: -x[1])
            if val > 0.005
        ]
        if sells or buys:
            trades[acct] = {}
            if sells:
                trades[acct]["sell"] = sells
            if buys:
                trades[acct]["buy"] = buys
    return trades


def build_final_state(
    holdings: dict[str, dict[str, float]],
    changes: dict[str, dict[str, float]],
) -> dict:
    """Build a structured dict of the final portfolio state after rebalancing."""
    portfolio_total = sum(
        sum(invs.values()) for invs in holdings.values()
    )

    state = {"portfolio_total": round(portfolio_total, 2), "accounts": {}}

    for acct in sorted(changes.keys()):
        acct_holdings = holdings.get(acct, {})
        acct_changes = changes[acct]
        acct_total = sum(acct_holdings.values())
        new_total = acct_total + sum(acct_changes.values())

        investments = sorted(
            set(list(acct_holdings.keys()) + [k for k in acct_changes if acct_changes[k] != 0])
        )

        acct_data = {
            "total": round(new_total, 2),
            "portfolio_weight": round(new_total / portfolio_total, 4) if portfolio_total else 0,
            "is_linked": acct in LINKED_ACCOUNTS,
            "investments": {},
        }

        for inv in investments:
            cur = acct_holdings.get(inv, 0)
            chg = acct_changes.get(inv, 0)
            new_val = cur + chg
            acct_data["investments"][inv] = {
                "value": round(new_val, 2),
                "account_weight": round(new_val / new_total, 4) if new_total else 0,
                "portfolio_weight": round(new_val / portfolio_total, 4) if portfolio_total else 0,
            }

        state["accounts"][acct] = acct_data

    return state


def write_outputs(
    holdings: dict[str, dict[str, float]],
    changes: dict[str, dict[str, float]],
):
    """Write trades.json and final_state.json to a timestamped output folder."""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", timestamp)
    os.makedirs(output_dir, exist_ok=True)

    trades = build_trades(changes)
    final_state = build_final_state(holdings, changes)

    trades_path = os.path.join(output_dir, "trades.json")
    with open(trades_path, "w") as f:
        json.dump(trades, f, indent=2)

    state_path = os.path.join(output_dir, "final_state.json")
    with open(state_path, "w") as f:
        json.dump(final_state, f, indent=2)

    print(f"    Output written to: {output_dir}/")
    print(f"     • trades.json")
    print(f"     • final_state.json")
    print()


def main():
    if len(sys.argv) != 3:
        print("Usage: python rebalance.py <holdings.json> <targets.json>")
        print()
        print("  holdings.json: array of {account_type, investment, value}")
        print("  targets.json:  object of {investment: target_weight}")
        sys.exit(1)

    holdings_path = sys.argv[1]
    targets_path = sys.argv[2]

    raw_holdings = load_json(holdings_path)
    targets = load_json(targets_path)

    holdings = build_account_holdings(raw_holdings)
    changes = rebalance(holdings, targets)
    print_report(holdings, targets, changes)
    write_outputs(holdings, changes)


if __name__ == "__main__":
    main()
