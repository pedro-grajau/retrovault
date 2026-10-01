# RetroVault Backend

API FastAPI do monolito modular RetroVault. O backend usa Python 3.14, dependencias travadas pelo `uv.lock`, PostgreSQL e Alembic.

## Desenvolvimento local

Na raiz do repositorio, suba banco e backend:

```bash
export APP_VERSION="$(scripts/app-version.sh)"
docker compose up --build db backend
```

A API fica em `http://localhost:8000/api/v1`; a documentacao interativa fica em `http://localhost:8000/api/v1/docs`.

Para executar diretamente no host, forneca uma `DATABASE_URL` para um PostgreSQL acessivel pelo host e rode a partir de `backend/`. O banco do Compose padrao usa o hostname interno `db` e nao publica a porta PostgreSQL.

```bash
export DATABASE_URL='postgresql+psycopg://usuario:senha@host:5432/banco'
uv sync --frozen
uv run alembic upgrade head
uv run fastapi dev
```

## Estrutura

Os modulos de dominio ficam em `app/modules/<modulo>/`, separados em `domain`, `application`, `ports`, `adapters` e `api`. Servicos compartilhados de plataforma ficam em `app/platform/`. Imports laterais de adapters e repositories entre modulos sao proibidos pelos testes arquiteturais.

## Ofertas demonstrativas Sandbox

O Compose executa `backend/scripts/seed-sandbox-commerce.py` depois das migrations.
Ele cria ofertas sintéticas apenas para jogos já publicados, com preço, condição,
modalidades e unidades marcados como Sandbox. Após publicar jogos com a API em
execução, rode novamente o seed para acrescentar suas ofertas:

```bash
docker compose exec backend python scripts/seed-sandbox-commerce.py
```

O seed é idempotente e não altera ofertas já existentes. Esses valores não
representam estoque nem preços reais.

## Testes e verificações

A partir de `backend/`:

```bash
uv run ruff check app tests
uv run ty check app
uv run pytest tests/architecture tests/api tests/contract
uv run alembic upgrade head --sql
```

Com a pilha Compose ativa, os testes tambem podem ser executados por:

```bash
docker compose exec -T backend bash scripts/tests-start.sh
```

O script `scripts/prestart.sh` aplica as migrations. Não há carga de usuários ou autenticação nesta história.

## Migrations

As migrations ficam em `app/alembic/versions/`. Para criar e aplicar uma nova revision:

```bash
uv run alembic revision -m "descricao"
uv run alembic upgrade head
```

Cada modulo e o unico escritor de seu schema PostgreSQL. Alteracoes devem manter essa propriedade e usar migrations versionadas.
