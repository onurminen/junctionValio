from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.config import PRODUCT_DATA_FILE
from app.domain import CandidateProduct


class CatalogService:
    _ALLERGEN_RELATIONS = {"CONTAINS", "MAY_CONTAIN", "UNDECLARED", "FREE_FROM"}

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

    @classmethod
    def _allergen_classifications(cls, product: dict) -> dict[str, list[str]]:
        classifications: dict[str, set[str]] = {}
        synkka_data = product.get("synkkaData") or {}
        if not isinstance(synkka_data, dict):
            return {}
        for group in synkka_data.get("classifications") or []:
            if not isinstance(group, dict):
                continue
            if (group.get("name") or "").lower() not in {"allergen", "nonallergen"}:
                continue
            for value in group.get("values") or []:
                if not isinstance(value, dict):
                    continue
                allergen_id = str(value.get("id") or "").strip().upper()
                relation = str(value.get("unit") or "").strip().upper()
                if allergen_id and relation in cls._ALLERGEN_RELATIONS:
                    classifications.setdefault(allergen_id, set()).add(relation)
        return {
            allergen_id: sorted(relations)
            for allergen_id, relations in classifications.items()
        }

    def search(
        self,
        query: str,
        limit: int = 5,
        category: str | None = None,
        excluded_allergen_ids: list[str] | None = None,
    ) -> list[CandidateProduct]:
        needle = (query or "").strip().lower()
        if not needle:
            return []
        required_category = category.strip() if category and category.strip() else None
        required_allergens = {
            allergen_id.strip().upper()
            for allergen_id in excluded_allergen_ids or []
            if allergen_id.strip()
        }

        scored: list[tuple[int, CandidateProduct]] = []
        for product in self.products:
            if required_category and product.get("category") != required_category:
                continue
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
                allergen_classifications = self._allergen_classifications(product)
                if any(
                    allergen_classifications.get(allergen_id) != ["FREE_FROM"]
                    for allergen_id in required_allergens
                ):
                    continue
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
                            allergen_classifications=allergen_classifications,
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
            allergen_classifications=self._allergen_classifications(product),
            relevance_score=100,
        )
