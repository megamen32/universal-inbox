# Universal Inbox — подробный план развития

Дата: 2026-08-07
Статус: план развития; реализация выполняется отдельными вертикальными срезами.

## 1. Продуктовая цель

Universal Inbox — независимый от конкретного агента слой, который принимает
нормализованные входящие из почты, мессенджеров, социальных сетей и документов.
Он хранит факты, положение курсоров, результаты дедупликации и receipts явно
запрошенных действий. Hermes, Codex и Claude являются **consumers**: они
получают ограниченный набор фактов, при необходимости читают источник через
adapter и сами формулируют полезную человеку сводку.

Это не "LLM внутри базы" и не замена почтовому или Telegram клиенту. Его
цель — не дать важному сообщению потеряться и сделать поиск по разнородным
источникам предсказуемым.

### Целевой пользовательский сценарий

1. В Gmail, Telegram, WhatsApp, VK или документном источнике появляется
   входящий объект.
2. Соответствующий adapter отдаёт Core нормализованный preview и свой cursor.
3. Core идемпотентно сохраняет объект и выдаёт consumer'у bounded кандидаты.
4. Consumer определяет, что заслуживает внимания, и пишет краткую сводку в
   текущий Hermes Telegram-топик.
5. По явной команде пользователя consumer вызывает allowlisted действие:
   открыть деталь, отправить ответ или скачать attachment. Adapter возвращает
   receipt; Core делает его durable и показывает результат пользователю.
6. Один и тот же объект, повторный poll, restart consumer'а или повторная
   доставка не создают дубликат и не вызывают скрытую отправку.

## 2. Неподвижные границы

| Зона | Владелец | Не делает |
| --- | --- | --- |
| Adapter | source login, source cursor, source-specific read/action | не пишет LLM-summary и не владеет общей дедупликацией |
| Core | contracts, normalisation, durable state, search, dedupe, action receipts | не хранит provider credentials, не логинится, не формирует текст человеку |
| MCP transport | bounded protocol exposure, input validation, process lifecycle | не интерпретирует важность и не обходит Core |
| Consumer agent | topic context, prioritisation, summary, confirmation of requested write | не дублирует cursor/dedupe состояние |
| Hermes delivery | доставка в выбранный Telegram topic и retry policy | не владеет inbox data model |

Обязательные правила:

- Provider credentials и MTProto/browser sessions никогда не экспортируются,
  не копируются между runtime и не выводятся в логи.
- Source writes всегда идут только по явному пользовательскому намерению;
  poll и автоматическая summary — read-only.
- Один MTProto session owner на один daemon. BrowserOS-session не является
  заменой MTProto session.
- Ошибка одного источника не блокирует поиск и digest по остальным; она
  возвращается consumer'у как bounded source-status.
- Нет silent fallback с MTProto на browser scraping или наоборот: source и
  representation явно видны в каждом результате.

## 3. Текущее состояние (факт, не план)

### Уже реализовано в `universal-inbox`

- Канонические Python contracts: `ItemIdentity`, `InboxItem`, `InboxCursor`,
  `PollBatch`, `ExplicitAction`, `ActionReceipt`, `ContentState`.
- SQLite store с source cursor, replay-safe dedupe, conflict detection,
  локальным preview search, recent candidates и idempotent accepted receipts.
- Read-only in-process facade: `inbox.search`, `inbox.digest_candidates`,
  `inbox.get`.
- 11 focused tests; отдельная only-new product-surface проверка прошла на
  временной SQLite базе.

### Уже существует вне Core

- Hermes Gmail unified-inbox slice с двумя Gmail accounts и Inbox/Spam.
- User-owned Telegram MCP bridge with `chats/read/send`; Core consumes only its
  provider-neutral read seam and does not own its credentials or session.
- Локальный fork `forks/mcp-telegram-inbox` от `@overpod/mcp-telegram`.
  Его публичная registration boundary ограничена восемью tools: status,
  login/logout, list chats, read messages, unread, explicit send и download.

### Ещё отсутствует

