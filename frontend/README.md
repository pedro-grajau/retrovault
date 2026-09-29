# Frontend RetroVault

O frontend é um shell React/Vite para o ambiente Sandbox da RetroVault. Ele
exibe a versão informada pela API, mantém o banner de demonstração e não
implementa catálogo, autenticação, checkout ou painel — essas capacidades
pertencem às histórias seguintes.

## Desenvolvimento local

Na raiz do repositório, instale as dependências travadas:

```bash
bun install --frozen-lockfile
```

Inicie API, banco e frontend juntos:

```bash
docker compose up --wait
```

Abra `http://localhost:5173`. O Compose fornece `VITE_API_URL` e
`VITE_APP_VERSION`. Fora do Compose, `frontend/.env.development` aponta o
servidor Vite local para a API em `http://localhost:8000`; esse valor não é
incluído em builds de produção.

Para iniciar somente o Vite:

```bash
bun run --cwd frontend dev
```

## Contrato OpenAPI

O contrato e o cliente TypeScript são artefatos versionados. Após uma mudança
de API, a partir da raiz do repositório, execute:

```bash
scripts/generate-client.sh
scripts/check-contracts.sh
```

O segundo comando falha se o OpenAPI, o cliente gerado ou o manifesto não
estiverem sincronizados.

## Testes

Instale o Chromium do Playwright uma vez no host:

```bash
bunx playwright install chromium
```

Execute os testes de acessibilidade e responsividade em desktop e celular:

```bash
bun run --cwd frontend test
```

Para verificar também a integração real entre a pilha Compose, a API e o
frontend, execute:

```bash
scripts/compose-smoke.sh
```

O smoke usa um projeto Compose isolado e remove apenas os recursos que criou.
