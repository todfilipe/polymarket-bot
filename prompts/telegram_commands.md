# Prompt — Implementar comandos interativos no Telegram Bot

## Contexto

O bot já envia notificações via `TelegramNotifier` (HTTP direto para Bot API),
mas não recebe comandos. O `CLAUDE.md §12` menciona comandos bidirecionais como
intenção, mas nunca foram implementados.

Quero receber comandos no Telegram e obter respostas com dados reais da DB.

---

## O que implementar

### 1. Novo ficheiro: `src/polymarket_bot/notifications/telegram_commander.py`

Uma classe `TelegramCommander` que:

- Faz **long polling** à Bot API (`getUpdates` com `timeout=30`) num loop asyncio
  separado — nunca bloqueia o monitor principal
- Usa a mesma `aiohttp.ClientSession` já existente (passada no construtor)
- Usa o mesmo `async_sessionmaker` da SQLite já existente
- Responde via `sendMessage` ao `chat_id` do update (não ao chat_id fixo —
  assim qualquer mensagem enviada no grupo/chat correto funciona)
- Ignora updates de outros chats que não o `allowed_chat_id` configurado em `.env`
- Em caso de erro de rede, espera 10s e continua — nunca crasha o processo

### Comandos a implementar

| Comando | O que mostra |
|---|---|
| `/status` | Modo (PAPER/LIVE), estado do circuit breaker, nº wallets seguidas, uptime desde arranque |
| `/saldo` | Capital atual estimado = $1000 + P&L realizado de `Position.realized_pnl_usd`; P&L semanal (posições fechadas nesta semana); P&L total |
| `/posicoes` | Posições abertas (`Position.status = OPEN`): mercado, outcome, tamanho, preço de entrada, P&L não realizado estimado (preço atual via `MarketBuilder` ou omitir se indisponível). Máx 10 linhas |
| `/historico` | Últimas 15 `BotTrade` com `status != SKIPPED`: data, mercado (truncado a 40 chars), outcome, tamanho, status (FILLED/PENDING/FAILED) |
| `/skips` | Últimas 10 trades ignoradas (`BotTrade.status = SKIPPED`): motivo + mercado |
| `/wallets` | Top 7 wallets seguidas (`Wallet.is_followed = True`) com score e tier do `WalletScore` mais recente |
| `/semana` | Resumo da semana atual: trades ganhas/perdidas/ignoradas, P&L realizado, melhor e pior trade |
| `/pause` | Insere `CircuitBreakerState(status=PAUSED, reason="manual /pause")` — o monitor para de fazer trades |
| `/resume` | Insere `CircuitBreakerState(status=NORMAL, reason="manual /resume")` |
| `/halt` | Insere `CircuitBreakerState(status=HALTED, reason="manual /halt")` — para completamente |
| `/ajuda` | Lista todos os comandos com descrição de 1 linha |

### 2. Integração em `src/polymarket_bot/main.py`

- Instanciar `TelegramCommander` após o `notifier`
- Arrancá-lo como task asyncio: `asyncio.create_task(commander.run_forever())`
  dentro do bloco `try:` do `main()`, antes do `await monitor.run_forever()`
- Cancelar a task no `finally:` do shutdown

---

## Arquitetura do `TelegramCommander`