- Реальный stdio/streamable MCP transport для Python Core.
- Adapter protocol и adapter registry.
- Ни один source adapter, подключённый непосредственно к Core.
- Scheduler/event loop, source health state, relevance features, summary
  request/response contract и Hermes consumer bridge.
- VK, WhatsApp и documents adapters.
- Любой Universal Core production deployment, cutover или отключение legacy
  mail polling.

## 4. Целевая архитектура

```text
Gmail ──────┐
Telegram ───┤
WhatsApp ───┼── adapters ──> Universal Inbox Core ──> MCP ──> Hermes/Codex/Claude
VK ─────────┤                        │                              │
Documents ──┘                        ├── SQLite / search              └── agent-written summary
                                     └── action receipts                         │
                                                                               Telegram topic
```

### Нормализованный item

Каждый adapter обязан вернуть как минимум:

- `source` — стабильный adapter/source account identifier;
- `item_id` — стабильный source object identifier;
- `title`, `body` — ограниченный preview без превращения reader в архив;
- `refs` — provenance ссылки на конкретный item;
- `cursor` — opaque cursor, принадлежащий источнику;
- `content_state` — `present` или `tombstoned`;
- adapter metadata: received timestamp, thread/chat identity, sender label,
  attachment descriptors, unread/importance features при их наличии.

Source payload и cursor — разные сущности. Replay одного item с новым cursor
не конфликтует с immutable item и обязан продвинуть source cursor.

### Минимальный adapter protocol

```python
class InboxAdapter(Protocol):
    adapter_id: str
    capabilities: frozenset[Capability]

    def status(self) -> SourceStatus: ...
    def poll(self, cursor: InboxCursor | None, *, limit: int) -> PollBatch: ...
    def get(self, identity: ItemIdentity) -> InboxItem | None: ...
    def execute(self, action: ExplicitAction) -> ActionReceipt: ...
```

`execute` существует только для capability `EXPLICIT_ACTION`; Core никогда не
вызывает его во время poll, digest или summary. Реальные transport details
(MCP stdio, HTTP, provider SDK) остаются внутри adapter; Telegram BrowserOS не
является допустимым transport fallback.

## 5. Порядок реализации

Каждая фаза завершается отдельной business canary, independent review и
only-new тестом. Никакой production cutover не начинается, пока не пройдена
канарейка предыдущей фазы.

### Фаза A — оформить Core как настоящий MCP service

**Цель:** consumer может пользоваться Core не импортируя Python-модули.

Работы:

1. Выбрать единственный transport: сначала stdio MCP, затем при необходимости
   streamable HTTP за локальным auth boundary.
2. Завести MCP tool schemas для `inbox.search`, `inbox.digest_candidates`,
   `inbox.get`, `inbox.sources.status`.
3. Добавить явные response envelopes: `items`, `next_cursor`,
   `partial_failures`, `provenance`, `request_id`.
4. Добавить limits на query length, item count, preview size и result size.
5. Зафиксировать machine-readable tool manifest и conformance test client.
6. Добавить service entry point и безопасную SQLite path policy.

Канарейка: новый stdio MCP process отвечает на `initialize`, `tools/list`,
`inbox.search` и `inbox.digest_candidates` временной базой; неизвестный tool
и invalid input fail-closed.

Готовность: реальный MCP client видит только documented Core tools; facade и
transport дают одинаковые results.

### Фаза B — adapter registry и orchestrated polling

**Цель:** Core принимает не тестовые items, а batch от нескольких адаптеров.

Работы:

1. Реализовать `InboxAdapter`, `SourceStatus`, adapter manifest и registry.
2. Добавить `poll_once(adapter_id)` и `poll_all()` без параллельных вызовов к
   одному source/session owner.
3. Сохранять source status: last success, last attempted, cursor, item count,
   bounded error class и retry-after.
4. Сохранять один poll receipt с adapter id, request id и accepted item ids.
5. Разделить permanent source failure, transient network failure и malformed
   adapter response; не двигать cursor после частичного/недостоверного batch.
6. Ввести per-source lock для рестартов и overlapping scheduler ticks.
7. Превратить `digest_candidates` из "recent rows" в bounded cross-source
   selection с объяснимыми factual signals, без LLM scoring.

