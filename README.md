# RetroVault

Base operacional importada do Full Stack FastAPI Template 0.12.0.

```bash
export APP_VERSION="$(scripts/app-version.sh)"
docker compose up --build
```

Frontend: `http://localhost:5173`; API: `http://localhost:8000/api/v1`.

`APP_VERSION` é derivada do commit atual. O fallback `dev` existe somente para árvores ainda sem commit. O ambiente é sempre Sandbox e não contém integrações pagas.

Para habilitar o Pixel, configure `PIXEL_WHATSAPP_NUMBER` com o número internacional em dígitos e `PIXEL_CONTEXT_REFERENCE_SECRET` com um segredo aleatório de pelo menos 32 bytes no `.env` privado de `retrovault/`. O prazo da referência pode ser ajustado por `PIXEL_CONTEXT_REFERENCE_TTL_SECONDS` (padrão: 1800 segundos). Sem esses valores, a conversa fica indisponível; nenhum segredo é enviado ao frontend.
