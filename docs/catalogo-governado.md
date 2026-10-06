# Operação do catálogo governado

O fluxo privado é composto por aquisição, processamento, revisão, correção, aprovação e retirada. Execute as etapas no backend com acesso autorizado ao PostgreSQL. A CI usa fixtures e não chama a API. O Compose publica frontend e API somente em `127.0.0.1`; a projeção local não deve ser exposta a interfaces de rede externas.

## Pré-requisitos da carga RetroAchievements

Configure `RETRO_ACHIEVEMENTS_KEY` (ou `RETROACHIEVEMENTS_API_KEY`) no `.env` privado de `retrovault/`; o Compose encaminha a chave somente ao backend sob o nome `RETROACHIEVEMENTS_API_KEY`. Não passe a chave como argumento nem a registre em logs. A cache de respostas, catálogos, páginas e manifests é persistida no volume privado `retroachievements-cache`.

Para executar a ingestão por console pelo PostgreSQL Compose:

```bash
docker compose up -d db backend
docker compose exec backend python -m app.modules.data_governance.api.cli ra-console-ingest --console-id 3
```

A allowlist operacional aceita IDs RetroAchievements `1` Mega Drive, `2` Nintendo 64, `3` SNES e `12` PlayStation. `ra-console-ingest --console-id <id>` descobre sistemas ativos e enumera todos os jogos com conquistas daquele console (`f=1`), usando offsets e lotes paginados. A carga completa por console pode ultrapassar os cerca de 300 jogos projetados no PRD; a decisão aprovada prioriza carregar o inventário inteiro de jogos com conquistas. Isso não aumenta a meta comercial: somente ofertas de jogos contam para os 60–100 SKUs e ofertas não são derivadas de cada jogo ingerido.

Por padrão, a página contém até 100 jogos e cada manifest contém 100 IDs para manter cada snapshot abaixo do limite agregado de 64 MB. O limite máximo continua sendo 1.000 IDs. Catálogo e manifests ficam em cache; repetir a operação continua os lotes ainda não concluídos e reaproveita respostas cacheadas. Se uma execução concluída precisar ser refeita, passe `--attempt <identificador>` para criar novas versões de ingestão a partir das páginas de catálogo cacheadas; as execuções anteriores permanecem auditáveis e os detalhes dos jogos são consultados novamente. Para refazer também a enumeração, passe `--refresh-cache`; isso cria uma nova versão de catálogo. `--page-size` e `--batch-size` permitem ajustar os lotes sem alterar os limites máximos. O comando não publica automaticamente. O alias legado `ra-snes-ingest` continua equivalente a `ra-console-ingest --console-id 3`.

