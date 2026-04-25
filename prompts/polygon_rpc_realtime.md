# Prompt — Deteção em tempo real via Polygon RPC

## Contexto e problema

O `SignalReader` atual faz polling à Polymarket Data API (`/trades?user=<address>`)
a cada 30 segundos. Descobrimos que esta API tem **mais de 1 hora de lag** na
indexação de novas trades — tornando o copytrade ineficaz.

O `CLAUDE.md §13` já prevê: "Polygon RPC como fallback". Precisamos substituir
(ou complementar) o polling da Data API por **deteção em tempo real via eventos
on-chain no Polygon**.

## Arquitetura solução

### Como a Polymarket funciona on-chain

Cada trade na Polymarket emite um evento `OrderFilled` no contrato CTF Exchange:
- Contrato Polymarket CTF Exchange (Polygon): `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`
- Evento: `OrderFilled(bytes32 orderHash, address indexed maker, address indexed taker, ...)`
- As posições são tokens ERC-1155 no contrato CTF: `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`

Cada proxy wallet das wallets seguidas aparece como `maker` ou `taker` nestes eventos.

### Abordagem: `eth_getLogs` polling + WebSocket

Usar a lib `web3.py` (já deve estar no projeto ou adicionar ao pyproject.toml)
para:
1. **Subscrever via WebSocket** a `eth_subscribe("logs", filter)` para deteção
   imediata de novos blocos com eventos das wallets seguidas
2. **Fallback**: se WebSocket falhar, fazer polling de `eth_getLogs` a cada 15s
   (mais rápido que os 30s atuais da Data API)

### Polygon RPC endpoints gratuitos

- `https://polygon-rpc.com` (HTTP)
- `wss://polygon-bor-rpc.publicnode.com` (WebSocket)
- Configurar via `.env`: `POLYGON_RPC_URL` e `POLYGON_WS_URL`

## O que implementar

### 1. Novo ficheiro: `src/polymarket_bot/monitoring/chain_watcher.py`

```python
class ChainWatcher:
    """Deteta trades on-chain em tempo real via Polygon RPC.
    
    Substitui o polling da Data API como fonte primária de sinais.
    Ao detetar um OrderFilled com maker/taker numa wallet seguida:
    1. Extrai conditionId (market_id) e tokenId do evento
    2. Determina side (BUY/SELL) e outcome (YES/NO) pelo tokenId
    3. Emite DetectedSignal para o WalletMonitor processar
    
    Usa WebSocket quando disponível; cai para eth_getLogs se falhar.
    """
    
    def __init__(
        self,
        rpc_url: str,
        ws_url: str | None,
        followed_addresses: set[str],      # proxy wallets em lowercase
        signal_callback: Callable[[DetectedSignal], Awaitable[None]],
        data_client: PolymarketDataClient, # para enriquecer com metadata do mercado
    ): ...
    
    async def run_forever(self) -> None: ...
    
    def update_followed_addresses(self, addresses: set[str]) -> None:
        """Chamado após rebalanceamento de domingo."""
```

### 2. Modificar `SignalReader`

- Adicionar modo `source: Literal["api", "chain", "both"]`  
- Em modo `"chain"`, o `ChainWatcher` injeta sinais diretamente via callback
  em vez do polling à Data API
- Em modo `"both"`, usa chain como primário e API como fallback/verificação
- **Default: `"chain"`** após esta implementação

### 3. Modificar `WalletMonitor`

- Aceitar `ChainWatcher` opcional no construtor
- Se presente, arranca `asyncio.create_task(chain_watcher.run_forever())`
- O ChainWatcher chama `_process_group()` diretamente via callback quando
  deteta um sinal

### 4. Modificar `main.py`

- Ler `POLYGON_RPC_URL` e `POLYGON_WS_URL` do `.env` (já existe
  `POLYGON_RPC_URL` nas settings para o CLOB client)
- Instanciar `ChainWatcher` e passar ao `WalletMonitor`

### 5. Adicionar ao `.env.example`

```
POLYGON_RPC_URL=https://polygon-rpc.com
POLYGON_WS_URL=wss://polygon-bor-rpc.publicnode.com
```

## Detalhes técnicos do evento OrderFilled

```
event OrderFilled(
    bytes32 indexed orderHash,
    address indexed maker,
    address indexed taker,
    uint256 makerAssetId,    # tokenId do outcome (0 = YES, 1 = NO geralmente)
    uint256 takerAssetId,
    uint256 makerAmountFilled,
    uint256 takerAmountFilled,
    uint256 fee
)
```

- `conditionId` (market_id) extrai-se do `makerAssetId` via chamada ao contrato
  CTF: `ctf.getConditionId(makerAssetId)`
- Alternativamente, fazer GET à Polymarket CLOB API:
  `GET /markets?clob_token_ids=<tokenId>` para obter o market_id e question
- `side`: se `maker` é a wallet seguida → é um SELL (maker vende); se `taker`
  é a wallet → é um BUY (taker compra). Confirmar com a documentação da CLOB.

## Robustez

- WebSocket desconecta → reconectar com backoff exponencial (1s, 2s, 4s, máx 60s)
- RPC rate limit → rodar entre múltiplos endpoints públicos
- Evento não parseable → log warning + skip, nunca crasha
- Após restart: processar eventos dos últimos 5 minutos para não perder trades
  ocorridas durante o downtime (usando `eth_getLogs` com `fromBlock`)

## Testes

Criar `tests/unit/test_chain_watcher.py` com:
- Parse correto de um evento `OrderFilled` mockado
- Filtragem correta por endereços seguidos
- Reconexão WebSocket após falha

## Dependências a adicionar ao `pyproject.toml`

```
web3>=7.0.0
websockets>=12.0
```

## Prioridade de implementação

1. `eth_getLogs` polling (15s) como substituto imediato — elimina o lag de 1h
2. WebSocket subscription — para deteção sub-segundo
3. Fallback automático entre os dois modos
