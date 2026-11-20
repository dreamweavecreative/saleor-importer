"""Import reporting."""

from dataclasses import dataclass, field


@dataclass
class ImportReport:
    filename: str
    total: int = 0
    valid: int = 0
    invalid: int = 0
    duplicate_skus: int = 0
    errors: list = field(default_factory=list)

    def add_error(self, row_number, sku, messages):
        self.errors.append({
            "row": row_number,
            "sku": sku,
            "errors": messages,
        })

    def print_summary(self):
        print()
        print("=" * 55)
        print("DICKERDATA IMPORT VALIDATION SUMMARY")
        print("=" * 55)
        print("File:", self.filename)
        print("Total records:", self.total)
        print("Valid records:", self.valid)
        print("Invalid records:", self.invalid)
        print("Duplicate SKUs:", self.duplicate_skus)
        print("=" * 55)

        for error in self.errors[:30]:
            print(
                f"ROW {error['row']} "
                f"SKU={error['sku']}: "
                f"{'; '.join(error['errors'])}"
            )

        if len(self.errors) > 30:
            print(
                f"... {len(self.errors) - 30} "
                "additional errors"
            )