Uma capa obtida da API fica disponível para revisão privada, mas a evidência original conserva os direitos de publicação como desconhecidos. O responsável pelo projeto declarou que o catálogo é um portfólio particular e invocou a exceção de uso privado da seção Copyrights dos [termos da RetroAchievements](https://retroachievements.org/terms). A decisão é registrada em tabela append-only por capa, com essa base, o escopo `loopback_only`, data e ator; ela não afirma que a RetroAchievements concedeu uma autorização geral. O comando generalizado verifica que o console da versão do catálogo e a plataforma de cada registro correspondem, exige capa válida e atribuição `RetroAchievements`, processa com `editorial-v3` e publica os candidatos elegíveis individualmente com ETag, motivo e chave de idempotência. `publication_right=denied`, mídia inválida, sem atribuição ou sem direitos elegíveis permanece em quarentena. O campo `publisher` continua sendo metadado do jogo fornecido pela API, não uma declaração de distribuidor ou titular da capa.

Para registrar uso privado e publicar um catálogo permitido já ingerido, informe o mesmo `--console-id` e exatamente `catalog_version` retornada por `ra-console-ingest` (sem o sufixo `-n...-b....`) e copie do mesmo resultado a contagem de jogos, lotes e o tamanho dos lotes. A verificação exige todas as evidências preservadas, todos os lotes contínuos e um único tamanho de lote escolhido; assim uma execução parcial ou de outro console não é publicada como catálogo completo:

```bash
docker compose exec backend python -m app.modules.data_governance.api.cli ra-console-private-publish \
  --console-id 3 \
  --catalog-version console-3-<hash32>[-timestamp-sorteio][-attempt-id] \
  --expected-game-count <game_count> \
  --expected-batch-count <batch_count> \
  --batch-size <batch_size>
```

O comando é retomável: decisões, aprovações e projeção são idempotentes e cada jogo conserva sua trilha de auditoria. Candidatos sem capa válida, atribuição ou campos obrigatórios ficam fora da publicação e aparecem resumidos na saída.

O alias legado `ra-snes-private-publish` continua fixando `--console-id 3`. Para os demais consoles, use o comando generalizado e a versão `console-1-...`, `console-2-...` ou `console-12-...` retornada pela respectiva ingestão. Toda publicação privada depende de Compose local, banco migrado, seleção revisada e serviço limitado a `127.0.0.1`; não exponha essa projeção em rede pública.

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

Cada ID é consultado individualmente pelo endpoint oficial Get Game e a resposta tem de corresponder ao console indicado no comando. O adapter mapeia PlayStation, SNES, Mega Drive e Nintendo 64. Preserva a data `Released` e a precisão `ReleasedAtGranularity` (`year`, `month` ou `day`), além do ano derivado para exibição. Uma capa ausente, inacessível, inválida ou com direitos/atribuição não confirmados mantém o candidato em quarentena. Falhas de um ID não interrompem a aquisição dos demais.

O manifesto aceita até 1.000 IDs. Cada aquisição tem limite total de 15 minutos e retém no máximo 64 MB de payloads mapeados, respostas originais e mídia; IDs que excederem esses limites ficam registrados como falha para aquela execução. Repetir um manifesto já concluído, com os mesmos bytes, devolve o resumo persistido sem consultar novamente a API. Reutilizar a versão com conteúdo diferente continua sendo conflito.

## Comandos privados

```text
scripts/catalog-ingest.sh ra-ingest /caminho/privado/manifesto.json
scripts/catalog-ingest.sh ra-ingest /caminho/privado/manifesto.json --console-id <1|2|3|12>
scripts/catalog-ingest.sh ra-snes-ingest [--page-size 100] [--batch-size 100] [--refresh-cache] [--attempt retry1]
scripts/catalog-ingest.sh ra-console-ingest --console-id <1|2|3|12> [--page-size 100] [--batch-size 100] [--refresh-cache] [--attempt retry1]
scripts/catalog-ingest.sh ra-snes-private-publish --catalog-version <versão-do-catálogo> --expected-game-count <jogos> --expected-batch-count <lotes> [--batch-size <tamanho>]
scripts/catalog-ingest.sh ra-console-private-publish --console-id <1|2|3|12> --catalog-version <versão-do-catálogo> --expected-game-count <jogos> --expected-batch-count <lotes> [--batch-size <tamanho>]
scripts/catalog-ingest.sh process <run-id>
scripts/catalog-ingest.sh process-summary <run-id>
scripts/catalog-ingest.sh review <run-id> <id-externo>
scripts/catalog-ingest.sh correct <run-id> <id-externo> --field title --value "Título corrigido" --reason "Motivo" --etag '"etag-atual"'
scripts/catalog-ingest.sh approve <run-id> <id-externo> --reason "Revisão concluída" --idempotency-key <chave-aleatória> --etag '"etag-atual"'
scripts/catalog-ingest.sh withdraw <public-id> --reason "Retirada editorial" --idempotency-key <chave-aleatória> --etag '"etag-publicado"'
```

Para corrigir `included_items`, passe o valor JSON com `--value-json`, por exemplo `--value-json '["Manual","Cartucho"]'`. Correções exigem ETag atual, autor Eduardo e motivo não vazio; cada revisão fica registrada sem substituir versões anteriores. A aprovação exige candidato em revisão com capa e direitos validados. Aprovação e retirada usam chave de idempotência e ETag; reutilizar a chave com outro conteúdo ou decidir sobre uma versão obsoleta resulta em conflito.

Antes da primeira carga, aplique as migrations com `uv run --project backend alembic upgrade head`. Não execute aquisição real na CI. As chamadas reais dependem de credencial privada e manifesto operacional aprovado.

## Quantidade de ofertas e evidências

O seed de Commerce contabiliza linhas de `commerce.offers` ligadas a jogos. Se existirem menos de 60, tenta completar até 60 usando somente jogos publicados; não altera ofertas, preço, unidade, condição, defeitos ou fatos existentes. O teto de criação é 100 ofertas de jogos e o seed não cria ofertas de consoles. A cada execução emite JSON com quantidade anterior, novas ofertas, total e se a faixa de 60–100 foi atingida. Um conjunto já acima de 100 é reportado sem remoções.

Depois da ingestão e revisão local, gere métricas agregadas sem exportar payloads ou mídia bruta:

```bash
docker compose exec -T backend python scripts/catalog-evidence.py > docs/evidencias/catalogo-compose-local.json
```

O relatório registra inventário ativo por plataforma, fingerprint do conjunto, ofertas de jogos por modalidade, completude crítica e opcional, linhagem por atributo e duplicatas exatas/publicadas mais sinais de staging. Inventário ausente é `inconclusive_no_published_games`; nenhum resultado nulo é convertido em sucesso.

## Baseline LCP e acessibilidade manual

Para medir o build Vite de produção servido pelo serviço Compose `frontend-lcp`, com Chromium, viewport fixo de 1365×900 CSS px, DPR 1, uma navegação de aquecimento e 30 amostras por Home, catálogo e detalhe publicado, execute `bash scripts/compose-lcp.sh`. O ambiente requer Bun e Chromium/Playwright instalados no host. O script cria JSON em `docs/evidencias/` contendo as amostras, p75, versão, commit, viewport, conjunto e limitações. Se qualquer amostra LCP estiver ausente, se não houver jogo publicado ou se uma rota falhar, o relatório fica inconclusivo. O limite de 2,5 s é um alvo local de referência; esse baseline não comprova desempenho de produção.

Zoom real não é simulado com CSS. Registre no artefato de evidências as jornadas Home, catálogo e detalhe com zoom do navegador em 200% e reflow a 400%, teclado e leitor de tela usando Orca 46.1 + Firefox 157 no Linux. Registre problema e resultado contra WCAG 2.2 AA; caso a combinação/ambiente não esteja disponível, mantenha o item como pendente.

Evidências locais da execução de fechamento:

- `docs/evidencias/catalogo-compose-local.json` — inventário ativo, ofertas, completude, linhagem e duplicatas.
- `docs/evidencias/lcp-compose-2026-10-06T20-41-02-876Z.json` — baseline final com 30 medições por rota e LCP p75 do build Compose local.
- `docs/evidencias/acessibilidade-manual.md` — validações manuais de teclado, Orca, zoom real e reflow registradas como positivas conforme confirmação do usuário.