Канарейка: два fake adapters возвращают overlapping/replayed batches; после
рестарта Core items не дублируются, cursor не откатывается, сбой одного
adapter отображается как partial failure.

### Фаза C — Gmail adapter (первый production source)

**Цель:** заменить Hermes-specific Gmail ingestion на Universal Core adapter,
сохранив две account × Inbox/Spam lanes.

Работы:

1. Выделить Gmail read adapter на существующем официальном/плагинном
   credential seam, не копируя auth material в Core.
2. Явно моделировать source id: account + label (`INBOX`, `SPAM`), чтобы
   одинаковое письмо в разных views не конфликтовало случайно.
3. Poll: metadata/preview first; full body только после explicit `get`.
4. Normalise Gmail thread/message identity, sender/subject/date/labels и
   attachment descriptors.
5. Реализовать search через Core index; provider search использовать только
   как opt-in fallback с источником результата.
6. Явные Gmail действия: read detail, скачать attachment, draft/send/reply —
   только после отдельного подтверждения и точного Gmail message/thread id.
7. Сопоставить текущие Hermes cursors/fingerprints с import strategy, не
   переотправляя historical mail в topic.

Канарейка: новая тестовая Gmail message появляется один раз в Core search и
один раз в consumer digest; replay и restart не создают вторую delivery.

### Фаза D — Telegram adapter

#### D1. Выбрать runtime, не смешивая representations

Выбран единый прямой Telegram MCP/MTProto adapter. BrowserOS Telegram Web не
является Core runtime и не должен возвращаться как скрытый fallback.

#### D2. BrowserOS adapter — удалён из Core

Не добавлять Telegram Web/BrowserOS adapter, browser profile, DOMSnapshot или
browser cookie/session path. Любой read path должен входить через
provider-neutral adapter seam и user-owned Telegram MCP runtime.

#### D3. Telegram MCP/MTProto adapter (полный capability путь)

1. Довести локальный fork до доказуемо минимального public manifest из восьми
   tools; удалить/не регистрировать остальной surface.
2. Оформить единственный daemon owner и private session path с permissions.
3. User сам завершает QR/2FA login. Никакой session export/import/browser
   cookie migration.
4. Adapter bridge вызывает только list/read/unread/status в poll; send/download
   доступны только через `ExplicitAction` с durable human evidence.
5. Сопоставить `telegram chat + message id` с Universal `ItemIdentity`.
6. Добавить attachment metadata, bounded download policy, file receipt и
   failure classification.
7. Добавить daemon restart/reconnect canary без parallel session owner.

Канарейка: после user-owned login adapter status authenticated, poll видит
новое unread message, Core dedupe survives restart, explicit test send/download
требует exact user request и возвращает receipt.

Текущий blocker D3: upstream GramJS fork требует `TELEGRAM_API_ID/HASH`; также
указанный `tg.bezrabotnyi.com` runtime оказался static preview, а не MTProto
daemon. Не начинать deploy sidecar без отдельного approval.

### Фаза E — Hermes consumer и agent-authored summary

**Цель:** пользователь получает не сырые rows, а полезную краткую сводку в
текущем Hermes Telegram topic.

Работы:

1. Добавить Hermes Core MCP client как plugin/config extension, не patching
   Hermes core.
2. Consumer вызывает `digest_candidates` с topic context и source status.
3. Agent применяет явную policy: важные запросы, дедлайны, личные сообщения,
   новые unread, failures; рекламу и noisy bulk не пересказывает без причины.
4. Перед summary agent при необходимости вызывает `inbox.get`; в summary
   возвращает source/title/sender/time и action suggestion, а не галлюцинацию.
5. Delivery использует текущий Hermes delivery router и idempotency key
   `(topic, item ref, summary version)`; downstream delivery остаётся
   at-least-once, поэтому duplicate suppression хранится выше отправки.
6. Реализовать explicit user commands: "найди …", "покажи письмо …",
   "скачай файл …", "ответь …". Write-команда всегда повторяет target и
   payload перед выполнением.
