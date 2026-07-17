"""
Modal deployment of the search/persona API (persona_api.py) as a public ASGI app.

Credentials are injected at runtime via modal.Secret.from_dotenv - the .env
file is NOT baked into the image. To require authentication on the public
URL, add API_KEY=<secret> to the .env before deploying (clients then must
send it as the X-API-Key header).

Deploy: modal deploy lead_clean/modal_api.py
"""

import os
import modal

app = modal.App("lead-search-api")

image = modal.Image.debian_slim().pip_install(
    "fastapi",
    "uvicorn",
    "boto3",
    "pydantic",
    "python-dotenv",
).add_local_dir(
    os.path.dirname(__file__), remote_path="/root/lead_clean"
)


@app.function(
    image=image,
    secrets=[modal.Secret.from_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))],
)
@modal.asgi_app()
def fastapi_app():
    import sys
    sys.path.append("/root/lead_clean")
    from persona_api import app as api
    return api
