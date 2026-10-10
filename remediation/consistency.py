"""Acknowledged-write consistency: does an item put in the cart before a fault survive it?

Before each run the probe adds a product to a fresh cart and reads the cart back; only a write the
app has acknowledged *and* that reads back counts. After the run (and recovery) it reads the same
cart again. A write that was acknowledged and is now gone is a lost write.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

import requests

PRODUCT_ID = "OLJCESPC7Z"


@dataclass
class CartWrite:
    acknowledged: bool
    quantity: int
    session: requests.Session = field(repr=False)
    error: str = ""


class CartProbe:
    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _quantity(self, session: requests.Session) -> int | None:
        page = session.get(f"{self.base}/cart", timeout=self.timeout)
        if page.status_code != 200:
            return None
        if f"SKU #{PRODUCT_ID}" not in page.text:
            return 0
        match = re.search(r"Quantity:\s*(\d+)", page.text)
        return int(match.group(1)) if match else None

    def write(self) -> CartWrite:
        session = requests.Session()
        quantity = random.randint(2, 9)  # distinctive, so a stale cart cannot pass for this write
        try:
            session.get(f"{self.base}/", timeout=self.timeout)
            added = session.post(
                f"{self.base}/cart",
                data={"product_id": PRODUCT_ID, "quantity": str(quantity)},
                timeout=self.timeout,
                allow_redirects=False,
            )
            if added.status_code >= 400:
                return CartWrite(False, quantity, session, f"add-to-cart returned {added.status_code}")
            ok = self._quantity(session) == quantity
            return CartWrite(ok, quantity, session, "" if ok else "cart did not read back")
        except requests.RequestException as exc:
            return CartWrite(False, quantity, session, str(exc)[:200])

    def preserved(self, write: CartWrite) -> bool | None:
        """True if the acknowledged write is still there, False if lost, None if unreadable."""
        try:
            found = self._quantity(write.session)
        except requests.RequestException:
            return None
        return None if found is None else found == write.quantity
