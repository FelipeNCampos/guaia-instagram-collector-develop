# Roadmap – Coleta de Mídias (Fotos e Vídeos)

Este roadmap organiza a evolução da coleta de mídias do **GuaIA Instagram Collector** em quatro fases, com foco em separar fotos/vídeos, armazenar em banco e controlar comportamento por ambiente (desenvolvimento vs produção).

---

## Visão geral (Kanban)

### To Do

- Fase 1 – Fundação de ambiente e banco
- Fase 2 – Extração de múltiplas mídias (DOM → objetos + banco)
- Fase 3 – Arquivamento local vs Google Drive (APP_ENV em ação)
- Fase 4 – Integração GuaIA, observabilidade e testes

### Doing

- (preencher conforme o time iniciar as fases)

### Done

- (preencher conforme as fases forem concluídas)

---

## Fase 1 – Fundação de ambiente e banco

**Objetivo:** criar a base para diferenciar desenvolvimento/produção e armazenar mídias em uma tabela própria, sem alterar ainda o fluxo de scraping.

### Itens

- Adicionar `APP_ENV` ao `.env.example` com valores esperados:
  - `APP_ENV=development` → mídia em disco local.
  - `APP_ENV=production` → mídia via Google Drive.
- Criar módulo de configuração (`config.py` ou `EnvironmentConfig`) que:
  - Exponha funções como `get_app_env()`, `is_development()`, `is_production()`.
  - Leia de env as variáveis: `POSTGRES_*`, `INSTAGRAM_DOWNLOAD_MEDIA`, `INSTAGRAM_MEDIA_DOWNLOAD_TIMEOUT_SECONDS`, `INSTAGRAM_MEDIA_MAX_BYTES`, `GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON`, `GOOGLE_DRIVE_FOLDER_ID`, `TWOCAPTCHA_API_KEY`.
- Adicionar tabela `post_media` ao `_DDL` de `db.py`, com índices por `post_id`/`post_url`.
- Implementar método `upsert_post_media(media_items: list[dict])` em `db.py`, seguindo o padrão de `upsert_posts`.

### Entregáveis da Fase 1

- Variável `APP_ENV` disponível e documentada no `.env.example`.
- Módulo de configuração centralizado (`config.py` / `EnvironmentConfig`) em uso.
- Tabela `post_media` criada e migrada automaticamente junto com as demais tabelas.
- Método `upsert_post_media` pronto para uso, mesmo que ainda não seja chamado pelo coletor.

---

## Fase 2 – Extração de múltiplas mídias

**Objetivo:** permitir que o coletor entenda múltiplas mídias por post (carrossel), separando imagens e vídeos, e registrando tudo em `post_media`.

### Itens

- Criar `media_extractor.py` contendo:
  - Função ou classe `extract_media(dom, post_url, post_id) -> list[dict]`.
  - Cada mídia com campos: `post_id`, `post_url`, `media_index`, `media_type` (`IMAGE`/`VIDEO`/`CAROUSEL_FRAME`), `media_url`, `thumbnail_url`, `mime_type`, `duration_seconds`.
- Implementar `MediaClassifier` (no mesmo módulo ou separado):
  - Funções `is_image(node)`, `is_video(node)`, `is_carousel(node)`, `resolve_mime_type(node)`.
- Integrar `collector.py` com o `MediaExtractor`:
  - No fluxo de enriquecimento do post, extrair DOM e chamar `extract_media(...)`.
  - Anexar o resultado à estrutura do post (ex.: campo `_media_items`).
- Chamar `Database.upsert_post_media(post['_media_items'])` logo após `upsert_posts`.

### Entregáveis da Fase 2

- Para cada post coletado, a tabela `post_media` contém 0..N registros de mídias associadas.
- Posts de carrossel têm múltiplas linhas em `post_media`, com `media_index` consistente.
- Não há ainda obrigação de download de arquivos nem upload para o Google Drive (somente URLs e metadados).

