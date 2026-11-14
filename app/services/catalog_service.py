from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.config import PRODUCT_DATA_FILE
from app.domain import CandidateProduct


class CatalogService:
    def __init__(self, data_file: str | Path | None = None) -> None:
        file_path = Path(data_file) if data_file else PRODUCT_DATA_FILE
        with open(file_path, "r", encoding="utf-8") as handle:
            self.products = json.load(handle)
        self.products_by_id = {product["id"]: product for product in self.products}

    @staticmethod
    def _allowed_lot_size(product: dict) -> Decimal | None:
        value = product.get("allowedLotSize")
        if value is None:
            return None
        lot_size = Decimal(str(value))
        return Decimal("1") if lot_size == Decimal("0.01") else lot_size

    def search(self, query: str, limit: int = 5) -> list[CandidateProduct]:
        needle = (query or "").strip().lower()
        if not needle:
            return []

        scored: list[tuple[int, CandidateProduct]] = []
        for product in self.products:
            brand = (product.get("synkkaData") or {}).get("brand") or ""
            names = [
                item.get("value", "")
                for item in (product.get("synkkaData") or {}).get("names", [])
            ]
            haystack = " ".join([
                product.get("id", ""),
                product.get("category", ""),
                brand,
                *names,
            ]).lower()

            score = 0
            normalized_query = needle.replace("-", " ")
            for token in normalized_query.split():
                if token in haystack:
                    score += 20
                if token in haystack.split():
                    score += 10
            if needle in haystack:
                score += 30

            if score:
                scored.append(
                    (
                        score,
                        CandidateProduct(
                            product_id=product["id"],
                            name=names[0] if names else product["id"],
                            brand=brand or None,
                            category=product.get("category"),
                            sales_unit=product.get("salesUnit"),
                            base_unit=product.get("baseUnit"),
                            allowed_lot_size=self._allowed_lot_size(product),
                            country_of_origin=product.get("countryOfOrigin"),
                            relevance_score=score,
                        ),
                    )
                )

        ranked = sorted(scored, key=lambda item: (-item[0], item[1].product_id))
        return [candidate for _, candidate in ranked[:limit]]

    def get_by_id(self, product_id: str) -> CandidateProduct | None:
        product = self.products_by_id.get(product_id)
        if product is None:
            return None
        return CandidateProduct(
            product_id=product["id"],
            name=(product.get("synkkaData") or {}).get("names", [{}])[0].get("value", product["id"]),
            brand=(product.get("synkkaData") or {}).get("brand") or None,
            category=product.get("category"),
            sales_unit=product.get("salesUnit"),
            base_unit=product.get("baseUnit"),
            allowed_lot_size=self._allowed_lot_size(product),
            country_of_origin=product.get("countryOfOrigin"),
            relevance_score=100,
        )
