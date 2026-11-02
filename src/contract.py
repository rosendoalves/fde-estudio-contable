"""Contrato de datos del motor (EngineInput / EngineOutput), derivado de la HU1 del PE1.

HU1: "Como contador, quiero cargar los CSV de ARCA de un cliente y obtener la
proyección de lo que va a pagar en el período (IVA, IIBB, Tributo Económico
Municipal o cuota de monotributo), para no armar la planilla a mano."
Criterios: las notas de crédito restan solas, la diferencia con el cálculo
manual es <= 1 % y nada sale al cliente sin aprobación de un socio.

EngineInput y EngineOutput son las clases "Entrada" y "Salida" de la consigna.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Códigos de "Tipo de Comprobante" de ARCA que acepta el motor.
INVOICES = {1, 6, 11, 51}          # factura A, B, C, M
DEBIT_NOTES = {2, 7, 12, 52}
CREDIT_NOTES = {3, 8, 13, 53}      # restan
RECEIPTS = {4, 9, 15, 54}
VALID_TYPES = INVOICES | DEBIT_NOTES | CREDIT_NOTES | RECEIPTS
TYPE_A = {1, 2, 3, 4, 51, 52, 53, 54}  # discriminan IVA (dan crédito fiscal)

PERIOD_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
CUIT_PATTERN = r"^\d{11}$"


class Regime(str, Enum):
    MONOTRIBUTO = "monotributo"
    RESPONSABLE_INSCRIPTO = "responsable_inscripto"


class Currency(str, Enum):
    ARS = "ARS"
    USD = "USD"


class Voucher(BaseModel):
    """Un comprobante de ARCA (factura, nota de crédito/débito o recibo)."""

    date: dt.date
    type: int
    point_of_sale: int = Field(ge=1)
    number: int = Field(ge=1)
    counterparty: str = Field(min_length=1)
    currency: Currency
    exchange_rate: float = Field(gt=0)
    taxed_net: float = Field(ge=0)
    untaxed: float = Field(ge=0)
    exempt: float = Field(ge=0)
    vat: float = Field(ge=0)
    total: float = Field(ge=0)

    @field_validator("type")
    @classmethod
    def known_type(cls, v: int) -> int:
        if v not in VALID_TYPES:
            raise ValueError(f"tipo de comprobante {v} no soportado")
        return v

    @property
    def sign(self) -> int:
        return -1 if self.type in CREDIT_NOTES else 1

    @property
    def period(self) -> str:
        return self.date.strftime("%Y-%m")


class EngineInput(BaseModel):
    """Lo que recibe el motor: la consulta del contador + los datos del cliente."""

    query: str = Field(min_length=5, max_length=2000)
    client_cuit: str = Field(pattern=CUIT_PATTERN)
    period: str = Field(pattern=PERIOD_PATTERN, description="AAAA-MM a liquidar")
    regime: Regime
    iibb_rate: float = Field(ge=0, le=0.15)
    tem_rate: float = Field(ge=0, le=0.05)
    monotributo_fee: Optional[float] = Field(default=None, ge=0)
    category_cap: Optional[float] = Field(default=None, gt=0)
    issued: list[Voucher]
    received: list[Voucher] = []

    @model_validator(mode="after")
    def monotributo_needs_fee(self) -> "EngineInput":
        if self.regime == Regime.MONOTRIBUTO and self.monotributo_fee is None:
            raise ValueError("un cliente monotributista necesita monotributo_fee")
        return self


class Tax(str, Enum):
    IVA = "IVA"
    IIBB = "IIBB"
    TEM = "TEM"
    MONOTRIBUTO = "MONOTRIBUTO"


class Status(str, Enum):
    OK = "ok"
    NEEDS_CLARIFICATION = "needs_clarification"
    OUT_OF_SCOPE = "out_of_scope"


class ProjectedTax(BaseModel):
    tax: Tax
    tax_base: float = Field(ge=0)
    rate: Optional[float] = Field(default=None, ge=0, le=0.30)
    amount: float = Field(ge=0)

    @model_validator(mode="after")
    def consistent_amount(self) -> "ProjectedTax":
        # Si hay alícuota, monto = base x alícuota con tolerancia de 1 % (criterio HU1).
        if self.rate is not None:
            expected = self.tax_base * self.rate
            if abs(self.amount - expected) > max(1.0, 0.01 * expected):
                raise ValueError(
                    f"{self.tax.value}: amount {self.amount} no coincide con tax_base x rate ({expected:.2f})"
                )
        return self


class EngineOutput(BaseModel):
    """Lo que devuelve el modelo, validado antes de mostrarse al contador."""

    status: Status
    period: str = Field(pattern=PERIOD_PATTERN)
    taxes: list[ProjectedTax] = []
    total_due: float = Field(ge=0)
    revenue_last_12m: Optional[float] = Field(default=None, ge=0)
    alerts: list[str] = []
    explanation: str = Field(min_length=20, max_length=1500)
    confidence: float = Field(ge=0, le=1)
    requires_partner_approval: Literal[True] = True

    @model_validator(mode="after")
    def coherence(self) -> "EngineOutput":
        if self.status == Status.OK:
            if not self.taxes:
                raise ValueError("status 'ok' sin impuestos proyectados")
            total = sum(t.amount for t in self.taxes)
            if abs(total - self.total_due) > max(1.0, 0.01 * total):
                raise ValueError(f"total_due {self.total_due} distinto de la suma de impuestos ({total:.2f})")
        elif self.taxes:
            raise ValueError(f"status '{self.status.value}' no debe traer impuestos")
        return self
