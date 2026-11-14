# Centinela — governance-service

Servicio FastAPI del Componente E de Centinela: gobernanza de la salida generada
por otros servicios. Evalúa cada respuesta contra una política versionada,
registra la decisión y, si la política lo pide, retiene la respuesta para que un
administrador la revise antes de entregarla.

Este README es para quien desarrolla o integra el servicio. Explica qué hace,
cómo ejecutarlo y dónde está el detalle. Los contratos HTTP y las decisiones de
arquitectura están en inglés, en [`docs/contracts/`](docs/contracts/) y
[`docs/adr/`](docs/adr/).

## Estado

El servicio es un prototipo local. Funciona hoy:

- Q&A gobernada: proxy de `search-service/generate-qa` con evaluación,
  retención y entrega diferida.
- Recomendaciones gobernadas: proxy de `ethical-recommender-service` que
  evalúa la configuración de equidad aplicada.
- Casos de revisión protegidos, con resolución final y vencimiento.
- Autenticación de administradores mediante `X-Admin-Token`.
- Muestreo retrospectivo reproducible de entregas de Q&A.

Pendiente: integración con gateway y frontend, políticas institucionales
aprobadas, almacén de políticas YAML y auditoría forense en PostgreSQL. Las
políticas incluidas (`policies/*.json`) son borradores experimentales, no
umbrales institucionales.

## Inicio rápido

Requisito: Python 3.11, el objetivo de despliegue y de CI. Las versiones
resueltas están fijadas en `constraints.txt`.

1. Crea el entorno e instala las dependencias:

   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
   ```

2. Copia `.env.example` a `.env`. La configuración por defecto no necesita
   base de datos ni servicios externos.
3. Inicia la API:

   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8005
   ```

4. Confirma que responde en `GET /health`, `GET /api/v1/status` y `/docs`.

`/health` describe solo el proceso de la API: un servicio sano no implica que
los controles de gobernanza estén configurados. El puerto 8005 es provisional
hasta acordarlo con el mantenedor.

## Configuración

Las variables usan el prefijo `GOVERNANCE_` y se leen también de `.env`. Una
configuración inválida falla al crear la aplicación. Los archivos de ejemplo no
contienen credenciales.

| Variable | Uso | Valor por defecto |
|---|---|---|
| `ENVIRONMENT` | `local`, `staging` o `production` | `local` |
| `PUBLIC_BASE_PATH` | Servidor declarado en OpenAPI | `/api/v1` |
| `PORT` | Puerto en Compose y contenedor (Uvicorn directo usa `--port`) | `8005` |
| `DATABASE` | Archivo SQLite escribible; requerido por revisión, Q&A y recomendaciones | sin valor |
| `ADMIN_VERIFIER_BASE_URL` | Origen interno de `search-service` que valida tokens | sin valor |
| `ADMIN_VERIFIER_TIMEOUT_SECONDS` | Tiempo máximo de validación (máx. 30) | `3` |
| `REVIEW_WINDOW_MINUTES` | Plazo para resolver un caso (máx. 10080) | `1440` |
| `QA_UPSTREAM_BASE_URL` | Origen de `search-service`; activa la Q&A gobernada | sin valor |
| `QA_RELEASE_KEY` | Secreto aleatorio estable, ≥ 32 caracteres, igual en todas las instancias | sin valor |
| `QA_POLICY_PATH` | Política de Q&A | `policies/search-qa-review.draft-1.json` |
| `QA_TIMEOUT_SECONDS` | Tiempos de fase de httpx (máx. 120) | `30` |
| `QA_RETAIN_SYNTHETIC_DELIVERIES` | Solo local: conserva cuerpos sintéticos para el muestreo | `false` |
| `RECOMMENDER_UPSTREAM_BASE_URL` | Origen de `ethical-recommender-service` | sin valor |
| `RECOMMENDER_HMAC_KEY` | Clave HMAC externa, ≥ 32 caracteres | sin valor |
| `RECOMMENDER_POLICY_PATH` | Política de equidad | `policies/recommender-fairness.draft-1.json` |
| `RECOMMENDER_TIMEOUT_SECONDS` | Tiempo máximo hacia el recomendador (máx. 120) | `30` |

Guarda las bases locales en el directorio ignorado `local/` y usa solo datos
sintéticos.

## API

Todas las rutas cuelgan de `/api/v1`. Las de administrador exigen la cabecera
`X-Admin-Token`.

