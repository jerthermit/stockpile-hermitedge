from datetime import date
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


Role = Literal["operator", "admin"]
MovementType = Literal["stock_in", "stock_out"]
MovementSource = Literal[
    "scanner",
    "manual",
    "import",
]
ReceiptSource = Literal[
    "scanner",
    "document",
    "manual",
    "import",
]
ReturnType = Literal[
    "customer_return",
    "supplier_return",
]
StockCondition = Literal[
    "sellable",
    "damaged",
]
DamageCategory = Literal[
    "physical_damage",
    "quality_issue",
    "expired",
    "water_damage",
    "other",
]
DamageResolution = Literal[
    "restock",
    "write_off",
    "return_to_supplier",
]
InvestigationKind = Literal[
    "missing",
    "misplaced",
    "unexpected",
]
InvestigationStatus = Literal[
    "open",
    "resolved",
]
InvestigationResolution = Literal[
    "located",
    "confirmed_missing",
    "record_error",
    "dismissed",
]
ReservationAction = Literal[
    "release",
    "fulfill",
]
OrderComparisonStatus = Literal[
    "matched",
    "missing_removal",
    "quantity_mismatch",
    "unknown_product",
]
OrderComparisonResolution = Literal[
    "link_removal",
    "source_corrected",
    "accept_exception",
]
AIConfidence = Literal[
    "low",
    "medium",
    "high",
]
AIReviewPriority = Literal[
    "low",
    "medium",
    "high",
]
AIReviewStatus = Literal[
    "ready",
    "no_issues",
    "insufficient_evidence",
]
DeliveryDocumentReadStatus = Literal[
    "readable",
    "partial",
    "unreadable",
]
DeliveryDocumentType = Literal[
    "delivery_receipt",
    "invoice",
    "purchase_order",
    "unknown",
]
DeliveryDocumentMatchStatus = Literal[
    "matched",
    "unknown_product",
    "identifier_conflict",
    "inactive_product",
    "invalid_quantity",
    "duplicate_product",
]
LocationKind = Literal[
    "warehouse",
    "zone",
    "rack",
    "bin",
    "receiving",
    "staging",
    "dispatch",
    "holding",
]

LOCATION_CODE_PATTERN = (
    r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$"
)
SKU_PATTERN = (
    r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,79}$"
)


