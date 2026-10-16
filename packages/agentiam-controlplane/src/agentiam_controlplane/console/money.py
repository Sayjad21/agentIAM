"""How the console shows an amount of money — APICTA-04.

Every amount in AgentIAM is BDT, held to four decimal places (`NUMERIC(20,4)`). The console
shows it in taka with a US-dollar equivalent beside it, because the people reading a demo are
not all Bangladeshi: `৳500,000 ($4,167)`.

**The dollar figure is display only, at a fixed rate.** Nothing is converted, settled or
compared in USD; the ledger, the caveats and the policy all speak BDT. The rate is
configuration (`AGENTIAM_CONTROLPLANE_BDT_PER_USD`, default 120) and every page states it,
so a figure is never mistaken for a live quote.

`console/static/ui.js`'s `AgentIAM.bdt()` renders the same string in the browser, by the same
rules, for the pages that draw amounts client-side. The two are kept in step by a test that
checks every money-rendering page uses the shared helper, not by sharing code: one is Python
on the server, the other JavaScript in a page that must work with no build step.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

#: Taka per US dollar, for display. Chosen as a round figure near the 2026 market rate.
DEFAULT_BDT_PER_USD: Final = Decimal("120")

#: From here up, cents add digits without adding meaning.
_WHOLE_DOLLARS_FROM: Final = Decimal("1000")

_WHOLE: Final = Decimal(1)
_CENTS: Final = Decimal("0.01")


def format_bdt(amount: Decimal, *, bdt_per_usd: Decimal = DEFAULT_BDT_PER_USD) -> str:
    """`৳500,000 ($4,167)` — taka first, the dollar equivalent beside it.

    Taka are shown whole when the amount is whole, which every demo amount is, and to two
    places otherwise; the ledger's four places would only add zeros. Dollars are whole from
    $1,000 and to the cent below it, so a small amount is not rounded into a misleading one.
    """
    if amount == amount.to_integral_value():
        taka = f"{amount.quantize(_WHOLE, rounding=ROUND_HALF_UP):,.0f}"
    else:
        taka = f"{amount.quantize(_CENTS, rounding=ROUND_HALF_UP):,.2f}"

    usd = amount / bdt_per_usd
    if abs(usd) >= _WHOLE_DOLLARS_FROM:
        dollars = f"{usd.quantize(_WHOLE, rounding=ROUND_HALF_UP):,.0f}"
    else:
        dollars = f"{usd.quantize(_CENTS, rounding=ROUND_HALF_UP):,.2f}"
    return f"৳{taka} (${dollars})"


__all__ = ["DEFAULT_BDT_PER_USD", "format_bdt"]
