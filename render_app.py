# -*- coding: utf-8 -*-
"""Punto de entrada para Render: la web (en /) y la app de reservas en vivo (en /en-vivo) en UN solo servicio,
para que compartan la misma base de datos SQLite y se sincronicen al instante.

Arranque (lo hace Render):  gunicorn render_app:application --worker-class gthread --workers 1 --threads 16 --timeout 120
"""
from werkzeug.middleware.dispatcher import DispatcherMiddleware

import casapepe_demo as web
import casapepe_reservas_live as live

live.preparar_db()
application = DispatcherMiddleware(web.app, {'/en-vivo': live.app})
