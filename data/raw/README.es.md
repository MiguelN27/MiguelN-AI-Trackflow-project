# Carpeta `data/raw`

Esta carpeta está pensada para almacenar **datos en bruto** (raw) relacionados con la compañía: dumps, exports, archivos de ejemplo, muestras de eventos, o datasets sin transformar.

- **Propósito principal**: servir como zona de aterrizaje o referencia de datos originales antes de ser procesados por pipelines.
- **Recomendación**: documenta el origen de cada dataset, formato, tamaño esperado, consideraciones de privacidad/PII y cómo se versiona (idealmente evitando subir datos sensibles al repositorio).

## En esta carpeta

- `incidents-trackflow.csv`: el export del helpdesk del que parte `scripts/seed_incidents.py`.
- `prefect-results/` (ignorada por git, la crea el pipeline): los resultados persistidos de las tareas en caché de Prefect. Contienen recuentos por almacén, cliente y semana, nunca filas de eventos, y se pueden borrar en cualquier momento.
- `telemetry_<AAAA-MM-DD>.csv` (ignorados por git, los escribe `scripts/nightly_export.py`): un día UTC de `telemetry_events`, exportado una sola vez como copia de seguridad y de auditoría, con las columnas de la tabla (`id,timestamp,event_type,payload,tags,created_at`). El pipeline lee la base de datos, no estos archivos. Llevan ids seudónimos de usuario y de sesión, por eso quedan fuera de git. Ver `docs/NIGHTLY_EXPORT.md`.
