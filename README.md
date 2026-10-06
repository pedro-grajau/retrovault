# RetroVault

Base operacional importada do Full Stack FastAPI Template 0.12.0.

```bash
export APP_VERSION="$(scripts/app-version.sh)"
docker compose up --build
```

Frontend: `http://localhost:5173`; API: `http://localhost:8000/api/v1`.

`APP_VERSION` é derivada do commit atual. O fallback `dev` existe somente para árvores ainda sem commit. O ambiente é sempre Sandbox e não contém integrações pagas.

Para habilitar a entrada global do Pixel, configure `PIXEL_WHATSAPP_NUMBER` com o número internacional em dígitos. Para gerar referências contextualizadas na página de um jogo, configure também `PIXEL_CONTEXT_REFERENCE_SECRET` com um segredo aleatório de pelo menos 32 bytes no `.env` privado de `retrovault/`. O prazo da referência pode ser ajustado por `PIXEL_CONTEXT_REFERENCE_TTL_SECONDS` (padrão: 1800 segundos). Sem o número, qualquer conversa fica indisponível; sem o segredo, somente a conversa contextualizada fica indisponível. Nenhum segredo é enviado ao frontend.
