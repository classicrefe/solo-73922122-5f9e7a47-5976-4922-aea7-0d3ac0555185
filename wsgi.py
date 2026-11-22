"""WSGI entry point: ``gunicorn wsgi:app`` or ``flask --app wsgi run``."""

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
