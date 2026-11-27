# ADR 0004 · Punto de aplicación de políticas para Q&A

## Status

Adoptado para el prototipo local del Sprint 5. Amplía el ADR 0003 con la
entrega al solicitante. AB#227, AB#228, AB#230, AB#231, AB#233 y AB#244.

## Context

El asistente genera Q&A en una petición separada de la búsqueda de artículos.
El evaluador consume el estado, los pares y los identificadores de fuente
devueltos por el asistente. La política JSON y el expediente SQLite preceden
a esta integración HTTP.

## Decision

- `POST /api/v1/governed/qa/generate` envía el objeto JSON a la ruta fija
  `POST /api-se/v1/llm-search/generate-qa/` mediante httpx. El cliente aplica
  timeouts por fase, limita el cuerpo de respuesta a 1 MiB, desactiva
  redirecciones y proxies del entorno y omite las cabeceras y cookies del
  solicitante.
- El PEP devuelve 200 para permiso, 202 para suspensión y 403 para denegación
  después de guardar el dictamen. El expediente y el dictamen de suspensión
  se guardan en la misma transacción. Los fallos del upstream dejan un
  dictamen cuando la política y el almacén están disponibles. Un fallo de
  evaluación o persistencia impide la entrega.
- El solicitante recibe una credencial portadora vinculada al `review_id`
  mediante HMAC-SHA-256. El PEP comprueba esa credencial antes de consultar el
  expediente. El sondeo devuelve 202 mientras esté pendiente, 200 tras
  aprobación o modificación autorizada y 403 ante rechazo o vencimiento.
  Una modificación entrega los pares corregidos.
- La política suministrada exige revisión humana. Una versión que habilite
  explícitamente `allow_complete` puede permitir una salida con estado de
  éxito, el número esperado de pares e identificadores de fuente completos.
- SQLite incorpora `allow` mediante una migración transaccional que conserva
  los dictámenes, sus relaciones y las protecciones de solo adición. Las
  políticas de revisión conservan el formato de snapshot anterior.

## Alternatives

- Aplicar la política en el frontend dejaría disponible la ruta directa del
  asistente. Integrar el PEP en el gateway requiere coordinar el perímetro.
- Usar el identificador como credencial daría acceso con un valor presente
  también en la API de supervisión. Vincular la entrega a identidad exigiría
  autenticar y persistir la identidad del solicitante.
- Agregar un cliente de enriquecimiento y caché no aporta campos que consuma
  el evaluador de este contrato. Los identificadores vienen del upstream.

## Consequences

La credencial debe quedar fuera de URLs, logs y cachés. Quien la obtenga
puede consultar ese expediente y recibir su resultado autorizado. Las
instancias deben compartir una clave aleatoria estable. Rotarla invalida las
credenciales previas. El sondeo no crea eventos de acceso.

La comprobación estructural deja pendientes la verificación factual y el
respaldo de las afirmaciones. El prototipo usa JSON y SQLite. YAML, PostgreSQL,
los roles de auditoría y las garantías forenses siguen pendientes. También
quedan pendientes la integración con gateway y frontend, el control de rutas
directas equivalentes y el despliegue.
