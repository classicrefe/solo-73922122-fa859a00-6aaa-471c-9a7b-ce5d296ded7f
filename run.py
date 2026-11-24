"""Local development entrypoint: ``python run.py`` → http://127.0.0.1:8000."""
from wsgi import app

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False)