def required_text(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("This value is required.")
    return stripped


def optional_text(
    value: str | None,
) -> str | None:
    if value is None:
        return None

    stripped = value.strip()
    return stripped or None


def normalized_code(value: str) -> str:
    return required_text(value).upper()


def optional_code(
    value: str | None,
) -> str | None:
    if value is None:
        return None

    return normalized_code(value)


class StockpileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(StockpileRequest):
    username: str = Field(
        min_length=1,
        max_length=80,
    )
    password: str = Field(
        min_length=1,
        max_length=256,
    )

    @field_validator("username")
    @classmethod
    def normalize_username(
        cls,
        value: str,
    ) -> str:
        return required_text(value).lower()


class BadgeLoginRequest(StockpileRequest):
    badge_code: str = Field(
        min_length=4,
        max_length=128,
    )
    pin: str | None = Field(
        default=None,
        min_length=4,
        max_length=12,
        pattern=r"^\d+$",
    )

    @field_validator("badge_code")
    @classmethod
    def normalize_badge_code(
        cls,
        value: str,
    ) -> str:
        return required_text(value)


class ProductCreate(StockpileRequest):
    sku: str = Field(
        min_length=1,
        max_length=80,
        pattern=SKU_PATTERN,
    )
    barcode: str = Field(
        min_length=1,
        max_length=128,
    )
    product_name: str = Field(
        min_length=1,
        max_length=180,
    )
    category: str = Field(
        default="General",
        min_length=1,
        max_length=100,
    )
    unit: str = Field(
        default="piece",
        min_length=1,
        max_length=40,
    )
    variant: str | None = Field(
        default=None,
        max_length=160,
    )
    acquisition_cost_centavos: int | None = Field(
        default=None,
        strict=True,
        ge=0,
        le=1_000_000_000,
    )
    track_individually: bool = False
    active: bool = True

    @field_validator("sku")
    @classmethod
    def normalize_sku(
        cls,
        value: str,
    ) -> str:
        return normalized_code(value)

    @field_validator(
        "barcode",
        "product_name",
        "category",
        "unit",
    )
    @classmethod
    def normalize_required_fields(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator("variant")
    @classmethod
    def normalize_variant(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)


class ProductReceiveCreate(ProductCreate):
    location_code: str = Field(
        min_length=1,
        max_length=64,
        pattern=LOCATION_CODE_PATTERN,
    )
    quantity: int = Field(
        strict=True,
        gt=0,
        le=1_000_000,
    )
    reason: str | None = Field(
        default=None,
        max_length=500,
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )

    @field_validator("location_code")
    @classmethod
    def normalize_stock_location(
        cls,
        value: str,
    ) -> str:
        code = normalized_code(value)

        if code == "UNASSIGNED":
            raise ValueError(
                "Choose a stock location."
            )

        return code

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator("reason")
    @classmethod
    def normalize_reason(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)

    @model_validator(mode="after")
    def require_active_product(
        self,
    ) -> "ProductReceiveCreate":
        if not self.active:
            raise ValueError(
                "A product received into stock must be active."
            )

        return self


class ProductUpdate(StockpileRequest):
    barcode: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    product_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=180,
    )
    category: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
    )
    unit: str | None = Field(
        default=None,
        min_length=1,
        max_length=40,
    )
    variant: str | None = Field(
        default=None,
        max_length=160,
    )
    acquisition_cost_centavos: int | None = Field(
        default=None,
        strict=True,
        ge=0,
        le=1_000_000_000,
    )
    track_individually: bool | None = None
    active: bool | None = None

    @field_validator(
        "barcode",
        "product_name",
        "category",
        "unit",
    )
    @classmethod
    def normalize_optional_required_fields(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None

        return required_text(value)

    @field_validator("variant")
    @classmethod
    def normalize_variant(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)

    @model_validator(mode="after")
    def require_change(
        self,
    ) -> "ProductUpdate":
        if not self.model_fields_set:
            raise ValueError(
                "Provide at least one product field to update."
            )

        return self


class LocationCreate(StockpileRequest):
    code: str = Field(
        min_length=1,
        max_length=64,
        pattern=LOCATION_CODE_PATTERN,
    )
    name: str = Field(
        min_length=1,
        max_length=120,
    )
    kind: LocationKind
    parent_code: str | None = Field(
        default=None,
        max_length=64,
        pattern=LOCATION_CODE_PATTERN,
    )
    active: bool = True

    @field_validator("code")
    @classmethod
    def normalize_location_code(
        cls,
        value: str,
    ) -> str:
        return normalized_code(value)

    @field_validator("parent_code")
    @classmethod
    def normalize_parent_code(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_code(value)

    @field_validator("name")
    @classmethod
    def normalize_location_name(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @model_validator(mode="after")
    def prevent_self_parent(
        self,
    ) -> "LocationCreate":
        if self.parent_code == self.code:
            raise ValueError(
                "A location cannot be its own parent."
            )

        return self


class LocationUpdate(StockpileRequest):
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
    )
    kind: LocationKind | None = None
    parent_code: str | None = Field(
        default=None,
        max_length=64,
        pattern=LOCATION_CODE_PATTERN,
    )
    active: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_location_name(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None

        return required_text(value)

    @field_validator("parent_code")
    @classmethod
    def normalize_parent_code(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_code(value)

    @model_validator(mode="after")
    def require_change(
        self,
    ) -> "LocationUpdate":
        if not self.model_fields_set:
            raise ValueError(
                "Provide at least one location field to update."
            )

        return self


class TransactionCreate(StockpileRequest):
    barcode: str = Field(
        min_length=1,
        max_length=128,
    )
    movement_type: MovementType
    quantity: int = Field(
        strict=True,
        gt=0,
        le=1_000_000,
    )
    location_code: str = Field(
        default="UNASSIGNED",
        min_length=1,
        max_length=64,
        pattern=LOCATION_CODE_PATTERN,
    )
    reason: str | None = Field(
        default=None,
        max_length=500,
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )
    source: MovementSource = "scanner"

    @field_validator(
        "barcode",
        "idempotency_key",
    )
    @classmethod
    def normalize_required_text(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator("location_code")
    @classmethod
    def normalize_location_code(
        cls,
        value: str,
    ) -> str:
        return normalized_code(value)

    @field_validator("reason")
    @classmethod
    def normalize_reason(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)


class ReceiptLine(StockpileRequest):
    barcode: str = Field(
        min_length=1,
        max_length=128,
    )
    quantity: int = Field(
        strict=True,
        gt=0,
        le=1_000_000,
    )
    location_code: str = Field(
        min_length=1,
        max_length=64,
        pattern=LOCATION_CODE_PATTERN,
    )

    @field_validator("barcode")
    @classmethod
    def normalize_barcode(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator("location_code")
    @classmethod
    def normalize_location_code(
        cls,
        value: str,
    ) -> str:
        return normalized_code(value)


class ReceiptCreate(StockpileRequest):
    supplier_name: str | None = Field(
        default=None,
        max_length=160,
    )
    reference: str | None = Field(
        default=None,
        max_length=120,
    )
    lines: list[ReceiptLine] = Field(
        min_length=1,
        max_length=500,
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )
    source: ReceiptSource = "scanner"
    receiver_badge_code: str = Field(
        min_length=4,
        max_length=128,
    )
    receiver_pin: str | None = Field(
        default=None,
        min_length=4,
        max_length=12,
        pattern=r"^\d+$",
    )
    note: str | None = Field(
        default=None,
        max_length=500,
    )

    @field_validator(
        "supplier_name",
        "reference",
        "note",
    )
    @classmethod
    def normalize_optional_fields(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator("receiver_badge_code")
    @classmethod
    def normalize_receiver_badge_code(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @model_validator(mode="after")
    def prevent_duplicate_lines(
        self,
    ) -> "ReceiptCreate":
        keys = [
            (
                line.barcode,
                line.location_code,
            )
            for line in self.lines
        ]

        if len(keys) != len(set(keys)):
            raise ValueError(
                "Combine duplicate product and location lines before submission."
            )

        return self


class ReturnLine(StockpileRequest):
    barcode: str = Field(
        min_length=1,
        max_length=128,
    )
    quantity: int = Field(
        strict=True,
        gt=0,
        le=1_000_000,
    )
    location_code: str = Field(
        min_length=1,
        max_length=64,
        pattern=LOCATION_CODE_PATTERN,
    )
    condition: StockCondition = "sellable"

    @field_validator("barcode")
    @classmethod
    def normalize_barcode(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator("location_code")
    @classmethod
    def normalize_location_code(
        cls,
        value: str,
    ) -> str:
        return normalized_code(value)


class ReturnCreate(StockpileRequest):
    return_type: ReturnType
    reference: str | None = Field(
        default=None,
        max_length=120,
    )
    party_name: str | None = Field(
        default=None,
        max_length=160,
    )
    lines: list[ReturnLine] = Field(
        min_length=1,
        max_length=500,
    )
    employee_badge_code: str = Field(
        min_length=4,
        max_length=128,
    )
    employee_pin: str | None = Field(
        default=None,
        min_length=4,
        max_length=12,
        pattern=r"^\d+$",
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )
    reason: str = Field(
        min_length=3,
        max_length=500,
    )

    @field_validator(
        "reference",
        "party_name",
    )
    @classmethod
    def normalize_optional_fields(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)

    @field_validator(
        "employee_badge_code",
        "idempotency_key",
        "reason",
    )
    @classmethod
    def normalize_required_fields(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @model_validator(mode="after")
    def prevent_duplicate_lines(
        self,
    ) -> "ReturnCreate":
        keys = [
            (
                line.barcode,
                line.location_code,
                line.condition,
            )
            for line in self.lines
        ]

        if len(keys) != len(set(keys)):
            raise ValueError(
                "Combine duplicate return lines before submission."
            )

        return self


class DamageCreate(StockpileRequest):
    barcode: str = Field(
        min_length=1,
        max_length=128,
    )
    location_code: str = Field(
        min_length=1,
        max_length=64,
        pattern=LOCATION_CODE_PATTERN,
    )
    quantity: int = Field(
        strict=True,
        gt=0,
        le=1_000_000,
    )
    category: DamageCategory = "physical_damage"
    reference: str | None = Field(
        default=None,
        max_length=120,
    )
    employee_badge_code: str = Field(
        min_length=4,
        max_length=128,
    )
    employee_pin: str | None = Field(
        default=None,
        min_length=4,
        max_length=12,
        pattern=r"^\d+$",
    )
    reason: str = Field(
        min_length=3,
        max_length=500,
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )

    @field_validator(
        "barcode",
        "employee_badge_code",
        "reason",
        "idempotency_key",
    )
    @classmethod
    def normalize_required_fields(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator("location_code")
    @classmethod
    def normalize_location_code(
        cls,
        value: str,
    ) -> str:
        return normalized_code(value)

    @field_validator("reference")
    @classmethod
    def normalize_reference(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)


class DamageResolve(StockpileRequest):
    resolution: DamageResolution
    employee_badge_code: str = Field(
        min_length=4,
        max_length=128,
    )
    employee_pin: str | None = Field(
        default=None,
        min_length=4,
        max_length=12,
        pattern=r"^\d+$",
    )
    reason: str = Field(
        min_length=3,
        max_length=500,
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )

    @field_validator(
        "employee_badge_code",
        "reason",
        "idempotency_key",
    )
    @classmethod
    def normalize_required_fields(
        cls,
        value: str,
    ) -> str:
        return required_text(value)


class InvestigationCreate(StockpileRequest):
    kind: InvestigationKind
    barcode: str = Field(
        min_length=1,
        max_length=128,
    )
    quantity: int = Field(
        strict=True,
        gt=0,
        le=1_000_000,
    )
    expected_location_code: str | None = Field(
        default=None,
        max_length=64,
        pattern=LOCATION_CODE_PATTERN,
    )
    observed_location_code: str | None = Field(
        default=None,
        max_length=64,
        pattern=LOCATION_CODE_PATTERN,
    )
    reference: str | None = Field(
        default=None,
        max_length=120,
    )
    employee_badge_code: str = Field(
        min_length=4,
        max_length=128,
    )
    employee_pin: str | None = Field(
        default=None,
        min_length=4,
        max_length=12,
        pattern=r"^\d+$",
    )
    note: str | None = Field(
        default=None,
        max_length=500,
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )

    @field_validator(
        "barcode",
        "employee_badge_code",
        "idempotency_key",
    )
    @classmethod
    def normalize_required_fields(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator(
        "expected_location_code",
        "observed_location_code",
    )
    @classmethod
    def normalize_location_codes(
        cls,
        value: str | None,
    ) -> str | None:
        code = optional_code(value)
        if code == "UNASSIGNED":
            raise ValueError(
                "Choose a stock location."
            )
        return code

    @field_validator(
        "reference",
        "note",
    )
    @classmethod
    def normalize_optional_fields(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)

    @model_validator(mode="after")
    def validate_locations(
        self,
    ) -> "InvestigationCreate":
        if self.kind == "missing":
            if self.expected_location_code is None:
                raise ValueError(
                    "A missing-stock report requires the expected location."
                )
            if self.observed_location_code is not None:
                raise ValueError(
                    "Missing stock cannot have an observed location."
                )

        if self.kind == "misplaced":
            if (
                self.expected_location_code is None
                or self.observed_location_code is None
            ):
                raise ValueError(
                    "A misplaced-stock report requires both locations."
                )
            if (
                self.expected_location_code
                == self.observed_location_code
            ):
                raise ValueError(
                    "Expected and observed locations must be different."
                )

        if (
            self.kind == "unexpected"
            and self.observed_location_code is None
        ):
            raise ValueError(
                "An unexpected-stock report requires the observed location."
            )

        if (
            self.expected_location_code is not None
            and self.observed_location_code is not None
            and self.expected_location_code
            == self.observed_location_code
        ):
            raise ValueError(
                "Expected and observed locations must be different."
            )

        return self


class InvestigationEvidenceCreate(StockpileRequest):
    location_code: str | None = Field(
        default=None,
        max_length=64,
        pattern=LOCATION_CODE_PATTERN,
    )
    note: str = Field(
        min_length=3,
        max_length=500,
    )
    employee_badge_code: str = Field(
        min_length=4,
        max_length=128,
    )
    employee_pin: str | None = Field(
        default=None,
        min_length=4,
        max_length=12,
        pattern=r"^\d+$",
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )

    @field_validator(
        "note",
        "employee_badge_code",
        "idempotency_key",
    )
    @classmethod
    def normalize_required_fields(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator("location_code")
    @classmethod
    def normalize_location_code(
        cls,
        value: str | None,
    ) -> str | None:
        code = optional_code(value)
        if code == "UNASSIGNED":
            raise ValueError(
                "Choose a recorded stock location."
            )
        return code


class InvestigationResolve(StockpileRequest):
    resolution: InvestigationResolution
    resolved_location_code: str | None = Field(
        default=None,
        max_length=64,
        pattern=LOCATION_CODE_PATTERN,
    )
    reference: str | None = Field(
        default=None,
        max_length=120,
    )
    employee_badge_code: str = Field(
        min_length=4,
        max_length=128,
    )
    employee_pin: str | None = Field(
        default=None,
        min_length=4,
        max_length=12,
        pattern=r"^\d+$",
    )
    reason: str = Field(
        min_length=3,
        max_length=500,
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )

    @field_validator(
        "employee_badge_code",
        "reason",
        "idempotency_key",
    )
    @classmethod
    def normalize_required_fields(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator("resolved_location_code")
    @classmethod
    def normalize_resolution_location(
        cls,
        value: str | None,
    ) -> str | None:
        code = optional_code(value)
        if code == "UNASSIGNED":
            raise ValueError(
                "Choose a stock location."
            )
        return code

    @field_validator("reference")
    @classmethod
    def normalize_reference(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)

    @model_validator(mode="after")
    def validate_resolution(
        self,
    ) -> "InvestigationResolve":
        if (
            self.resolution == "located"
            and self.resolved_location_code is None
        ):
            raise ValueError(
                "A located item requires its confirmed location."
            )

        if (
            self.resolution != "located"
            and self.resolved_location_code is not None
        ):
            raise ValueError(
                "Only located stock can have a resolved location."
            )

        return self


class ReservationLine(StockpileRequest):
    barcode: str = Field(
        min_length=1,
        max_length=128,
    )
    location_code: str = Field(
        min_length=1,
        max_length=64,
        pattern=LOCATION_CODE_PATTERN,
    )
    quantity: int = Field(
        strict=True,
        gt=0,
        le=1_000_000,
    )

    @field_validator("barcode")
    @classmethod
    def normalize_barcode(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator("location_code")
    @classmethod
    def normalize_location_code(
        cls,
        value: str,
    ) -> str:
        return normalized_code(value)


class ReservationCreate(StockpileRequest):
    reference: str = Field(
        min_length=1,
        max_length=120,
    )
    customer_name: str | None = Field(
        default=None,
        max_length=160,
    )
    sales_channel: str | None = Field(
        default=None,
        max_length=80,
    )
    lines: list[ReservationLine] = Field(
        min_length=1,
        max_length=500,
    )
    note: str | None = Field(
        default=None,
        max_length=500,
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )

    @field_validator(
        "reference",
        "idempotency_key",
    )
    @classmethod
    def normalize_required_fields(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator(
        "customer_name",
        "sales_channel",
        "note",
    )
    @classmethod
    def normalize_optional_fields(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)

    @model_validator(mode="after")
    def prevent_duplicate_lines(
        self,
    ) -> "ReservationCreate":
        keys = [
            (
                line.barcode,
                line.location_code,
            )
            for line in self.lines
        ]

        if len(keys) != len(set(keys)):
            raise ValueError(
                "Combine duplicate reservation lines before submission."
            )

        return self


class ReservationClose(StockpileRequest):
    action: ReservationAction
    employee_badge_code: str | None = Field(
        default=None,
        min_length=4,
        max_length=128,
    )
    employee_pin: str | None = Field(
        default=None,
        min_length=4,
        max_length=12,
        pattern=r"^\d+$",
    )
    reason: str | None = Field(
        default=None,
        max_length=500,
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )

    @field_validator("employee_badge_code")
    @classmethod
    def normalize_employee_badge_code(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)

    @field_validator("reason")
    @classmethod
    def normalize_reason(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @model_validator(mode="after")
    def require_fulfillment_signoff(
        self,
    ) -> "ReservationClose":
        if (
            self.action == "fulfill"
            and self.employee_badge_code is None
        ):
            raise ValueError(
                "Scan the employee badge before fulfilling reserved stock."
            )

        return self


class SellingOrderLine(StockpileRequest):
    order_reference: str = Field(
        min_length=1,
        max_length=120,
    )
    sku: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=SKU_PATTERN,
    )
    barcode: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    quantity: int = Field(
        strict=True,
        gt=0,
        le=1_000_000,
    )

    @field_validator("order_reference")
    @classmethod
    def normalize_order_reference(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator("sku")
    @classmethod
    def normalize_sku(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_code(value)

    @field_validator("barcode")
    @classmethod
    def normalize_barcode(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)

    @model_validator(mode="after")
    def require_product_identifier(
        self,
    ) -> "SellingOrderLine":
        if self.sku is None and self.barcode is None:
            raise ValueError(
                "Provide a SKU or barcode for each order line."
            )

        return self


class SellingOrderImport(StockpileRequest):
    sales_channel: str = Field(
        min_length=1,
        max_length=80,
    )
    lines: list[SellingOrderLine] = Field(
        min_length=1,
        max_length=5_000,
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )

    @field_validator(
        "sales_channel",
        "idempotency_key",
    )
    @classmethod
    def normalize_required_fields(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @model_validator(mode="after")
    def prevent_duplicate_lines(
        self,
    ) -> "SellingOrderImport":
        keys = [
            (
                line.order_reference,
                line.sku or "",
                line.barcode or "",
            )
            for line in self.lines
        ]

        if len(keys) != len(set(keys)):
            raise ValueError(
                "Combine duplicate order and product lines before submission."
            )

        return self


class OrderComparisonResolve(StockpileRequest):
    resolution: OrderComparisonResolution
    inventory_transaction_id: str | None = Field(
        default=None,
        max_length=80,
    )
    reason: str = Field(
        min_length=3,
        max_length=500,
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )

    @field_validator("inventory_transaction_id")
    @classmethod
    def normalize_transaction_id(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)

    @field_validator(
        "reason",
        "idempotency_key",
    )
    @classmethod
    def normalize_required_fields(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @model_validator(mode="after")
    def validate_resolution(
        self,
    ) -> "OrderComparisonResolve":
        if (
            self.resolution == "link_removal"
            and self.inventory_transaction_id is None
        ):
            raise ValueError(
                "Choose the recorded stock removal to link."
            )

        if (
            self.resolution != "link_removal"
            and self.inventory_transaction_id is not None
        ):
            raise ValueError(
                "Only a linked-removal resolution can include a transaction."
            )

        return self


class DeliveryDocumentExtractedLine(StockpileRequest):
    source_line: int = Field(
        strict=True,
        ge=1,
        le=500,
    )
    sku: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=SKU_PATTERN,
    )
    barcode: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    description: str | None = Field(
        default=None,
        min_length=1,
        max_length=180,
    )
    quantity: int | None = Field(
        default=None,
        strict=True,
        gt=0,
        le=1_000_000,
    )
    unit: str | None = Field(
        default=None,
        min_length=1,
        max_length=40,
    )
    source_text: str = Field(
        min_length=1,
        max_length=240,
    )
    confidence: AIConfidence

    @field_validator("sku")
    @classmethod
    def normalize_sku(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_code(value)

    @field_validator(
        "barcode",
        "description",
        "unit",
    )
    @classmethod
    def normalize_optional_fields(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)

    @field_validator("source_text")
    @classmethod
    def normalize_source_text(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @model_validator(mode="after")
    def require_product_evidence(
        self,
    ) -> "DeliveryDocumentExtractedLine":
        if (
            self.sku is None
            and self.barcode is None
            and self.description is None
        ):
            raise ValueError(
                "Each document line needs a SKU, barcode, or description."
            )

        return self


class DeliveryDocumentExtraction(StockpileRequest):
    read_status: DeliveryDocumentReadStatus
    document_type: DeliveryDocumentType
    supplier_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    reference: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
    )
    document_date: date | None = None
    lines: list[DeliveryDocumentExtractedLine] = Field(
        max_length=500,
    )
    warnings: list[str] = Field(
        max_length=20,
    )
    confidence: AIConfidence

    @field_validator(
        "supplier_name",
        "reference",
    )
    @classmethod
    def normalize_optional_fields(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)

    @field_validator("warnings")
    @classmethod
    def normalize_warnings(
        cls,
        values: list[str],
    ) -> list[str]:
        normalized: list[str] = []

        for value in values:
            warning = required_text(value)
            if len(warning) > 240:
                raise ValueError(
                    "Document warnings cannot exceed 240 characters."
                )
            if warning not in normalized:
                normalized.append(warning)

        return normalized

    @model_validator(mode="after")
    def validate_document_lines(
        self,
    ) -> "DeliveryDocumentExtraction":
        source_lines = [line.source_line for line in self.lines]
        if len(source_lines) != len(set(source_lines)):
            raise ValueError(
                "Document source-line numbers must be unique."
            )

        if self.read_status == "unreadable" and self.lines:
            raise ValueError(
                "An unreadable document cannot include extracted lines."
            )

        return self


class DeliveryDocumentProduct(StockpileRequest):
    sku: str = Field(
        min_length=1,
        max_length=80,
        pattern=SKU_PATTERN,
    )
    barcode: str = Field(
        min_length=1,
        max_length=128,
    )
    product_name: str = Field(
        min_length=1,
        max_length=180,
    )
    unit: str = Field(
        min_length=1,
        max_length=40,
    )

    @field_validator("sku")
    @classmethod
    def normalize_sku(
        cls,
        value: str,
    ) -> str:
        return normalized_code(value)

    @field_validator(
        "barcode",
        "product_name",
        "unit",
    )
    @classmethod
    def normalize_required_fields(
        cls,
        value: str,
    ) -> str:
        return required_text(value)


class DeliveryDocumentLineMatch(StockpileRequest):
    source_line: int = Field(
        strict=True,
        ge=1,
        le=500,
    )
    status: DeliveryDocumentMatchStatus
    product: DeliveryDocumentProduct | None = None

    @model_validator(mode="after")
    def validate_product_match(
        self,
    ) -> "DeliveryDocumentLineMatch":
        product_required = self.status in {
            "matched",
            "inactive_product",
            "invalid_quantity",
            "duplicate_product",
        }

        if product_required and self.product is None:
            raise ValueError(
                "This match status requires a Stockpile product."
            )

        if not product_required and self.product is not None:
            raise ValueError(
                "This match status cannot select a Stockpile product."
            )

        return self


class DeliveryDocumentAnalysis(StockpileRequest):
    extraction: DeliveryDocumentExtraction
    matches: list[DeliveryDocumentLineMatch] = Field(
        max_length=500,
    )
    receipt_ready: bool

    @model_validator(mode="after")
    def validate_analysis(
        self,
    ) -> "DeliveryDocumentAnalysis":
        extracted_by_line = {
            line.source_line: line for line in self.extraction.lines
        }
        matches_by_line = {
            line.source_line: line for line in self.matches
        }

        if len(matches_by_line) != len(self.matches):
            raise ValueError(
                "Document match results cannot repeat a source line."
            )

        if extracted_by_line.keys() != matches_by_line.keys():
            raise ValueError(
                "Every extracted line needs exactly one match result."
            )

        for source_line, match in matches_by_line.items():
            extracted = extracted_by_line[source_line]
            product = match.product

            if product is not None:
                if extracted.sku is None and extracted.barcode is None:
                    raise ValueError(
                        "Description-only lines cannot be matched to a product."
                    )

                if (
                    extracted.sku is not None
                    and extracted.sku != product.sku
                ):
                    raise ValueError(
                        "The extracted SKU does not match the selected product."
                    )

                if (
                    extracted.barcode is not None
                    and extracted.barcode != product.barcode
                ):
                    raise ValueError(
                        "The extracted barcode does not match the selected product."
                    )

            if match.status == "matched" and extracted.quantity is None:
                raise ValueError(
                    "A matched document line needs a valid quantity."
                )

            if (
                match.status == "invalid_quantity"
                and extracted.quantity is not None
            ):
                raise ValueError(
                    "An invalid-quantity result requires a missing quantity."
                )

        should_be_ready = (
            self.extraction.read_status == "readable"
            and self.extraction.document_type in {
                "delivery_receipt",
                "invoice",
            }
            and bool(self.matches)
            and all(
                line.status == "matched" for line in self.matches
            )
        )
        if self.receipt_ready != should_be_ready:
            raise ValueError(
                "Receipt readiness must reflect every document line."
            )

        return self


class AIDiscrepancyFinding(StockpileRequest):
    priority: AIReviewPriority
    title: str = Field(
        min_length=1,
        max_length=80,
    )
    explanation: str = Field(
        min_length=1,
        max_length=220,
    )
    evidence_refs: list[str] = Field(
        min_length=1,
        max_length=8,
    )
    next_step: str = Field(
        min_length=1,
        max_length=180,
    )
    confidence: AIConfidence

    @field_validator(
        "title",
        "explanation",
        "next_step",
    )
    @classmethod
    def normalize_required_fields(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator("evidence_refs")
    @classmethod
    def normalize_evidence_refs(
        cls,
        values: list[str],
    ) -> list[str]:
        normalized: list[str] = []

        for value in values:
            reference = required_text(value)
            if len(reference) > 160:
                raise ValueError(
                    "Evidence references cannot exceed 160 characters."
                )
            if reference in normalized:
                raise ValueError(
                    "Evidence references cannot be repeated."
                )
            normalized.append(reference)

        return normalized


class AIDiscrepancyReview(StockpileRequest):
    review_status: AIReviewStatus
    summary: str = Field(
        min_length=1,
        max_length=240,
    )
    findings: list[AIDiscrepancyFinding] = Field(
        max_length=3,
    )
    questions_for_staff: list[str] = Field(
        max_length=1,
    )
    limitations: list[str] = Field(
        max_length=1,
    )
    confidence: AIConfidence

    @field_validator("summary")
    @classmethod
    def normalize_summary(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator(
        "questions_for_staff",
        "limitations",
    )
    @classmethod
    def normalize_short_text_lists(
        cls,
        values: list[str],
    ) -> list[str]:
        normalized: list[str] = []

        for value in values:
            item = required_text(value)
            if len(item) > 180:
                raise ValueError(
                    "Review notes cannot exceed 180 characters."
                )
            if item not in normalized:
                normalized.append(item)

        return normalized

    @model_validator(mode="after")
    def validate_review_status(
        self,
    ) -> "AIDiscrepancyReview":
        if self.review_status == "ready" and not self.findings:
            raise ValueError(
                "A ready review must include at least one finding."
            )

        if self.review_status != "ready" and self.findings:
            raise ValueError(
                "Only a ready review can include findings."
            )

        if (
            self.review_status == "insufficient_evidence"
            and not self.limitations
        ):
            raise ValueError(
                "An evidence-limited review must explain its limitation."
            )

        return self


class AIDiscrepancyReviewCreate(StockpileRequest):
    window_hours: Literal[24] = 24
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(
        cls,
        value: str,
    ) -> str:
        return required_text(value)


class TransferLine(StockpileRequest):
    barcode: str = Field(
        min_length=1,
        max_length=128,
    )
    quantity: int = Field(
        strict=True,
        gt=0,
        le=1_000_000,
    )

    @field_validator("barcode")
    @classmethod
    def normalize_barcode(
        cls,
        value: str,
    ) -> str:
        return required_text(value)


class TransferCreate(StockpileRequest):
    source_location_code: str = Field(
        min_length=1,
        max_length=64,
        pattern=LOCATION_CODE_PATTERN,
    )
    destination_location_code: str = Field(
        min_length=1,
        max_length=64,
        pattern=LOCATION_CODE_PATTERN,
    )
    lines: list[TransferLine] = Field(
        min_length=1,
        max_length=500,
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )
    note: str | None = Field(
        default=None,
        max_length=500,
    )

    @field_validator(
        "source_location_code",
        "destination_location_code",
    )
    @classmethod
    def normalize_location_code(
        cls,
        value: str,
    ) -> str:
        return normalized_code(value)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator("note")
    @classmethod
    def normalize_note(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)

    @model_validator(mode="after")
    def validate_transfer(
        self,
    ) -> "TransferCreate":
        if (
            self.source_location_code
            == self.destination_location_code
        ):
            raise ValueError(
                "Source and destination locations must be different."
            )

        barcodes = [
            line.barcode
            for line in self.lines
        ]

        if len(barcodes) != len(set(barcodes)):
            raise ValueError(
                "Combine duplicate product lines before submission."
            )

        return self


class TransferDiscrepancy(
    StockpileRequest,
):
    barcode: str = Field(
        min_length=1,
        max_length=128,
    )
    quantity_received: int = Field(
        strict=True,
        ge=0,
        le=1_000_000,
    )
    note: str | None = Field(
        default=None,
        max_length=240,
    )

    @field_validator("barcode")
    @classmethod
    def normalize_barcode(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator("note")
    @classmethod
    def normalize_note(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)


class TransferReceive(StockpileRequest):
    receiver_badge_code: str = Field(
        min_length=4,
        max_length=128,
    )
    receiver_pin: str | None = Field(
        default=None,
        min_length=4,
        max_length=12,
        pattern=r"^\d+$",
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )
    discrepancies: list[
        TransferDiscrepancy
    ] = Field(
        default_factory=list,
        max_length=500,
    )
    note: str | None = Field(
        default=None,
        max_length=500,
    )

    @field_validator("receiver_badge_code")
    @classmethod
    def normalize_receiver_badge_code(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator("note")
    @classmethod
    def normalize_note(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)

    @model_validator(mode="after")
    def prevent_duplicate_discrepancies(
        self,
    ) -> "TransferReceive":
        barcodes = [
            discrepancy.barcode
            for discrepancy in self.discrepancies
        ]

        if len(barcodes) != len(set(barcodes)):
            raise ValueError(
                "Provide only one received quantity for each product."
            )

        return self


class StockCountLine(StockpileRequest):
    barcode: str = Field(
        min_length=1,
        max_length=128,
    )
    quantity: int = Field(
        strict=True,
        ge=0,
        le=1_000_000,
    )

    @field_validator("barcode")
    @classmethod
    def normalize_barcode(
        cls,
        value: str,
    ) -> str:
        return required_text(value)


class StockCountCreate(StockpileRequest):
    location_code: str = Field(
        min_length=1,
        max_length=64,
        pattern=LOCATION_CODE_PATTERN,
    )
    lines: list[StockCountLine] = Field(
        min_length=1,
        max_length=5_000,
    )
    counter_badge_code: str = Field(
        min_length=4,
        max_length=128,
    )
    counter_pin: str | None = Field(
        default=None,
        min_length=4,
        max_length=12,
        pattern=r"^\d+$",
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )
    note: str | None = Field(
        default=None,
        max_length=500,
    )

    @field_validator("location_code")
    @classmethod
    def normalize_location_code(
        cls,
        value: str,
    ) -> str:
        return normalized_code(value)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator("counter_badge_code")
    @classmethod
    def normalize_counter_badge_code(
        cls,
        value: str,
    ) -> str:
        return required_text(value)

    @field_validator("note")
    @classmethod
    def normalize_note(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)

    @model_validator(mode="after")
    def prevent_duplicate_lines(
        self,
    ) -> "StockCountCreate":
        barcodes = [
            line.barcode
            for line in self.lines
        ]

        if len(barcodes) != len(set(barcodes)):
            raise ValueError(
                "Provide only one counted quantity for each product."
            )

        return self


class StockCountApprove(StockpileRequest):
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )
    reason: str = Field(
        min_length=3,
        max_length=500,
    )

    @field_validator(
        "idempotency_key",
        "reason",
    )
    @classmethod
    def normalize_required_text(
        cls,
        value: str,
    ) -> str:
        return required_text(value)


class StockCountCancel(StockpileRequest):
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )
    reason: str = Field(
        min_length=3,
        max_length=500,
    )

    @field_validator(
        "idempotency_key",
        "reason",
    )
    @classmethod
    def normalize_required_text(
        cls,
        value: str,
    ) -> str:
        return required_text(value)


class ReversalCreate(StockpileRequest):
    reason: str = Field(
        min_length=3,
        max_length=500,
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
    )

    @field_validator(
        "reason",
        "idempotency_key",
    )
    @classmethod
    def normalize_required_text(
        cls,
        value: str,
    ) -> str:
        return required_text(value)


class InventoryImportResult(
    StockpileRequest,
):
    created: int = Field(ge=0)
    updated: int = Field(ge=0)
    skipped: int = Field(ge=0)
    errors: list[str] = Field(
        default_factory=list,
    )


class ExchangeRetryRequest(StockpileRequest):
    transaction_id: str | None = Field(
        default=None,
        max_length=80,
    )

    @field_validator("transaction_id")
    @classmethod
    def normalize_transaction_id(
        cls,
        value: str | None,
    ) -> str | None:
        return optional_text(value)
