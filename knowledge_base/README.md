# Base de conocimiento del estudio (ficticia y anonimizada)

Documentos internos de un estudio contable de San Miguel de Tucumán, armados para el curso
a partir del relevamiento del PE1. **Todos los nombres de clientes, CUIT, montos, honorarios y
valores de tablas son ficticios** y deben validarse con el estudio y con ARCA / DGR / municipio
antes de usarse con un cliente real.

Cada documento empieza con un encabezado entre `---` que la ingesta lee como metadatos:

| campo | uso |
|---|---|
| `doc_id` | identificador estable del documento (se cita como fuente) |
| `title` | título legible |
| `roles` | roles que pueden leerlo: `socio`, `auxiliar` (separados por coma) |
| `version` / `effective_date` / `owner` | versionado: número de versión, fecha de vigencia y responsable de actualizarlo |

Carpetas: `tax_tables/` (tablas de alícuotas y topes, versionadas), `procedures/` (procedimientos
de DDJJ), `policies/` (políticas internas) y `restricted/` (honorarios, legajos y actas: **solo socios**).
