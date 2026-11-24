"""gunicorn entrypoint: ``gunicorn -b 0.0.0.0:8000 wsgi:app``."""
from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False)
