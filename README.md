# RetroVault

Base operacional importada do Full Stack FastAPI Template 0.12.0.

```bash
export APP_VERSION="$(scripts/app-version.sh)"
docker compose up --build
```

Frontend: `http://localhost:5173`; API: `http://localhost:8000/api/v1`.

`APP_VERSION` é derivada do commit atual. O fallback `dev` existe somente para árvores ainda sem commit. O ambiente é sempre Sandbox e não contém integrações pagas.

Para habilitar o Pixel pelo Telegram, configure `PIXEL_TELEGRAM_BOT_TOKEN`, `PIXEL_TELEGRAM_BOT_USERNAME` (sem `@`), `PIXEL_TELEGRAM_WEBHOOK_SECRET` e `PIXEL_TELEGRAM_ALLOWED_USER_IDS` como IDs separados por vírgula. Use um bot de teste em chat privado e inclua na allowlist somente os IDs autorizados para a demonstração. Segredos ficam no `.env` privado e nunca são enviados ao frontend.

O webhook recebe atualizações em `/api/v1/concierge/telegram/webhook` e valida o header `X-Telegram-Bot-Api-Secret-Token`. Configure o `setWebhook` do bot para apontar a esse endereço público usando o mesmo `secret_token`; uma instância somente em `localhost` não pode receber chamadas do Telegram. CI usa apenas o simulador, sem acessar a API.

Configure também `PIXEL_CONTEXT_REFERENCE_SECRET` com um segredo aleatório de pelo menos 32 bytes. O deep link contextual usa um parâmetro `start` compacto de 56 caracteres (limite Telegram: 64), com nonce aleatório para que cada link de uso único seja distinto, e preserva referências assinadas antigas até expirarem. Ajuste o prazo por `PIXEL_CONTEXT_REFERENCE_TTL_SECONDS` (padrão: 1800 segundos). Mensagens com mais de `PIXEL_TELEGRAM_MESSAGE_MAX_AGE_SECONDS` (padrão: 900) são persistidas para reconciliação sem resposta automática; conversas e checkpoints são retidos por 30 dias por padrão.
