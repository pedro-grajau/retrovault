# RetroVault

Base operacional importada do Full Stack FastAPI Template 0.12.0.

```bash
export APP_VERSION="$(scripts/app-version.sh)"
docker compose up --build
```

Frontend: `http://localhost:5173`; API: `http://localhost:8000/api/v1`.

`APP_VERSION` é derivada do commit atual. O fallback `dev` existe somente para árvores ainda sem commit. O ambiente é sempre Sandbox e não contém integrações pagas.
