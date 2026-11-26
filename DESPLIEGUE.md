# Guía de Despliegue - Automatización Low-Code

## 📋 Resumen de decisiones

| Componente | Decisión | Justificación |
|------------|----------|---------------|
| **GPU Ollama** | **Solo CPU** | AMD Radeon Vega 8 (integrada, arquitectura Raven/Picasso) **NO es compatible con ROCm**. ROCm solo soporta GPU discretas RDNA/CDNA. |
| **Red** | `automatizacion_net` (bridge internal) | Aislamiento total: solo expone n8n en puerto 5678. Ollama y discord-bridge solo accesibles por nombre de contenedor. |
| **Persistencia** | Bind mounts en `./data/{n8n,ollama}` | Datos versionables y portables dentro de la carpeta del proyecto. |
| **RAM** | Límite 6G / reserva 4G para Ollama | 9.8 GiB total → deja ~3-4 GiB para SO + n8n + discord-bridge. Modelos pequeños (qwen2.5:0.5b, phi3:mini, llama3.2:1b). |

---

## 🧹 1. Limpieza previa (inicio desde cero)

**⚠️ Ejecuta en orden y confirma cada paso. No borra imágenes base.**

```bash
cd "/home/craos6518/Documentos/Semestre 2/Automatizacion y lowcode/Proyecto final"

# 1. Ver contenedores existentes (proyecto actual u otros)
docker ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"

# 2. Detener y eliminar contenedores del stack anterior (si existen)
docker compose down -v --remove-orphans 2>/dev/null || true

# 3. Ver volúmenes existentes
docker volume ls --format "table {{.Name}}\t{{.Driver}}"

# 4. Eliminar volúmenes huérfanos del proyecto anterior (n8n_data, ollama_data, qdrant_data, rag-network)
#    SOLO si no pertenecen a OTROS proyectos en la máquina
docker volume rm n8n_data ollama_data qdrant_data 2>/dev/null || true

# 5. Eliminar red anterior si existe
docker network rm rag-network automatizacion_net 2>/dev/null || true

# 6. Verificar limpieza
docker ps -a
docker volume ls
docker network ls | grep -E "automatizacion|rag"
```

**Volúmenes a borrar confirmados:** `n8n_data`, `ollama_data`, `qdrant_data` (del compose anterior).  
**Red a borrar:** `rag-network`.  
*Si ves otros volúmenes/redes no listados aquí, **no los borres** — pertenecen a otros proyectos.*

---

## 🚀 2. Preparación y despliegue

```bash
cd "/home/craos6518/Documentos/Semestre 2/Automatizacion y lowcode/Proyecto final"

# 1. Crear directorios de datos (bind mounts)
mkdir -p data/n8n data/ollama

# 2. Copiar plantilla de entorno y editar con tus credenciales REALES
cp .env.template .env
# Edita .env con: nano .env  (pon tu DISCORD_TOKEN y DISCORD_ALLOWED_CHANNELS)

# 3. Descargar imágenes (opcional, compose lo hace al levantar)
docker compose pull

# 4. Levantar stack en segundo plano
docker compose up -d

# 5. Ver logs de arranque
docker compose logs -f
```

---

## ✅ 3. Verificación post-despliegue

```bash
# Estado de contenedores
docker compose ps
# Debe mostrar: n8n (healthy), ollama (healthy), discord-bridge (running)

# Conectividad interna por nombre de contenedor
docker exec n8n wget -qO- http://ollama:11434/api/tags
# Debe devolver JSON: {"models":[]} (Ollama responde)

docker exec ollama wget -qO- http://n8n:5678/healthz
# Debe devolver: OK (n8n responde)

# Acceso a interfaz n8n (desde el host)
curl -s http://localhost:5678/healthz
# Debe devolver: OK

# Abrir en navegador: http://localhost:5678
```

---

## 🔧 4. Gestión del stack

```bash
# Detener (mantiene volúmenes y red)
docker compose stop

# Iniciar de nuevo
docker compose start

# Reiniciar un servicio específico
docker compose restart ollama

# Ver logs en tiempo real
docker compose logs -f ollama
docker compose logs -f n8n
docker compose logs -f discord-bridge

# Bajar todo (contenedores + red, SIN borrar volúmenes ./data)
docker compose down

# Bajar TODO incluyendo volúmenes bind mount (¡pierdes datos!)
docker compose down -v

# Actualizar imágenes y recrear
docker compose pull && docker compose up -d --force-recreate
```

---

## 🤖 5. Primeros pasos en Ollama (modelos recomendados para 9.8 GiB RAM)

```bash
# Entrar al contenedor
docker exec -it ollama bash

# Modelos pequeños (< 2 GB c/u) — caben varios en 6 GB límite
ollama pull qwen2.5:0.5b      # ~400 MB, rápido, buen razonamiento
ollama pull phi3:mini         # ~2.3 GB, Microsoft, muy capaz
ollama pull llama3.2:1b       # ~1.3 GB, Meta, buen equilibrio
ollama pull gemma2:2b         # ~1.6 GB, Google, fuerte en código

# Ver modelos instalados
ollama list

# Probar inferencia rápida
ollama run qwen2.5:0.5b "Hola, responde en una frase."
```

**Regla práctica:** Con límite 6 GB, carga **máximo 1 modelo a la vez** (`OLLAMA_MAX_LOADED_MODELS=1` ya configurado). Cambia modelo según tarea.

---

## 📁 Estructura final del proyecto

```
/home/craos6518/Documentos/Semestre 2/Automatizacion y lowcode/Proyecto final/
├── docker-compose.yml       # Orquestación completa
├── .env.template            # Plantilla de variables (sin secretos)
├── .env                     # TU archivo real (gitignorado)
├── data/
│   ├── n8n/                 # Persistencia n8n (workflows, credenciales, BD SQLite)
│   └── ollama/              # Modelos y cache Ollama (~GBs)
├── discord-bridge/
│   ├── Dockerfile           # python:3.11-slim + discord.py + requests
│   └── bot.py               # Puente Discord -> n8n webhook
└── DESPLIEGUE.md            # Esta guía
```

---

## ⚠️ Riesgos y limitaciones conocidas

| Riesgo | Mitigación |
|--------|------------|
| **RAM agotada** | Límites en compose + `OLLAMA_NUM_PARALLEL=1` + modelos ≤ 2 GB. Monitorea con `docker stats`. |
| **Token Discord en .env** | `.env` en `.gitignore`. Nunca commitees. Rota token si se filtra. |
| **Ollama solo CPU** | Inferencia más lenta. Para producción con GPU real, cambia a `deploy.resources.reservations.devices` con `driver: nvidia` o `driver: amd` (ROCm). |
| **Red internal** | No puedes acceder a Ollama (11434) desde el host. Usa `docker exec ollama ollama ...` o expón puerto temporal si necesitas. |
| **discord-bridge depende de n8n healthy** | Si n8n tarda en subir, discord-bridge espera. Healthcheck en n8n mitiga. |

---

## 📝 Notas para el usuario

- **Edita `.env` ANTES del primer `docker compose up -d`** — el bot no arranca sin `DISCORD_TOKEN`.
- **Webhook n8n:** En n8n, crea un webhook en `/webhook/redteam` (POST) que reciba el JSON que manda `bot.py` (`prompt`, `session_id`, `discord`).
- **Timezone:** `America/Bogota` configurado en n8n. Cambia en `docker-compose.yml` si estás en otra zona.
- **shortcut:** `OLLAMA_FLASH_ATTENTION=1` acelera atención en CPU; desactiva si hay inestabilidad.