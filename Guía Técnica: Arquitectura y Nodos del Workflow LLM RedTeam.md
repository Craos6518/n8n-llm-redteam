# Guía Técnica: Arquitectura y Nodos del Workflow LLM RedTeam

Este documento describe la arquitectura completa y el funcionamiento paso a paso de cada nodo del pipeline **LLM RedTeam**, implementado en **n8n** con **Ollama** (`phi3:mini` y `bge-m3`), **Supabase (`pgvector`)**, **PostgreSQL** y **Discord**.

---

## 1. Visión General de la Arquitectura

El sistema opera bajo un enfoque **Defense-in-Depth** (defensa en profundidad) con política **fail-closed** (ante cualquier error de análisis, formato inválido o sospecha de ataque, la petición se bloquea por defecto) y está dividido en dos flujos independientes dentro de n8n:

* **Flujo A (Inferencia, Seguridad y Discord):** Procesa consultas en tiempo real provenientes de Discord o clientes HTTP, aplica filtros deterministas y de IA, ejecuta recuperación híbrida (RAG) con control de umbral semántico y audita todas las sesiones.
* **Flujo B (Ingesta Vectorial del Corpus):** Permite cargar, fragmentar y vectorizar documentos Markdown, TXT o PDF hacia Supabase.

---

## 2. Flujo A: Pipeline de Seguridad, RAG Híbrido y Respuesta

### Nodo 1: `Webhook RedTeam` (Trigger HTTP POST)
* **Tipo:** `n8n-nodes-base.webhook`
* **Ruta:** `POST /webhook/redteam`
* **Función:** Punto de entrada principal del workflow. Recibe el payload JSON enviado por el servicio `discord-bridge` (o cualquier cliente API) con los campos `prompt`, `session_id` y el objeto `discord` (`channel_id`, `message_id`). Mantiene abierta la respuesta HTTP hasta que un nodo `Respond to Webhook` finaliza la ejecución.

### Nodo 2: `Normalizar` (Code — Sanitización y Detección Heurística)
* **Tipo:** `n8n-nodes-base.code`
* **Función:** Primera barrera de defensa determinista (latencia ~0 ms).
* **Operaciones que realiza:**
  1. Valida que el campo `prompt` no esté vacío y recorta su longitud a un máximo de `10.000` caracteres para mitigar ataques de denegación de servicio (*Unbounded Consumption*).
  2. Aplica desofuscación *leetspeak* (`0→o`, `1→i`, `3→e`, `4→a`, `5→s`, `7→t`) para detectar ataques polimórficos.
  3. Evalúa expresiones regulares contra firmas conocidas de inyección directa, delimitadores de sistema (`| --- |`, `system:`) o intentos de *jailbreak* (`modo desarrollador`), asignando el indicador booleano `heuristic_attack`.
  4. Registra `started_at` (`Date.now()`) para calcular la latencia total de extremo a extremo.

### Nodo 3: `Guardian (Ollama)` (HTTP Request — Clasificador de Amenazas)
* **Tipo:** `n8n-nodes-base.httpRequest`
* **Endpoint:** `POST http://ollama:11434/api/chat`
* **Modelo y parámetros:** `phi3:mini`, `format: "json"`, `temperature: 0`, `num_ctx: 1024`, `num_predict: 150`, `keep_alive: "15m"`.
* **Función:** Segunda barrera de seguridad basada en IA. Encierra la entrada del usuario entre delimitadores `<<<` y `>>>` con refuerzo *sandwich* para evitar que el modelo obedezca instrucciones inyectadas, obligándolo a devolver exclusivamente un objeto JSON con:
  * `threat_level`: `"low"`, `"medium"` o `"high"`
  * `risk_category`: categoría de la amenaza detectada
  * `explanation`: justificación breve
  * `should_block`: booleano (`true` / `false`)

### Nodo 4: `Parse Guardian` (Code — Política Fail-Closed)
* **Tipo:** `n8n-nodes-base.code`
* **Función:** Valida la integridad de la salida del modelo clasificador y aplica la política **fail-closed**.
* **Reglas de decisión:**
  * Intenta parsear el JSON retornado por `phi3:mini`. Si la salida no es JSON válido (`parse_error: true`), clasifica automáticamente la petición como `threat_level = "high"` y `should_block = true`.
  * Si la heurística del nodo `Normalizar` detectó ofuscación (`heuristic_attack === true`), escala la amenaza a `high` con categoría `polymorphic_or_delimiter_injection`.
  * Fuerza `should_block = true` siempre que `threat_level === "high"` o exista un error de parseo.

