"""Contrato de datos del motor (Entrada / Salida), derivado de la HU1 del PE1.

HU1: "Como contador, quiero cargar los CSV de ARCA de un cliente y obtener la
proyección de lo que va a pagar en el período (IVA, IIBB, Tributo Económico
Municipal o cuota de monotributo), para no armar la planilla a mano."
Criterios: las notas de crédito restan solas, la diferencia con el cálculo
manual es <= 1 % y nada sale al cliente sin aprobación de un socio.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Códigos de "Tipo de Comprobante" de ARCA que acepta el motor.
FACTURAS = {1, 6, 11, 51}          # A, B, C, M
NOTAS_DEBITO = {2, 7, 12, 52}
NOTAS_CREDITO = {3, 8, 13, 53}     # restan
RECIBOS = {4, 9, 15, 54}
TIPOS_VALIDOS = FACTURAS | NOTAS_DEBITO | NOTAS_CREDITO | RECIBOS
TIPOS_A = {1, 2, 3, 4, 51, 52, 53, 54}  # discriminan IVA (crédito fiscal)

PATRON_PERIODO = r"^\d{4}-(0[1-9]|1[0-2])$"
PATRON_CUIT = r"^\d{11}$"


class Regimen(str, Enum):
    MONOTRIBUTO = "monotributo"
    RESPONSABLE_INSCRIPTO = "responsable_inscripto"


class Moneda(str, Enum):
    PESOS = "ARS"
    DOLARES = "USD"


class Comprobante(BaseModel):
    fecha: dt.date
    tipo: int
    punto_venta: int = Field(ge=1)
    numero: int = Field(ge=1)
    contraparte: str = Field(min_length=1)
    moneda: Moneda
    tipo_cambio: float = Field(gt=0)
    neto_gravado: float = Field(ge=0)
    no_gravado: float = Field(ge=0)
    exento: float = Field(ge=0)
    iva: float = Field(ge=0)
    total: float = Field(ge=0)

    @field_validator("tipo")
    @classmethod
    def tipo_conocido(cls, v: int) -> int:
        if v not in TIPOS_VALIDOS:
            raise ValueError(f"tipo de comprobante {v} no soportado")
        return v

    @property
    def signo(self) -> int:
        return -1 if self.tipo in NOTAS_CREDITO else 1


class Entrada(BaseModel):
    """Lo que recibe el motor: la consulta del contador + los datos del cliente."""

    consulta: str = Field(min_length=5, max_length=2000)
    cuit_cliente: str = Field(pattern=PATRON_CUIT)
    periodo: str = Field(pattern=PATRON_PERIODO, description="AAAA-MM a liquidar")
    regimen: Regimen
    alicuota_iibb: float = Field(ge=0, le=0.15)
    alicuota_tem: float = Field(ge=0, le=0.05)
    cuota_monotributo: Optional[float] = Field(default=None, ge=0)
    tope_categoria: Optional[float] = Field(default=None, gt=0)
    comprobantes_emitidos: list[Comprobante]
    comprobantes_recibidos: list[Comprobante] = []

    @model_validator(mode="after")
    def monotributo_con_cuota(self) -> "Entrada":
        if self.regimen == Regimen.MONOTRIBUTO and self.cuota_monotributo is None:
            raise ValueError("un cliente monotributista necesita cuota_monotributo")
        return self


class Impuesto(str, Enum):
    IVA = "IVA"
    IIBB = "IIBB"
    TEM = "TEM"
    MONOTRIBUTO = "MONOTRIBUTO"


class Estado(str, Enum):
    OK = "ok"
    REQUIERE_ACLARACION = "requiere_aclaracion"
    FUERA_DE_ALCANCE = "fuera_de_alcance"


class ImpuestoProyectado(BaseModel):
    impuesto: Impuesto
    base_imponible: float = Field(ge=0)
    alicuota: Optional[float] = Field(default=None, ge=0, le=0.30)
    monto: float = Field(ge=0)

    @model_validator(mode="after")
    def monto_consistente(self) -> "ImpuestoProyectado":
        # Si hay alícuota, monto = base x alícuota con tolerancia de 1 % (criterio HU1).
        if self.alicuota is not None:
            esperado = self.base_imponible * self.alicuota
            if abs(self.monto - esperado) > max(1.0, 0.01 * esperado):
                raise ValueError(
                    f"{self.impuesto.value}: monto {self.monto} no coincide con base x alícuota ({esperado:.2f})"
                )
        return self


class Salida(BaseModel):
    """Lo que devuelve el modelo, validado antes de mostrarse al contador."""

    estado: Estado
    periodo: str = Field(pattern=PATRON_PERIODO)
    impuestos: list[ImpuestoProyectado] = []
    total_a_pagar: float = Field(ge=0)
    facturacion_ultimos_12m: Optional[float] = Field(default=None, ge=0)
    alertas: list[str] = []
    explicacion: str = Field(min_length=20, max_length=1500)
    confianza: float = Field(ge=0, le=1)
    requiere_aprobacion_socio: Literal[True] = True

    @model_validator(mode="after")
    def coherencia(self) -> "Salida":
        if self.estado == Estado.OK:
            if not self.impuestos:
                raise ValueError("estado 'ok' sin impuestos proyectados")
            suma = sum(i.monto for i in self.impuestos)
            if abs(suma - self.total_a_pagar) > max(1.0, 0.01 * suma):
                raise ValueError(f"total_a_pagar {self.total_a_pagar} distinto de la suma de impuestos ({suma:.2f})")
        elif self.impuestos:
            raise ValueError(f"estado '{self.estado.value}' no debe traer impuestos")
        return self
