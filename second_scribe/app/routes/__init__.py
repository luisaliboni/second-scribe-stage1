"""API route modules. Each module here registers its routes with ``@api.route`` on import; the server imports them
all at start (``api.load_route_modules``). One module per task area, so tasks never edit each other's lines."""