### Nodo 5: `Amenaza media o alta?` (IF — Enrutamiento de Auditoría)
* **Tipo:** `n8n-nodes-base.if`
* **Condición:** `['medium', 'high'].includes($json.threat_level)`
* **Función:** Bifurca el flujo para garantizar que todo intento sospechoso o malicioso quede registrado en la base de datos forense antes de ejecutar el bloqueo.

### Nodo 6: `Registrar hallazgo` (Postgres — Auditoría de Incidentes)
* **Tipo:** `n8n-nodes-base.postgres`
* **Consulta SQL:**
  ```sql
  INSERT INTO audit_findings (session_id, prompt, threat_level, risk_category, explanation, should_block)
  VALUES ($1, $2, $3, $4, $5, $6)
  RETURNING id;
  ```
* **Tolerancia a fallos:** Configurado con `onError: continueRegularOutput` para asegurar que, ante una indisponibilidad temporal de PostgreSQL, el sistema nunca deje pasar una petición maliciosa.

### Nodo 7: `Bloquear?` (IF — Cortafuegos de Ejecución)
* **Tipo:** `n8n-nodes-base.if`
* **Condición:** `$('Parse Guardian').first().json.should_block === true`
* **Función:** Actúa como cortafuegos principal antes de acceder a la base vectorial o al agente generador:
  * **Rama `true`:** Detiene el procesamiento RAG y dispara la notificación de bloqueo.
  * **Rama `false`:** Autoriza la consulta y la envía al pipeline de recuperación de conocimiento.

### Nodos 8A y 8B: `Respuesta bloqueada` y `Preparar envio Discord (bloqueo)` $\rightarrow$ `Enviar bloqueo a Discord`
* **Tipos:** `n8n-nodes-base.respondToWebhook`, `n8n-nodes-base.code`, `n8n-nodes-base.discord`
* **Función:**
  * `Respuesta bloqueada` retorna el payload JSON con `blocked: true` al cliente HTTP.
  * `Preparar envio Discord (bloqueo)` construye el mensaje de alerta (`⚠️ Bloqueado por Guardian`) indicando el nivel de amenaza, categoría y explicación, fragmentándolo en bloques de máximo `1900` caracteres para respetar el límite de la API de Discord antes de publicarlo mediante `Enviar bloqueo a Discord`.

### Nodo 9: `Embedding de la consulta` (HTTP Request — Vectorización)
* **Tipo:** `n8n-nodes-base.httpRequest`
* **Endpoint:** `POST http://ollama:11434/api/embed`
* **Modelo:** `bge-m3` (`keep_alive: "5m"`).
* **Función:** Transforma la consulta legítima del usuario en un vector denso multilingüe de $1024$ dimensiones compatible con los embeddings almacenados en Supabase.

### Nodo 10: `Busqueda hibrida (Supabase RPC)` (HTTP Request — Recuperación RAG)
* **Tipo:** `n8n-nodes-base.httpRequest`
* **Endpoint:** `POST https://<proyecto>.supabase.co/rest/v1/rpc/search_documents_hybrid`
* **Función:** Ejecuta una búsqueda híbrida (vectorial + léxica) enviando `query_text`, `query_embedding` y `match_count: 3`. Retorna los 3 fragmentos más cercanos junto con dos métricas:
  * `score`: puntuación de ranking RRF (*Reciprocal Rank Fusion*, $\frac{1}{60 + \text{rango}}$).
  * `vector_score`: similitud coseno real en el rango $[0, 1]$.

### Nodo 11: `Construir contexto` (Code — Filtro Semántico y Blindaje del Prompt)
* **Tipo:** `n8n-nodes-base.code`
* **Función:** Filtra los fragmentos recuperados y construye el prompt defensivo para el modelo generador.
* **Operaciones clave:**
  1. **Filtro de umbral:** Descarta cualquier fragmento con `vector_score < 0.45` (`MIN_VECTOR_SCORE = 0.45`), eliminando ruido irrelevante en consultas fuera de dominio.
  2. **Control de longitud:** Trunca cada fragmento a `600` caracteres (máximo `1800` caracteres totales) para optimizar la velocidad de inferencia en CPU.
  3. **Aislamiento XML:** Encierra los datos recuperados en `<contexto>` y la pregunta en `<consulta_usuario>` para prevenir inyecciones secundarias.
  4. **Selección de System Prompt:** Asigna `general_system` (respuestas educativas concisas basadas estrictamente en el corpus) o `attack_system` (análisis técnico mapeado al estándar OWASP Top 10 for LLMs 2025).

