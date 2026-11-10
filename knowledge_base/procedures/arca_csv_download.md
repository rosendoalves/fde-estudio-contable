---
doc_id: arca_csv_download
title: Descarga y normalización de Mis Comprobantes (ARCA)
roles: socio, auxiliar
version: 2
effective_date: 2026-03-01
owner: Mercedes (socia)
---
# Descarga y normalización de Mis Comprobantes (ARCA)

Procedimiento que sigue el auxiliar cada mes, por cliente, antes de cualquier liquidación.
Es el paso que más tiempo consume del ciclo (aprox. 25 minutos de descarga y 40 de normalización por cliente).

## Descarga

1. Ingresar a ARCA con la clave fiscal delegada del cliente y abrir el servicio "Mis Comprobantes".
2. Descargar por separado "Emitidos" y "Recibidos" del período, en formato CSV.
3. Guardar los archivos en la carpeta del cliente con el nombre `AAAA-MM_emitidos.csv` y `AAAA-MM_recibidos.csv`.
4. Si el cliente factura en dólares, verificar que la columna "Tipo Cambio" esté completa.

## Normalización

- Los CSV de ARCA usan punto y coma como separador y coma decimal; las celdas vacías se toman como cero.
- Los importes en dólares se convierten a pesos con el tipo de cambio de cada comprobante.
- Las notas de crédito (tipos 3, 8 y 13) se cargan con signo negativo: restan de la base imponible
  y, si son tipo A, también restan del débito o crédito fiscal. Antes se corregían a mano en la
  planilla; ahora el motor las resta solo, pero el auxiliar debe controlar que el total coincida.
- Los comprobantes anulados no aparecen en Mis Comprobantes y no hay que cargarlos.

## Control de consistencia

Antes de pasar la planilla al socio, el auxiliar compara la suma de "Imp. Total" de emitidos contra
el libro IVA digital del período. Si la diferencia supera el 1 %, se revisa comprobante por comprobante.
