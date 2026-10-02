# Operação do catálogo governado

O fluxo privado é composto por aquisição, processamento, revisão, correção, aprovação e retirada. Execute as etapas no backend com acesso autorizado ao PostgreSQL. A CI usa fixtures e não chama a API. O Compose publica frontend e API somente em `127.0.0.1`; a projeção local não deve ser exposta a interfaces de rede externas.

## Pré-requisitos da carga RetroAchievements

Configure `RETRO_ACHIEVEMENTS_KEY` (ou `RETROACHIEVEMENTS_API_KEY`) no `.env` privado de `retrovault/`; o Compose encaminha a chave somente ao backend sob o nome `RETROACHIEVEMENTS_API_KEY`. Não passe a chave como argumento nem a registre em logs. A cache de respostas, catálogos, páginas e manifests é persistida no volume privado `retroachievements-cache`.

Para executar a ingestão do SNES pelo PostgreSQL Compose:

```bash
docker compose up -d db backend
docker compose exec backend python -m app.modules.data_governance.api.cli ra-snes-ingest
```

A primeira operação descobre sistemas ativos de jogos e enumera o SNES (`ID 3`) com `f=1`, usando offsets e lotes paginados. Por padrão, a página contém até 100 jogos e cada manifesto contém até 1.000 IDs. Catálogo e manifests ficam em cache; repetir a operação continua os lotes ainda não concluídos e reaproveita respostas cacheadas. Se uma execução concluída precisar ser refeita, passe `--attempt <identificador>` para criar novas versões de ingestão a partir das páginas de catálogo cacheadas; as execuções anteriores permanecem auditáveis e os detalhes dos jogos são consultados novamente. Para refazer também a enumeração, passe `--refresh-cache`; isso cria uma nova versão de catálogo. `--page-size` e `--batch-size` permitem reduzir os lotes sem alterar os limites máximos. O comando não publica automaticamente.

Uma capa obtida da API fica disponível para revisão privada, mas a evidência original conserva os direitos de publicação como desconhecidos. O responsável pelo projeto declarou que o catálogo é um portfólio particular e invocou a exceção de uso privado da seção Copyrights dos [termos da RetroAchievements](https://retroachievements.org/terms). A decisão é registrada em tabela append-only por capa, com essa base, o escopo `loopback_only`, data e ator; ela não afirma que a RetroAchievements concedeu uma autorização geral. O comando de publicação verifica a plataforma SNES, a capa válida e a atribuição `RetroAchievements`, processa com `editorial-v3` e publica os candidatos elegíveis individualmente com ETag, motivo e chave de idempotência. O campo `publisher` continua sendo metadado do jogo fornecido pela API, não uma declaração de distribuidor ou titular da capa.

Para publicar o catálogo privado SNES já ingerido, informe exatamente `catalog_version` retornada por `ra-snes-ingest` (sem o sufixo `-n...-b....`) e copie do mesmo resultado a contagem de jogos, lotes e o tamanho dos lotes. A verificação exige todas as evidências preservadas, todos os lotes contínuos e um único tamanho de lote escolhido; assim uma execução parcial não é publicada como catálogo completo:

```bash
docker compose exec backend python -m app.modules.data_governance.api.cli ra-snes-private-publish \
  --catalog-version console-3-<hash32>[-timestamp-sorteio][-attempt-id] \
  --expected-game-count <game_count> \
  --expected-batch-count <batch_count> \
  --batch-size <batch_size>
```

O comando é retomável: decisões, aprovações e projeção são idempotentes e cada jogo conserva sua trilha de auditoria. Candidatos sem capa válida, atribuição ou campos obrigatórios ficam fora da publicação e aparecem resumidos na saída.

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
scripts/catalog-ingest.sh ra-snes-ingest [--page-size 100] [--batch-size 1000] [--refresh-cache] [--attempt retry1]
scripts/catalog-ingest.sh ra-snes-private-publish --catalog-version <versão-do-catálogo> --expected-game-count <jogos> --expected-batch-count <lotes> [--batch-size <tamanho>]
scripts/catalog-ingest.sh process <run-id>
scripts/catalog-ingest.sh process-summary <run-id>
scripts/catalog-ingest.sh review <run-id> <id-externo>
scripts/catalog-ingest.sh correct <run-id> <id-externo> --field title --value "Título corrigido" --reason "Motivo" --etag '"etag-atual"'
scripts/catalog-ingest.sh approve <run-id> <id-externo> --reason "Revisão concluída" --idempotency-key <chave-aleatória> --etag '"etag-atual"'
scripts/catalog-ingest.sh withdraw <public-id> --reason "Retirada editorial" --idempotency-key <chave-aleatória> --etag '"etag-publicado"'
```

Para corrigir `included_items`, passe o valor JSON com `--value-json`, por exemplo `--value-json '["Manual","Cartucho"]'`. Correções exigem ETag atual, autor Eduardo e motivo não vazio; cada revisão fica registrada sem substituir versões anteriores. A aprovação exige candidato em revisão com capa e direitos validados. Aprovação e retirada usam chave de idempotência e ETag; reutilizar a chave com outro conteúdo ou decidir sobre uma versão obsoleta resulta em conflito.

Antes da primeira carga, aplique as migrations com `uv run --project backend alembic upgrade head`. Não execute aquisição real na CI. As chamadas reais dependem de credencial privada e manifesto operacional aprovado.
