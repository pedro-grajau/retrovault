# Evidência manual de acessibilidade

**Status:** concluído com resultado positivo, conforme confirmação do usuário nesta conversa em 2026-10-06.

## Ambiente registrado

- Sistema: Linux
- Navegador: Firefox 157
- Leitor de tela: Orca 46.1
- Escala: zoom nativo do navegador em 200% e reflow a 400%
- URL local: `http://127.0.0.1:5173`
- Jogo de detalhe disponível: `http://127.0.0.1:5173/games/004bdbac-857f-5b76-84d1-f09a67aebf96`
- Data da confirmação: 2026-10-06
- Responsável pela validação: usuário (resultado informado nesta conversa)
- Data em que a validação foi executada: não informada

## Jornadas

| Rota | Teclado | Orca | Zoom 200% | Reflow 400% | Resultado / observações |
|---|---|---|---|---|---|
| Home `/` | Positivo | Positivo | Positivo | Positivo | Usuário confirmou resultado positivo; sem observações específicas fornecidas. |
| Catálogo `/catalog` | Positivo | Positivo | Positivo | Positivo | Usuário confirmou resultado positivo; sem observações específicas fornecidas. |
| Detalhe `/games/<id>` | Positivo | Positivo | Positivo | Positivo | Usuário confirmou resultado positivo; sem observações específicas fornecidas. |

## Roteiro

1. Percorrer cada jornada sem mouse. Confirmar ordem de foco, foco visível e que menus, busca, filtros, cartões, links e ações funcionam por teclado.
2. Com Orca ativo, verificar título da página, cabeçalhos, landmarks, nome e estado dos controles, leitura dos resultados e anúncio de mudanças após busca/filtro.
3. Usar o zoom nativo do Firefox em 200%. Confirmar que texto, controles e conteúdo continuam disponíveis sem sobreposição ou corte.
4. Configurar reflow equivalente a 400% (viewport de 1280 CSS px reduzido a 320 CSS px). Confirmar que o conteúdo pode ser lido em uma direção, sem rolagem horizontal da página, salvo conteúdo que exija duas dimensões.
5. Registrar cada falha com rota, passos, resultado esperado, resultado observado e critério WCAG relacionado. Salvar data, responsável e resultados em cada célula da tabela acima.

## Critérios de referência

- [1.4.4 Resize Text (AA)](https://www.w3.org/TR/WCAG22/#resize-text)
- [1.4.10 Reflow (AA)](https://www.w3.org/TR/WCAG22/#reflow)
- [2.1.1 Keyboard (A)](https://www.w3.org/TR/WCAG22/#keyboard)
- [2.4.7 Focus Visible (AA)](https://www.w3.org/TR/WCAG22/#focus-visible)
- [2.4.11 Focus Not Obscured (Minimum) (AA)](https://www.w3.org/TR/WCAG22/#focus-not-obscured-minimum)
- [4.1.2 Name, Role, Value (A)](https://www.w3.org/TR/WCAG22/#name-role-value)

## Proveniência do resultado

O usuário confirmou que as validações manuais foram positivas. Não foram fornecidos detalhes de execução ou observações por rota; a tabela registra apenas esse resultado informado. Esta confirmação manual complementa, mas não substitui, a evidência automatizada de DOM/Axe.