| Método y ruta | Quién | Propósito |
|---|---|---|
| `POST /governed/qa/generate` | Solicitante | Generar Q&A gobernada |
| `GET /governed/qa/reviews/{review_id}` | Solicitante (token de revisión) | Consultar una Q&A retenida |
| `POST /governed/recommender/recommend` | Solicitante | Obtener recomendaciones gobernadas |
| `GET /governed/recommender/reviews/{review_id}` | Solicitante (token de revisión) | Consultar una recomendación retenida |
| `GET /supervisors/me` | Administrador | Comprobar el token (`{"role": "admin"}`) |
| `GET /review-cases/{case_id}` | Administrador | Ver un caso retenido |
| `POST /review-cases/{case_id}/resolution` | Administrador | Aprobar, modificar o rechazar un caso |
| `POST /qa-samples/` | Administrador | Crear una muestra retrospectiva |
| `GET /qa-samples/{sample_id}` | Administrador | Leer una muestra |
| `GET /qa-samples/{sample_id}/deliveries/{assessment_id}` | Administrador | Leer una entrega muestreada |
| `PUT /qa-samples/{sample_id}/deliveries/{assessment_id}/resolution` | Administrador | Confirmar, marcar o corregir una entrega |
| `GET /qa-samples/{sample_id}/interventions` | Administrador | Tasas de intervención |
| `GET /status` | Cualquiera | Metadatos del servicio |

Los contratos completos:

- [Q&A gobernada](docs/contracts/search-generate-qa.v1.md)
- [Recomendaciones gobernadas](docs/contracts/ethical-recommender-recommend.v1.md)
- [Muestreo retrospectivo](docs/contracts/qa-samples-v1.md)

## Flujo de gobernanza

1. El solicitante llama a una ruta gobernada. El servicio reenvía la petición
   al servicio de origen.
2. El evaluador aplica la política versionada: Q&A devuelve `allow`, `suspend`
   o `deny`; recomendaciones, `permit`, `retain` o `deny`.
3. La decisión, la instantánea de la política y, si hay retención, el caso de
   revisión se guardan en una sola transacción.
4. Según el veredicto:
   - Permitido: 200 con la respuesta.
   - Retenido: 202 con `review_id` y `review_token`.
   - Denegado, o fallo del origen, de la política o del almacenamiento: 403.
5. El solicitante consulta la revisión con el token como credencial Bearer:
   202 mientras está pendiente, 200 tras aprobar o modificar, 403 si se
   rechaza, vence o la credencial no es válida.

El servicio falla de forma cerrada: ante cualquier error deniega o retiene,
nunca libera. No interpretes el éxito por el código HTTP del origen:
`search-service` puede devolver errores dentro de un 200.

Mantén el token de revisión fuera de logs y URL, y usa TLS en el borde público.

### Q&A

La política borrador retiene toda respuesta no vacía dentro del número esperado
de pares, incluso la completa: los identificadores de fuente no verifican el
contexto real ni el respaldo de cada afirmación. Deniega cuerpos malformados,
errores de generación y conteos inutilizables. Una versión de política con
`allow_complete: true` permite entregar directamente la respuesta completa.
Detalle en [ADR 0004](docs/adr/0004-governed-qa-policy-enforcement.md).

### Recomendaciones

El servicio evalúa los parámetros de `rerank` realmente aplicados, no los
pedidos. La política borrador retiene las respuestas que desactivan la
equidad. Las respuestas protegidas omiten nombres y los autores se
seudonimizan con HMAC. Detalle en
[ADR 0006](docs/adr/0006-governed-recommendations.md).

## Revisión por administradores

Cada petición protegida envía el token a
`GET /internal/auth/validate-admin-token/` en `search-service`. Solo un 204
autoriza. Ver [ADR 0005](docs/adr/0005-supervision-through-admin-token.md).

| Situación | Estado | `code` |
|---|---|---|
| Falta la cabecera | 401 | `AUTHENTICATION_REQUIRED` |
| Token inválido o rechazado | 403 | `ADMIN_TOKEN_REJECTED` |
| Verificador inaccesible, lento o con respuesta inesperada | 503 | `ADMIN_VERIFIER_UNAVAILABLE` |
| Falta el verificador o la base de datos | 503 | `SUPERVISION_NOT_CONFIGURED` |

Reglas de resolución (`approve`, `modify` o `reject`):

- Modificar exige justificación (máx. 2000 caracteres). Aprobar también la
  exige si la salida tiene `PARTIAL_GENERATION` o `SOURCE_EVIDENCE_INCOMPLETE`.
- Un caso tiene una sola resolución final. Otro resultado u otro supervisor
  recibe 409 `CASE_ALREADY_RESOLVED`.
