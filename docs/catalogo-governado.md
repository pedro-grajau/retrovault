# Operação do catálogo governado

O fluxo privado é composto por aquisição, processamento, revisão, correção, aprovação e retirada. Execute as etapas no backend com acesso autorizado ao PostgreSQL. A CI usa fixtures e não chama a API.

## Pré-requisitos da carga RetroAchievements

Configure `DATABASE_URL` e `RETROACHIEVEMENTS_API_KEY` na configuração privada do backend. A chave não é argumento de CLI e não deve ser registrada em logs. Prepare um manifesto versionado fora do repositório com esta forma:

```json
{
  "source": "retroachievements",
  "version": "portfolio-2026-09-30-v1",
  "captured_at": "2026-09-30T12:00:00Z",
  "actor": "Eduardo",
  "record_ids": [123, 456]
}
```

Cada ID é consultado individualmente pelo endpoint oficial Get Game. O adapter mapeia os consoles autorizados: PlayStation, SNES, Mega Drive e Nintendo 64. Uma capa ausente, inacessível, inválida ou com direitos/atribuição não confirmados mantém o candidato em quarentena. Falhas de um ID não interrompem a aquisição dos demais.

## Comandos privados

```text
scripts/catalog-ingest.sh ra-ingest /caminho/privado/manifesto.json
scripts/catalog-ingest.sh process <run-id>
scripts/catalog-ingest.sh process-summary <run-id>
scripts/catalog-ingest.sh review <run-id> <id-externo>
scripts/catalog-ingest.sh correct <run-id> <id-externo> --field title --value "Título corrigido" --reason "Motivo" --etag '"etag-atual"'
scripts/catalog-ingest.sh approve <run-id> <id-externo> --reason "Revisão concluída" --idempotency-key <chave-aleatória> --etag '"etag-atual"'
scripts/catalog-ingest.sh withdraw <public-id> --reason "Retirada editorial" --idempotency-key <chave-aleatória> --etag '"etag-publicado"'
```

Para corrigir `included_items`, passe o valor JSON com `--value-json`, por exemplo `--value-json '["Manual","Cartucho"]'`. Correções exigem ETag atual, autor Eduardo e motivo não vazio; cada revisão fica registrada sem substituir versões anteriores. A aprovação exige candidato em revisão com capa e direitos validados. Aprovação e retirada usam chave de idempotência e ETag; reutilizar a chave com outro conteúdo ou decidir sobre uma versão obsoleta resulta em conflito.

Antes da primeira carga, aplique as migrations com `uv run --project backend alembic upgrade head`. Não execute aquisição real na CI. As chamadas reais dependem de credencial privada e manifesto operacional aprovado.