7. Добавить quiet/no-op policy: пустой digest не отправляет «ничего нового».

Канарейка: seeded new item даёт одну понятную summary в целевом topic; exact
search возвращает provenance; replay/restart не создаёт вторую summary.

### Фаза F — VK adapter

**Цель:** включить VK conversations без добавления непонятного token ownership.

Работы:

1. Выбрать один read seam: documented VK API с user-owned token **или**
   shared BrowserOS read adapter; зафиксировать representation.
2. Реализовать dialogs/unread/poll/read preview, stable conversation/message
   ids и cursor semantics.
3. Ограничить action surface до explicit read/download/send после отдельной
   capability review.
4. Прогнать read-only preflight и one-message dedupe canary.

Канарейка: одно новое VK message появляется в Core и search, но не порождает
неявную исходящую активность.

### Фаза G — WhatsApp adapter

**Цель:** не пропускать личные сообщения при сохранении user-owned pairing.

Работы:

1. Выбрать поддерживаемый runtime (официальный API при доступности либо
   отдельный user-owned linked-device adapter) и явно записать ограничения.
2. Pairing/QR остаётся ручным; Core не видит key material.
3. Реализовать только inbox poll, preview/read, stable chat/message ids и
   attachment descriptors.
4. Send/download добавлять отдельным vertical slice после read-only canary.
5. Обработать reconnect, revoked pairing и source status без silent re-pair.

Канарейка: новое WhatsApp message появляется в digest; expired/revoked pairing
виден в source status, но не провоцирует автоматический login.

### Фаза H — documents adapter

**Цель:** documents становятся поисковым source, но не притворяются chat.

Работы:

1. Выбрать providers в порядке ценности: Google Drive/Docs, local watched
   folders, Notion/other облака — каждый отдельным adapter id.
2. Normalise file id, path/url, title, modified time, owner, mime type,
   preview/extraction state и attachment/ref.
3. Poll incremental change token; не re-index whole drive на каждом цикле.
4. Полный текст/embeddings — optional later capability; сначала metadata и
   bounded textual preview.
5. Download/open всегда explicit action; deleted files становятся tombstones.

Канарейка: новый документ доступен в search с provenance; rename/edit/delete
корректно обновляет один identity без historical duplicate.

### Фаза I — production reliability и observability

**Цель:** Inbox можно оставить включённым без потерь и без скрытого шума.

Работы:

1. Per-source status: last successful poll, lag, cursor age, item delta,
   partial failure, auth-required state.
2. Structured secret-safe logs с request/correlation ids; payload/text/session
   никогда не логируются целиком.
3. Metrics: poll duration, items accepted/deduped/conflicted, search latency,
   action receipts, delivery attempts, summary suppressions.
4. Liveness/readiness: Core storage writable, registry valid, each source
   отдельно classified; all-source outage visible distinctly.
5. Bounded retry/backoff/jitter и circuit breaker per source.
6. SQLite lifecycle: migrations/version table, backup/export policy selected
   отдельно, integrity check before upgrades.
7. Retention: preview/body/attachment metadata time limits и tombstone policy
   утверждаются отдельно до включения новых personal sources.

Канарейка: controlled adapter timeout не блокирует Gmail search; source status
показывает degraded lane, а recovery advances cursor once.

### Фаза J — migration, parallel run и cutover

**Цель:** заменить legacy Hermes poll без потери входящих и без двойных summary.

Работы:

1. Зафиксировать current owner, transport и canary delta до любой mutation.
2. Создать source/account inventory и migration mapping старых cursor/
   fingerprint в Core identities.
3. Запустить Core в shadow mode: ingest/search только, delivery выключена.
4. Сверить окна новых Gmail/Telegram items: accepted, deduped, missing,
   false-positive. Исправить mapping до delivery.
5. Включить single-topic canary delivery только для Core; legacy delivery для
   этого topic не дублирует сообщения.
6. Подтвердить restart/replay/drop-recovery и explicit action receipts.
7. Только после подтверждённой замены запросить отдельный approval на
   отключение legacy email poll.
