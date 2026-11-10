---
doc_id: iva_rates_and_voucher_codes
title: Alícuotas de IVA y códigos de comprobante de ARCA
roles: socio, auxiliar
version: 1
effective_date: 2025-01-01
owner: Nicolás (socio)
---
# Alícuotas de IVA y códigos de comprobante de ARCA

## Alícuotas de IVA

| Alícuota | Casos típicos de nuestros clientes |
|---|---|
| 21 % | Alícuota general: ventas de ferretería, indumentaria, servicios profesionales |
| 10,5 % | Alícuota reducida: ciertos alimentos, bienes de capital, obra en vivienda |
| 27 % | Servicios públicos facturados a responsables inscriptos (luz, gas, telefonía) |
| 0 % / exento | Operaciones exentas o no gravadas: libros, intereses de ciertos préstamos |

## Códigos de tipo de comprobante (columna "Tipo de Comprobante" de Mis Comprobantes)

| Código | Comprobante | Efecto en la liquidación |
|---|---|---|
| 1 | Factura A | Suma; discrimina IVA y da crédito fiscal al receptor |
| 2 | Nota de débito A | Suma |
| 3 | Nota de crédito A | Resta |
| 6 | Factura B | Suma; no da crédito fiscal |
| 8 | Nota de crédito B | Resta |
| 11 | Factura C | Suma; la emiten monotributistas |
| 13 | Nota de crédito C | Resta |
| 51 | Factura M | Suma; sujeta a retención |

Solo los comprobantes recibidos de tipo A (1, 2, 3, 4) y M (51 a 54) generan crédito fiscal de IVA.
