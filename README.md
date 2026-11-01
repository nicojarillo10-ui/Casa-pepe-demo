# Casa-pepe-demo
# Casa Pepe · Web y reservas en vivo

Web de Casa Pepe (Flask) con panel de reservas en vivo, en un solo servicio. Usa SQLite.

## Archivos
- `casapepe_demo.py`: la web (en `/`)
- `casapepe_reservas_live.py`: reservas en vivo (en `/en-vivo`)
- `render_app.py`: une las dos apps para que compartan la base de datos
- `requirements.txt`: dependencias (flask, gunicorn, tzdata)

## Despliegue en Render
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `gunicorn render_app:application --worker-class gthread --workers 1 --threads 16 --timeout 120`

## Variables de entorno
| Variable | Para qué sirve |
|---|---|
| `CASAPEPE_ENV` | `production` |
| `CASAPEPE_SECRET_KEY` | Texto secreto de 32 caracteres o más |
| `CASAPEPE_ADMIN_PASSWORD` | Clave de los paneles de la web (16 caracteres o más) |
| `CASAPEPE_PUBLIC_URL` | URL pública HTTPS, sin `/` final |
| `CASAPEPE_TRUST_PROXY` | `1` (Render usa proxy) |
| `CASAPEPE_DB_PATH` | Ruta de la base de datos SQLite |
| `CASAPEPE_LIVE_PASSWORD` | Clave de la app en vivo |
| `CASAPEPE_LIVE_HTTPS` | `1` para cookies seguras |
| `CASAPEPE_RESERVAS_SECRET` | Texto secreto para las sesiones de la app en vivo |

## Aviso
En el plan gratuito de Render la base de datos SQLite se borra en cada reinicio o redespliegue. Para uso real hace falta un disco persistente (plan de p