### Nodo 12: `IF (num_chunks > 0)` (Control de Abstención / Anti-Alucinación)
* **Tipo:** `n8n-nodes-base.if`
* **Condición:** `{{ $json.num_chunks > 0 }}`
* **Función:** Optimiza recursos y elimina alucinaciones:
  * **Rama `true`:** Envía el contexto válido al nodo `Analyst (Ollama)`.
  * **Rama `false`:** Salta la inferencia del LLM y pasa directamente a `Formatear respuesta` para devolver un mensaje estandarizado de abstención en pocos segundos.

### Nodo 13: `Analyst (Ollama)` (HTTP Request — Agente Generador)
* **Tipo:** `n8n-nodes-base.httpRequest`
* **Endpoint:** `POST http://ollama:11434/api/chat`
* **Modelo y parámetros:** `phi3:mini`, `temperature: 0.1`, `num_ctx: 1536`, `num_predict: 480`, `num_thread: 4`, `keep_alive: "15m"`.
* **Función:** Sintetiza la respuesta final en español utilizando exclusivamente los hechos explícitos contenidos dentro de `<contexto>`.

### Nodo 14: `Formatear respuesta` (Code — Post-procesamiento y Citas)
* **Tipo:** `n8n-nodes-base.code`
* **Función:** Consolida la salida final del pipeline:
  * Si `num_chunks === 0`, asigna: *"No dispongo de información en el corpus de seguridad en LLMs para responder a esa consulta."*
  * Si hubo generación del Analyst, verifica que la respuesta incluya la sección `Fuentes:` y, en caso de que el modelo la haya omitido, agrega automáticamente los nombres únicos de los archivos recuperados (`sources`).
  * Calcula la métrica `latency_ms` total de la ejecución.

### Nodos 15A, 15B y 15C: `Guardar sesion de auditoria`, `Respuesta final` y `Enviar respuesta a Discord`
* **Tipos:** `n8n-nodes-base.postgres`, `n8n-nodes-base.respondToWebhook`, `n8n-nodes-base.code`, `n8n-nodes-base.discord`
* **Función:**
  * `Guardar sesion de auditoria` registra la consulta, respuesta generada, nivel de amenaza, modelo, latencia y fuentes JSONB en la tabla `audit_sessions` de PostgreSQL.
  * `Respuesta final` retorna el resultado completo al webhook HTTP.
  * `Preparar envio Discord (RAG)` y `Enviar respuesta a Discord` dividen la respuesta en bloques $\le 1900$ caracteres y la publican en el canal de Discord correspondiente.

---

## 3. Flujo B: Ingesta Vectorial del Corpus

### Nodo 16: `Formulario de ingesta` (Form Trigger)
* **Tipo:** `n8n-nodes-base.formTrigger`
* **Función:** Provee una interfaz web nativa en n8n para cargar uno o varios archivos técnicos en formato `.md`, `.txt` o `.pdf`.

### Nodo 17: `Separar archivos` (Code)
* **Tipo:** `n8n-nodes-base.code`
* **Función:** Desempaqueta los archivos binarios cargados en el formulario y extrae el nombre original (`fileName`) de cada documento asignándolo al campo `source` para garantizar la trazabilidad de citas durante las consultas.

### Nodo 18: `Chunking Markdown (1000/150)` (Recursive Character Text Splitter)
* **Tipo:** `@n8n/n8n-nodes-langchain.textSplitterRecursiveCharacterTextSplitter`
* **Parámetros:** `chunkSize: 1000`, `chunkOverlap: 150`, `splitCode: "markdown"`.
* **Función:** Divide los documentos respetando la jerarquía de encabezados Markdown y manteniendo un solapamiento de $150$ caracteres entre fragmentos consecutivos para preservar la continuidad semántica.

### Nodo 19: `Cargador de documentos` (Default Data Loader)
* **Tipo:** `@n8n/n8n-nodes-langchain.documentDefaultDataLoader`
* **Función:** Vincula el contenido binario fragmentado con los metadatos estructurados (`source`).

### Nodo 20: `Embeddings Ollama (ingesta)` (Embeddings Ollama)
* **Tipo:** `@n8n/n8n-nodes-langchain.embeddingsOllama`
* **Modelo:** `bge-m3:latest`
* **Función:** Calcula los vectores semánticos de cada chunk antes de su inserción en la base vectorial.

### Nodo 21: `Supabase Vector Store (ingesta)` (Supabase Vector Store)
* **Tipo:** `@n8n/n8n-nodes-langchain.vectorStoreSupabase`
* **Tabla destino:** `documents`
* **Función:** Almacena los fragmentos de texto, sus metadatos JSON (`source`) y sus vectores de embedding en Supabase (`pgvector`), dejándolos indexados para la función `search_documents_hybrid`.