8. Выполнить финальный Hermes restart только после отдельного approval;
   проверить post-restart business canary, а не только service health.

Канарейка cutover: одно реальное новое Gmail message проходит source → Core →
agent summary → текущий topic ровно один раз; legacy poll не даёт второй copy.

## 6. Приоритетная очередность (YAGNI 80/20)

1. Фаза A: настоящий Core MCP transport.
2. Фаза B: adapter registry/poll/status.
3. Фаза C: Gmail adapter и shadow comparison.
4. Фаза E: Hermes topic summary/search consumer.
5. Фаза J: controlled Gmail cutover, только после real E2E proof.
6. D3 Telegram MCP/MTProto, VK, WhatsApp и documents — по отдельным user-owned auth и
   value gates, в этом порядке: Telegram MTProto, WhatsApp, VK, documents.
8. Фаза I hardening идёт перед каждым новым production source и перед final
   cutover, но не блокирует локальные contract slices.

## 7. Матрица capabilities

| Source | Poll / search | Read detail | Send | Download | Current decision |
| --- | --- | --- | --- | --- | --- |
| Gmail | да | да | later explicit | explicit | production evidence exists, Core adapter absent |
| Telegram Web/BrowserOS | removed | removed | removed | removed | explicitly excluded from Core |
| Telegram MCP/MTProto | да | да | explicit + evidence | explicit | needs separate legitimate daemon/login |
| VK | planned | planned | later explicit | later explicit | auth seam unselected |
| WhatsApp | planned | planned | later explicit | later explicit | user-owned pairing unselected |
| Documents | planned | metadata/preview | n/a | explicit | providers unselected |

## 8. Тестовая стратегия

### Contract tests

- identity normalisation, ref ownership, cursor validity, content states;
- accepted/rejected receipt invariants;
- adapter capability/action compatibility.

### Store tests

- exact replay, replay с новым cursor, collision, reopen/restart;
- result limits, tombstones, multi-source search and ordering;
- receipt idempotency и conflict.

### Adapter contract tests

- malformed item/batch fails before cursor movement;
- partial source failure does not poison other adapters;
- fake provider retry/restart/single-writer locking;
- every exposed action maps to one allowlisted adapter action.

### MCP conformance tests

- initialize, tools/list exact-set, input schema and unknown tool failure;
- bounded response sizes and no provider credential fields;
- stdio reconnect and process restart.

### Product canaries

- per-source one-new-item path;
- one-source failure while another still search/digest works;
- one summary in the actual configured Hermes topic;
- one explicitly requested write with target/payload/receipt;
- migration shadow diff and post-cutover restart/replay.

## 9. Decision and approval gates

| Gate | Required before |
| --- | --- |
| Transport choice | opening Core beyond in-process facade |
| Source auth owner | enabling any adapter against real personal data |
| MTProto runtime choice | QR login, daemon install or session path creation |
| Source write policy | send/reply/download or mark-read behavior |
| External deployment | new systemd/container service, public/private ingress or restart |
| Data retention policy | storing long-lived bodies, file text or embeddings |
| Cutover approval | disabling existing polling or changing live delivery ownership |
| Final restart approval | restart Hermes or an existing production service |

## 10. Definition of done

Universal Inbox считается production-ready только когда одновременно доказано:

1. Не менее Gmail и одного Telegram representation работают через Core,
   имеют source status, cursor/dedupe/restart canary и unified search.
2. Hermes consumer выдаёт agent-authored краткие сводки и exact search в
   текущем Telegram topic без duplicate delivery.
3. Каждое write/download действие явно подтверждаемо, allowlisted и имеет
   durable receipt.
4. Перезапуск Core и consumer не приводит к потере cursor, повторному item или
   неявной исходящей операции.
5. Shadow/cutover сравнение подтверждает замену legacy Gmail poll; только
   после этого он отключён отдельным разрешённым действием.
6. Post-cutover real-message canary и final Hermes restart canary зелёные.

До этого статуса отдельные готовые slices честно называются готовыми slices,
а не production Universal Inbox.
