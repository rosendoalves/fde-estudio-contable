# fde-estudio-contable

Motor LLM base del proyecto **Estudio contable** (Coderhouse, Forward Deployed Engineer, preentrega 2).

Toma los CSV de "Mis Comprobantes" de ARCA de un cliente y una consulta del contador, y devuelve una proyección validada de lo que el cliente va a pagar en el período: IVA, Ingresos Brutos, Tributo Económico Municipal (TEM) o cuota de monotributo. Nada sale al cliente sin aprobación de un socio (`requires_partner_approval` es siempre `true`).

## Cómo correrlo desde un clon limpio

```bash
git clone https://github.com/rosendoalves/fde-estudio-contable.git
cd fde-estudio-contable
python -m venv .venv && source .venv/bin/activate   # en Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env      # y completá ANTHROPIC_API_KEY
```

Una consulta:

```bash
python src/motor.py --client data/sample/client_ri --period 2026-10 \
  --query "Necesito la proyección de octubre para revisarla con el socio"
```

Los 5 casos de evaluación (3 normales, 1 confusa y 1 fuera de tema), con la tabla de métricas y la comparación contra el KPI del PE1:

```bash
python src/evaluate.py
```

Pruebas sin API key (un cliente falso simula al modelo):

```bash
python -m unittest discover tests
```

## Estructura

El código y los nombres de archivos están en inglés; comentarios y documentación, en castellano. `src/motor.py` conserva el nombre que pide la consigna.

```
src/
  motor.py        # motor: prompt -> API de Anthropic -> validación -> métricas, reintento y errores
  contract.py     # contrato de datos Pydantic: EngineInput y EngineOutput (la "Entrada" y "Salida" de la consigna)
  prompts/        # plantillas del prompt (system.txt y user.txt), separadas del código
  arca_csv.py     # lectura de los CSV de ARCA (punto y coma, coma decimal, USD con tipo de cambio)
  reference.py    # cálculo determinístico para medir la exactitud (criterio HU1: <= 1 %)
  evaluate.py     # corre los 5 casos de cases.json y compara con el KPI
data/sample/      # dos clientes FICTICIOS: monotributo y responsable inscripto
tests/            # pruebas offline del contrato, el reintento y los errores
results/          # CSV de cada corrida de evaluate.py
```

## Contrato de datos

**EngineInput (Entrada)**: `query` (5 a 2000 caracteres), `client_cuit` (11 dígitos), `period` (`AAAA-MM`), `regime` (enum `monotributo` | `responsable_inscripto`), `iibb_rate` (0 a 0,15), `tem_rate` (0 a 0,05), `monotributo_fee` (obligatoria si es monotributo), `category_cap` (opcional) y las listas de comprobantes `issued` y `received`. Cada comprobante valida que el tipo sea un código ARCA soportado y que los importes no sean negativos.

**EngineOutput (Salida)**: `status` (enum `ok` | `needs_clarification` | `out_of_scope`), `period`, `taxes` (IVA, IIBB, TEM, MONOTRIBUTO con `tax_base`, `rate` y `amount`), `total_due`, `revenue_last_12m`, `alerts`, `explanation` (20 a 1500 caracteres), `confidence` (0 a 1) y `requires_partner_approval` (siempre `true`). Además se valida que amount = tax_base x rate (tolerancia 1 %), que `total_due` sea la suma de los impuestos y que un status distinto de `ok` no traiga números.

## Comportamiento ante fallas

- **Entrada inválida**: no se llama a la API; se devuelve `error.kind = "input_validation"` con los campos que fallaron.
- **Salida inválida**: se registra en el log qué campos fallaron, se le devuelve el error al modelo y se reintenta **una vez**. Si vuelve a fallar: `error.kind = "output_validation"`.
- **Errores de la API** (key inválida, modelo inexistente, 429, 5xx, sin conexión): el SDK reintenta 2 veces los transitorios y después el motor devuelve `error.kind = "api"` con el motivo.
- **Rechazo del modelo**: `error.kind = "refusal"`. En Opus y Sonnet 5.5 está activado el fallback del lado del servidor (`fallbacks="default"`).

Cada resultado trae `metrics`: modelo, intentos, tokens de entrada y salida (de `usage`), costo en USD con los precios publicados, latencia en ms y los `request_id`.

## Datos

Los CSV de `data/sample/` son **ficticios**: se generaron a partir del formato real de ARCA, con CUIT, DNI, nombres y códigos de autorización reemplazados. Las alícuotas y la cuota de monotributo son parámetros ficticios del curso y hay que validarlos con el estudio. Los datos reales de clientes van en `data/real/`, que está en `.gitignore`.