```python
class TelegramCommander:
    def __init__(
        self,
        bot_token: str,
        allowed_chat_id: str,
        session: aiohttp.ClientSession,
        session_factory: async_sessionmaker,
        monitor: WalletMonitor,          # para n_followed e stop/pause
        circuit_breaker: CircuitBreaker,
        started_at: datetime,            # para calcular uptime
        live_mode: bool,
    ): ...

    async def run_forever(self) -> None:
        """Loop de long polling. Nunca lança excepção."""

    async def _handle_update(self, update: dict) -> None:
        """Despacha para o handler correto baseado no texto do comando."""

    # handlers individuais:
    async def _cmd_status(self, chat_id: str) -> None: ...
    async def _cmd_saldo(self, chat_id: str) -> None: ...
    async def _cmd_posicoes(self, chat_id: str) -> None: ...
    async def _cmd_historico(self, chat_id: str) -> None: ...
    async def _cmd_skips(self, chat_id: str) -> None: ...
    async def _cmd_wallets(self, chat_id: str) -> None: ...
    async def _cmd_semana(self, chat_id: str) -> None: ...
    async def _cmd_pause(self, chat_id: str) -> None: ...
    async def _cmd_resume(self, chat_id: str) -> None: ...
    async def _cmd_halt(self, chat_id: str) -> None: ...
    async def _cmd_ajuda(self, chat_id: str) -> None: ...

    async def _reply(self, chat_id: str, text: str) -> None:
        """Envia mensagem — mesmo retry logic do TelegramNotifier."""
```

---

## Formato das respostas (exemplos)

### `/status`
```
🤖 Bot Polymarket
Modo: PAPER | Estado: NORMAL
Wallets seguidas: 7
Uptime: 3h 42m
```

### `/saldo`
```
💰 Saldo estimado
Capital: $1.024,30
P&L total: +$24,30 (+2,4%)
P&L esta semana: +$12,10 (+1,2%)
```

### `/posicoes`
```
📂 Posições abertas (3)
• Trump vence eleição — YES @ 0.62 | $80 | aberta há 2d
• BTC > 100k em março — YES @ 0.31 | $50 | aberta há 5h
• Fed corta taxa em maio — NO @ 0.44 | $60 | aberta há 1d
```

### `/historico`
```
📋 Últimas trades
✅ Trump vence eleição YES $80 FILLED (há 2d)
✅ Fed corta taxa NO $60 FILLED (há 1d)
⏳ BTC > 100k YES $50 PENDING (há 5h)
❌ Oscars Best Pic NO $40 FAILED (há 3d)
```

### `/wallets`
```
👛 Wallets seguidas (7)
🥇 0xabc...123 | score 87.4 | TOP
🥇 0xdef...456 | score 82.1 | TOP
🥇 0x789...abc | score 79.3 | TOP
🔹 0x321...fed | score 71.0 | BOT
🔹 0xaaa...bbb | score 68.5 | BOT
🔹 0xccc...ddd | score 65.2 | BOT
🔹 0xeee...fff | score 61.8 | BOT
```

---

## Detalhes técnicos importantes

- **Long polling**: `GET /getUpdates?offset={last_update_id+1}&timeout=30`
  Guarda `last_update_id` em memória (não precisas de DB para isto)
- **Segurança**: Ignora silenciosamente qualquer update de `chat_id` diferente
  do `allowed_chat_id` — não responde nem loga
- **Comandos com @botname**: `"/status@MyBot"` deve funcionar igual a `"/status"`
  — faz strip do sufixo `@...` antes de despachar
- **Erros de DB**: Se a query falhar, responde `"⚠️ Erro interno — tenta de novo"`
  e loga o erro, não crasha
- **Formatação**: Usa `parse_mode=HTML` como o notifier existente

## Não fazer

- Não usar `python-telegram-bot` nem outras libs — HTTP direto com `aiohttp`
  como já está no `TelegramNotifier`
- Não criar ficheiro `.env` novo — reutiliza `TELEGRAM_BOT_TOKEN` e
  `TELEGRAM_CHAT_ID` já existentes em `Settings`; o `allowed_chat_id` é o
  mesmo `telegram_chat_id`
- Não bloquear o event loop — toda a I/O é `async`

## Testes

Cria `tests/unit/test_telegram_commander.py` com testes para:
- `_handle_update` despacha para o handler correto
- Updates de chat_id errado são ignorados
- Comandos com sufixo `@botname` são reconhecidos
- `/ajuda` lista todos os comandos