---

## Fase 3 – Arquivamento local vs Google Drive

**Objetivo:** implementar o fluxo de download de mídias com comportamento dependente de `APP_ENV`, separando claramente desenvolvimento (disco local) e produção (Google Drive).

### Itens

- Revisar e evoluir `media_archiver.py` para usar `APP_ENV`:
  - Em desenvolvimento (`APP_ENV=development`):
    - Salvar imagens em `./media/photos/{post_id}_{index}.ext`.
    - Salvar vídeos em `./media/videos/{post_id}_{index}.ext`.
  - Em produção (`APP_ENV=production`):
    - Baixar para arquivos temporários (ex.: `/tmp/...`).
    - Após upload para o Drive, remover arquivos temporários.
- Implementar estratégias específicas (funções ou classes):
  - `archive_photo_local(media_item)` e `archive_video_local(media_item)`.
  - `archive_photo_drive(media_item)` e `archive_video_drive(media_item)`.
- Expor uma API única em `media_archiver.py`:
  - Por exemplo, `archive_media(media_items)` que decide internamente se usa a estratégia local ou Drive.
- Ajustar `google_drive.py` (se necessário) para:
  - Ler `GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON` e `GOOGLE_DRIVE_FOLDER_ID` via módulo de configuração.
  - Suportar subpastas por tipo (ex.: `photos/`, `videos/`).
- Após upload bem-sucedido:
  - Preencher `drive_file_id` e `drive_url` em cada `media_item`.
  - Atualizar os registros em `post_media` com essas informações.

### Entregáveis da Fase 3

- Em desenvolvimento, mídias estão salvas no disco local sob `./media/photos` e `./media/videos`.
- Em produção, as mídias são enviadas ao Google Drive, com arquivos temporários limpos.
- `post_media.drive_file_id` e `post_media.drive_url` passam a ser preenchidos para mídias processadas com sucesso.

---

## Fase 4 – Integração GuaIA, observabilidade e testes

**Objetivo:** fechar o ciclo fim-a-fim com GuaIA, logs úteis e cenários de teste para garantir estabilidade em produção.

### Itens

- Revisar formato de payload do `instagram_api_feeder.py` / `pyGuaIA_API.py`:
  - Confirmar se hoje é enviado somente um `media_url` ou se há suporte a múltiplas mídias.
  - Se necessário, estender o payload para incluir um array `media` com itens `{ media_type, media_url, drive_url, thumbnail_url, ... }`.
- Garantir retrocompatibilidade ou introduzir versão de contrato (se o GuaIA já estiver em produção).
- Adicionar logs específicos ao pipeline de mídia:
  - Início/fim de download por mídia (URL, tipo, tamanho aproximado).
  - Resultado do upload (OK/FALHA, `drive_file_id`).
  - Erros de mídia registrados sem derrubar a coleta do post.
- Definir e executar cenários de teste:
  - **Cenário 1 – APP_ENV=development**:
    - `INSTAGRAM_DOWNLOAD_MEDIA=true`, sem credenciais de Drive obrigatórias.
    - Verificar que `post_media` é preenchida e arquivos vão para `./media/...`.
  - **Cenário 2 – APP_ENV=production**:
    - 2Captcha e Drive configurados.
    - Verificar que arquivos locais são temporários, Drive recebe as mídias e `post_media.drive_url` é populado.

### Entregáveis da Fase 4

- API GuaIA consumindo e entendendo múltiplas mídias por post (quando aplicável).
- Logs e métricas suficientes para operar o coletor em produção com visibilidade de problemas de mídia.
- Pipeline validado em dois modos: desenvolvimento e produção, com comportamento coerente em cada um.

---

## Como usar este roadmap

- Use as seções **To Do / Doing / Done** acima como quadro Kanban textual.
- Mova as fases entre as colunas conforme o time avança.
- Dentro de cada fase, marque os itens concluídos no próprio arquivo ou em issues do repositório.
