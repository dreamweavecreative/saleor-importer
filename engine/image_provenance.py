from dataclasses import dataclass, asdict
from typing import Optional

@dataclass
class ImageProvenance:
    status: str
    source_tier: str
    source_url: Optional[str]
    brand: str
    sku: str
    mpn: Optional[str]
    confidence: float
    license: str
    sha256: Optional[str]

    def to_dict(self) -> dict:
        return asdict(self)
