# fde-estudio-contable

Motor LLM del proyecto **Estudio contable** (Coderhouse, Forward Deployed Engineer). PE2: motor base con validación estructurada. **PE3: RAG con permisos por rol y búsqueda híbrida** (ver más abajo).

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
knowledge_base/   # PE3: documentos del estudio (ficticios), con encabezado de metadatos y roles
src/
  rag.py          # PE3: ingest(), search() híbrida con filtro por rol, answer() con fuentes / requires_human
  rag_contract.py # PE3: User, SearchHit, Source, RagOutput (la Salida de la PE2 + sources)
  chunking.py     # PE3: limpieza y dos configuraciones de fragmentado (section / fixed)
  evaluate_rag.py # PE3: medición top-k, demo híbrida vs vectorial, 10 preguntas contra la API, resumen desde CSV
  motor.py        # motor: prompt -> API de Anthropic -> validación -> métricas, reintento y errores
  contract.py     # contrato de datos Pydantic: EngineInput y EngineOutput (la "Entrada" y "Salida" de la consigna)
  prompts/        # plantillas del prompt (system.txt y user.txt), separadas del código
  arca_csv.py     # lectura de los CSV de ARCA (punto y coma, coma decimal, USD con tipo de cambio)
  reference.py    # cálculo determinístico para medir la exactitud (criterio HU1: <= 1 %)
  evaluate.py     # corre los 5 casos de cases.json y compara con el KPI
data/sample/      # dos clientes FICTICIOS: monotributo y responsable inscripto
data/users.json   # PE3: usuarios ficticios y su rol (socio / auxiliar)
data/rag_questions.json  # PE3: las 10 preguntas de prueba con documento esperado y condición de aprobación
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

## PE3: RAG con permisos y búsqueda híbrida

El motor ahora responde consultas con los documentos del estudio (`knowledge_base/`): tablas de alícuotas y topes **versionadas** (número de versión, fecha de vigencia y responsable), procedimientos de DDJJ, políticas internas y documentos **restringidos a socios** (honorarios, legajos de clientes, actas). Todo es ficticio y anonimizado.

Equivalencias con la consigna (el código va en inglés): `ingestar()` → `rag.ingest()`, `buscar(consulta, usuario, k)` → `rag.search(query, user, k)`, `responder(entrada)` → `rag.answer(entrada, user)`; campos `documento`, `fragmento_id`, `texto`, `puntaje` → `document`, `chunk_id`, `text`, `score`; `fuentes` → `sources`; `requiere_humano` → `requires_human`.

```bash
python src/rag.py ingest                                   # un comando: lee, limpia, fragmenta y guarda en ChromaDB (.chroma/)
python src/rag.py show-chunk "iibb_tucuman_rates::section::001"   # metadatos de un fragmento
python src/rag.py search --user lucia --compare "NAES 477461"     # vectorial vs híbrida
python src/rag.py ask --user nicolas "¿Cuánto paga de honorarios mensuales Ferretería Los Lapachos?"
python src/rag.py ask --user lucia   "¿Cuánto paga de honorarios mensuales Ferretería Los Lapachos?"  # -> requires_human
python src/evaluate_rag.py topk          # medición top-k con los dos fragmentados (sin API)
python src/evaluate_rag.py demo          # consultas donde la híbrida acierta y la vectorial falla
python src/evaluate_rag.py answers [--model claude-haiku-5-5]   # 10 preguntas + 2 accesos denegados -> results/*.csv
python src/evaluate_rag.py summary results/<archivo>.csv          # promedios y KPI recalculados desde el CSV
```

La primera vez, ChromaDB descarga su modelo de embeddings local (all-MiniLM-L6-v2, ~80 MB, ONNX): los documentos del estudio no salen de la máquina para generar embeddings.

**Cómo funciona**

- **Ingesta**: cada documento tiene un encabezado con `doc_id`, `roles`, `version`, `effective_date` y `owner`. Cada fragmento se guarda con `document`, `section`, `roles` y un booleano por rol (`role_socio`, `role_auxiliar`).
- **Permisos antes de buscar**: `search()` pasa `where={"role_<rol>": True}` a ChromaDB en la consulta vectorial **y** al traer el corpus de BM25. Un documento restringido nunca entra a los candidatos de un auxiliar, así que tampoco puede llegar al prompt.
- **Búsqueda híbrida**: ranking vectorial (similitud coseno) + ranking BM25 (tokens sin acentos que conservan códigos como `477461`, `F.2002` o `P-884512`), fusionados con Reciprocal Rank Fusion (k = 60). BM25 solo aporta fragmentos con al menos 30 % del mejor puntaje, para que palabras comunes no metan ruido.
- **Respuesta con fuentes**: el modelo solo ve los fragmentos recuperados y devuelve los `source_ids` que usó; el código arma `sources` desde lo recuperado y rechaza (con un reintento) cualquier fuente que no se haya recuperado o una respuesta sin fuentes. Si no hay evidencia, `requires_human = true`.