- El caso vence tras `REVIEW_WINDOW_MINUTES`. Una resolución tardía registra el
  vencimiento y recibe 409 `CASE_EXPIRED`; la salida vencida nunca se libera.
- Otros errores: 404 `CASE_NOT_FOUND`, 422 `JUSTIFICATION_REQUIRED` o
  `INVALID_REQUEST`. Los cuerpos de error solo traen `code` y `trace_id`.
- Las vistas usan `Cache-Control: no-store` y la creación responde 201 (200 si
  se repite la misma petición).

Ver [ADR 0003](docs/adr/0003-final-review-resolutions-with-expiry.md).

## Datos y auditoría local

- El caso de revisión guarda solo pares pregunta-respuesta e identificadores de
  fuente. Descarta la salida cruda y los demás campos del origen.
- Las decisiones y los casos son de solo anexado. Un digest SHA-256 detecta
  contenido alterado al leer (`ReviewCaseIntegrityError`).
- El contenido protegido no aparece en logs, mensajes de excepción, `repr` ni
  consultas generales. Solo `SqliteReviewCaseRepository` lo lee.
- Una política no cambia dentro de su versión: reutilizar ID y versión con otro
  contenido lanza `PolicyVersionConflictError`.

Límites del prototipo: SQLite no cifra en reposo, y quien pueda abrir el
archivo puede leer o reemplazar registros, o desactivar triggers y checksums.
No es una cadena de hashes, un log anclado ni una base de producción. No se
auditan las vistas, no hay listado de casos y ningún planificador llama a
`ReviewCases.expire_overdue()`. Ver
[ADR 0001](docs/adr/0001-protected-review-cases-in-local-store.md).

## Uso sin HTTP

El flujo también funciona como biblioteca, útil para experimentos offline:

```python
from pathlib import Path

from app.main import create_qa_assessor

Path("local").mkdir(exist_ok=True)
assessor = create_qa_assessor(
    Path("policies/search-qa-review.draft-1.json"),
    Path("local/assessments.sqlite3"),
)
record = assessor.execute({"status": "error", "qa_pairs": []})
assert record.assessment.verdict == "deny"
```

Para evaluar sin guardar, usa
`app.infrastructure.qa_response.assess_qa_response(response, policy)`. Esa
función no libera contenido ni crea auditoría.

## Arquitectura

Capas con la regla de dependencia hacia adentro:

- `app/domain`: modelos inmutables y protocolos de repositorio, sin frameworks.
- `app/application`: casos de uso; no importa Pydantic, HTTP ni SQLite.
- `app/infrastructure`: carga estricta de políticas JSON, clientes httpx hacia
  los orígenes y repositorios SQLite.
- `app/api`: rutas FastAPI; `app.api.errors` traduce errores de dominio a
  respuestas con código.
- `app/main.py`: raíz de composición.

`tests/test_architecture.py` verifica la regla de dependencia. OpenAPI declara
como servidor `GOVERNANCE_PUBLIC_BASE_PATH`. El gateway publicaría el servicio
en `/api/governance` y reescribiría a `/api/v1`; esa integración requiere un PR
aparte en el gateway.

## Calidad

Ejecuta antes de cada commit:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m black --check .
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing --cov-report=xml --junitxml=report.xml
```

La cobertura de ramas debe ser ≥ 90 %. Las reglas de código, pruebas,
seguridad y revisión están en los
[estándares de ingeniería](docs/engineering-standards.md). La CI usa
Python 3.11, valida Compose y construye la imagen Docker; no despliega.

## Contenedor

Crea la red externa si no existe y levanta el servicio:

```powershell
docker network create centinela-net
docker compose up -d --build
```

Compose publica solo en loopback, ejecuta como usuario no root, elimina
capacidades y monta el sistema de archivos en solo lectura.

## Trazabilidad

Work items de Azure Boards:

- AB#221–224: PBI, arranque, workflows y contenedor.
- AB#230/231: evaluación de políticas y pruebas.
- AB#240/241: casos de revisión protegidos (local).
- AB#242, AB#244/245: vista, resolución y vencimiento de casos.
- AB#248: verificación del token de administrador.
- AB#217/218/236/237 y AB#258/260: base parcial; las tareas YAML y PostgreSQL
  siguen abiertas.

## Publicación

Sincroniza antes de empezar y sube las ramas `feature/*` cuando pasen las
comprobaciones. Abrir PR, fusionar y desplegar requieren aprobación del
responsable: fusionar en `develop` despliega en staging automáticamente. Ver la
sección 9 de los [estándares de ingeniería](docs/engineering-standards.md).
