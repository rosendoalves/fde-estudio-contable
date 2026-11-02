# fde-estudio-contable

Motor LLM base del proyecto **Estudio contable** (Coderhouse, Forward Deployed Engineer, preentrega 2).

Toma los CSV de "Mis Comprobantes" de ARCA de un cliente y una consulta del contador, y devuelve una proyección validada de lo que el cliente va a pagar en el período: IVA, Ingresos Brutos, Tributo Económico Municipal (TEM) o cuota de monotributo. Nada sale al cliente sin aprobación de un socio (`requiere_aprobacion_socio` es siempre `true`).

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
python src/motor.py --cliente datos/ejemplo/cliente_ri --periodo 2026-10 \
  --consulta "Necesito la proyección de octubre para revisarla con el socio"
```

Los 5 casos de evaluación (3 normales, 1 confusa y 1 fuera de tema), con la tabla de métricas y la comparación contra el KPI del PE1:

```bash
python src/evaluar.py
```

Pruebas sin API key (un cliente falso simula al modelo):

```bash
python -m unittest discover tests
```

## Estructura

```
src/
  motor.py        # motor: prompt -> API de Anthropic -> validación -> métricas, reintento y errores
  contrato.py     # contrato de datos Pydantic: Entrada y Salida (derivados de la HU1)
  prompts/        # plantillas del prompt (sistema y usuario), separadas del código
  arca_csv.py     # lectura de los CSV de ARCA (punto y coma, coma decimal, USD con tipo de cambio)
  referencia.py   # cálculo determinístico para medir la exactitud (criterio HU1: <= 1 %)
  evaluar.py      # corre los 5 casos de casos.json y compara con el KPI
datos/ejemplo/    # dos clientes FICTICIOS: monotributo y responsable inscripto
tests/            # pruebas offline del contrato, el reintento y los errores
resultados/       # CSV de cada corrida de evaluar.py
```

## Contrato de datos

**Entrada**: `consulta` (5 a 2000 caracteres), `cuit_cliente` (11 dígitos), `periodo` (`AAAA-MM`), `regimen` (enum `monotributo` | `responsable_inscripto`), `alicuota_iibb` (0 a 0,15), `alicuota_tem` (0 a 0,05), `cuota_monotributo` (obligatoria si es monotributo), `tope_categoria` (opcional) y las listas de comprobantes emitidos y recibidos. Cada comprobante valida que el tipo sea un código ARCA soportado y que los importes no sean negativos.

**Salida**: `estado` (enum `ok` | `requiere_aclaracion` | `fuera_de_alcance`), `periodo`, `impuestos` (IVA, IIBB, TEM, MONOTRIBUTO con base, alícuota y monto), `total_a_pagar`, `facturacion_ultimos_12m`, `alertas`, `explicacion` (20 a 1500 caracteres), `confianza` (0 a 1) y `requiere_aprobacion_socio` (siempre `true`). Además se valida que monto = base x alícuota (tolerancia 1 %), que el total sea la suma de los impuestos y que un estado distinto de `ok` no traiga números.

## Comportamiento ante fallas

- **Entrada inválida**: no se llama a la API; se devuelve `error.tipo = "validacion_entrada"` con los campos que fallaron.
- **Salida inválida**: se registra en el log qué campos fallaron, se le devuelve el error al modelo y se reintenta **una vez**. Si vuelve a fallar: `error.tipo = "validacion_salida"`.
- **Errores de la API** (key inválida, modelo inexistente, 429, 5xx, sin conexión): el SDK reintenta 2 veces los transitorios y después el motor devuelve `error.tipo = "api"` con el motivo.
- **Rechazo del modelo**: `error.tipo = "rechazo"`. En Opus y Sonnet 5.5 está activado el fallback del lado del servidor (`fallbacks="default"`).

Cada resultado trae `metricas`: modelo, intentos, tokens de entrada y salida (de `usage`), costo en USD con los precios publicados, latencia en ms y los `request_id`.

## Datos

Los CSV de `datos/ejemplo/` son **ficticios**: se generaron a partir del formato real de ARCA, con CUIT, DNI, nombres y códigos de autorización reemplazados. Las alícuotas y la cuota de monotributo son parámetros ficticios del curso y hay que validarlos con el estudio. Los datos reales de clientes van en `datos/reales/`, que está en `.gitignore`.
