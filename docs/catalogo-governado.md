# Operação do catálogo governado

O fluxo privado é composto por aquisição, processamento, revisão, correção, aprovação e retirada. Execute as etapas no backend com acesso autorizado ao PostgreSQL. A CI usa fixtures e não chama a API.

## Pré-requisitos da carga RetroAchievements

Configure `RETROACHIEVEMENTS_API_KEY` no `.env` privado de `retrovault/`; o Compose encaminha a variável somente ao backend. Não passe a chave como argumento nem a registre em logs. A cache de respostas, catálogos, páginas e manifests é persistida no volume privado `retroachievements-cache`.

Para executar a ingestão do SNES pelo PostgreSQL Compose:

```bash
docker compose up -d db backend
docker compose exec backend python -m app.modules.data_governance.api.cli ra-snes-ingest
```

A primeira operação descobre sistemas ativos de jogos e enumera o SNES (`ID 3`) com `f=1`, usando offsets e lotes paginados. Por padrão, a página contém até 100 jogos e cada manifesto contém até 1.000 IDs. Catálogo e manifests ficam em cache; repetir a operação continua os lotes ainda não concluídos e reaproveita respostas cacheadas. Para refazer a enumeração e chamadas, passe `--refresh-cache`; isso cria uma nova versão de catálogo. `--page-size` e `--batch-size` permitem reduzir os lotes sem alterar os limites máximos. O comando não publica automaticamente.

Antes de aprovar candidatos, revise os detalhes privados. Uma capa obtida da API fica disponível para revisão privada, mas direitos de publicação começam como desconhecidos; candidato sem capa válida ou direitos e atribuição confirmados permanece em quarentena. A aprovação continua individual, com ETag, motivo e chave de idempotência.

Para uma carga manual existente, prepare um manifesto versionado fora do repositório com esta forma:

```json
{
  "source": "retroachievements",
  "version": "portfolio-2026-09-30-v1",
  "captured_at": "2026-09-30T12:00:00Z",
  "actor": "Eduardo",
  "record_ids": [123, 456]
}
```

Cada ID é consultado individualmente pelo endpoint oficial Get Game. O adapter mapeia os consoles autorizados: PlayStation, SNES, Mega Drive e Nintendo 64. Preserva a data `Released` e a precisão `ReleasedAtGranularity` (`year`, `month` ou `day`), além do ano derivado para exibição. Uma capa ausente, inacessível, inválida ou com direitos/atribuição não confirmados mantém o candidato em quarentena. Falhas de um ID não interrompem a aquisição dos demais.

O manifesto aceita até 1.000 IDs. Cada aquisição tem limite total de 15 minutos e retém no máximo 64 MB de payloads mapeados, respostas originais e mídia; IDs que excederem esses limites ficam registrados como falha para aquela execução. Repetir um manifesto já concluído, com os mesmos bytes, devolve o resumo persistido sem consultar novamente a API. Reutilizar a versão com conteúdo diferente continua sendo conflito.

## Comandos privados

```text
scripts/catalog-ingest.sh ra-ingest /caminho/privado/manifesto.json
scripts/catalog-ingest.sh ra-snes-ingest [--page-size 100] [--batch-size 1000] [--refresh-cache]
scripts/catalog-ingest.sh process <run-id>
scripts/catalog-ingest.sh process-summary <run-id>
scripts/catalog-ingest.sh review <run-id> <id-externo>
scripts/catalog-ingest.sh correct <run-id> <id-externo> --field title --value "Título corrigido" --reason "Motivo" --etag '"etag-atual"'
scripts/catalog-ingest.sh approve <run-id> <id-externo> --reason "Revisão concluída" --idempotency-key <chave-aleatória> --etag '"etag-atual"'
scripts/catalog-ingest.sh withdraw <public-id> --reason "Retirada editorial" --idempotency-key <chave-aleatória> --etag '"etag-publicado"'
```

Para corrigir `included_items`, passe o valor JSON com `--value-json`, por exemplo `--value-json '["Manual","Cartucho"]'`. Correções exigem ETag atual, autor Eduardo e motivo não vazio; cada revisão fica registrada sem substituir versões anteriores. A aprovação exige candidato em revisão com capa e direitos validados. Aprovação e retirada usam chave de idempotência e ETag; reutilizar a chave com outro conteúdo ou decidir sobre uma versão obsoleta resulta em conflito.

Antes da primeira carga, aplique as migrations com `uv run --project backend alembic upgrade head`. Não execute aquisição real na CI. As chamadas reais dependem de credencial privada e manifesto operacional aprovado.
