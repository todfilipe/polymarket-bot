"""Parâmetros inegociáveis da lógica estratégica.

Fonte de verdade: Polymarket_Bot_Logica_Estrategica.docx + CLAUDE.md.
Não alterar sem atualizar ambos os documentos.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyConstants:
    # --- Capital e objetivos (§1, §7.1) ---
    TARGET_WEEKLY_RETURN_MIN: float = 0.01
    TARGET_WEEKLY_RETURN_MAX: float = 0.05
    WEEKLY_STOP_LOSS: float = -0.15
    CASH_RESERVE_RATIO: float = 0.30

    # --- Sizing ---
    BASE_SIZE_RATIO: float = 0.03   # base 3% da banca por trade (era 5%)
    MAX_SIZE_RATIO: float = 0.10    # cap absoluto por posição (era 8%)
    MIN_TRADE_USD: float = 1.0      # mínimo CLOB ~$1 (era $20)

    # Multiplicadores de sizing
    TIER_TOP_MULTIPLIER: float = 1.4
    TIER_BOTTOM_MULTIPLIER: float = 0.7
    CONSENSUS_MULTIPLIER: float = 1.3
    SOLO_WALLET_MULTIPLIER: float = 0.5
    RECOVERY_MULTIPLIER: float = 0.6
    ALL_WALLETS_MULTIPLIER: float = 1.5

    # --- Limites de portfolio (§7.1) ---
    MAX_RATIO_PER_TRADE: float = 0.10  # alinhado com MAX_SIZE_RATIO (era 8%)
    MAX_RATIO_PER_CATEGORY: float = 0.25
    MAX_RATIO_PER_EVENT: float = 0.15
    MAX_RATIO_TOP3_COMBINED: float = 0.50
    MAX_OPEN_POSITIONS: int = 30   # era 10; cash reserve já actua como backstop real

    # --- Wallets seguidas (§2.1) ---
    WALLETS_FOLLOWED: int = 7
    TOP_TIER_COUNT: int = 3
    BOTTOM_TIER_COUNT: int = 4
    TOP_TIER_WEIGHT: float = 0.60
    BOTTOM_TIER_WEIGHT: float = 0.40

    # --- Scoring — pesos das métricas (§2.2), soma = 1.0 ---
    SCORE_WEIGHT_PROFIT_FACTOR: float = 0.30
    SCORE_WEIGHT_CONSISTENCY: float = 0.20
    SCORE_WEIGHT_WIN_RATE: float = 0.20
    SCORE_WEIGHT_DRAWDOWN: float = 0.15
    SCORE_WEIGHT_DIVERSIFICATION: float = 0.10
    SCORE_WEIGHT_RECENCY: float = 0.05

    # --- Filtros obrigatórios de wallets (§2.3) ---
    MIN_TRADES_HISTORY: int = 50
    MIN_ACTIVITY_WEEKS: int = 2           # ativa nas últimas N semanas
    MAX_HISTORICAL_DRAWDOWN: float = 0.35
    MIN_VOLUME_USD: float = 500.0
    MIN_PROFIT_FACTOR: float = 1.5
    MIN_CATEGORIES: int = 2
    MIN_WIN_RATE: float = 0.55

    # --- Deteção de sorte vs skill (§2.4) ---
    MAX_LUCK_CONCENTRATION: float = 0.60  # >60% lucro em 1-2 trades = sorte

    # --- Filtros de mercado (§3.1) ---
    MIN_MARKET_VOLUME_USD: float = 50_000.0
    IDEAL_MARKET_VOLUME_USD: float = 100_000.0
    # Mínimo de horas até resolução. Sem mínimo (0h) por opção do utilizador
    # — segue qualquer trade das wallets, incluindo last-minute.
    MIN_HOURS_TO_RESOLUTION: int = 0
    MAX_DAYS_TO_RESOLUTION: int = 60
    MIN_IMPLIED_PROB: float = 0.10
    MAX_IMPLIED_PROB: float = 0.82
    IDEAL_PROB_MIN: float = 0.20
    IDEAL_PROB_MAX: float = 0.65
    IDEAL_DAYS_MIN: int = 7
    IDEAL_DAYS_MAX: int = 30
    MAX_ORDERBOOK_IMPACT: float = 0.02   # fill sem mover > 2%

    # --- EV mínimo (§3.4) ---
    MIN_EV_MARGIN: float = 0.10           # EV > 10% do stake

    # --- Slippage (§3.2) ---
    MAX_SLIPPAGE_HIGH_VOLUME: float = 0.05   # volume > $500k
    MAX_SLIPPAGE_MID_VOLUME: float = 0.02    # $50k – $500k
    HIGH_VOLUME_THRESHOLD: float = 500_000.0
    ORDER_EXECUTION_WINDOW_SECONDS: int = 60

    # --- Saídas ---
    # Hard stop loss removido: incoerente com pure copy. A wallet decide quando
    # sair (wallet exit); o cap por posição (MAX_SIZE_RATIO=10%) é o backstop
    # contra ruína da banca; o auto-settle trata da resolução. Em prediction
    # markets binários, eventos discretos (golos, news) movem o preço 30-50%
    # num minuto e revertem com igual rapidez — um SL fixo desfaz trades em
    # curso sem proteger de catástrofe verdadeira.
    # Take profit parcial também removido pela mesma razão (ir até ao fim).
    MIN_WALLET_TIMING_SKILL: float = 0.60

    # --- Adições (§5.1) ---
    MOMENTUM_ADD_FRACTION: float = 0.50
    # MAX_ENTRIES_PER_POSITION removido: o cap absoluto (MAX_SIZE_RATIO=10%)
    # serve de backstop; permitir N adds dentro do cap fica mais alinhado
    # com pure copy quando a wallet faz DCA/fatiamento.

    # --- Filtro de ruído (sizing-relativo) ---
    # Trades da wallet abaixo desta fracção da mediana das últimas
    # MEDIAN_WINDOW_WEEKS são considerados ruído (testes, erros, dust).
    # Mediana é resistente a outliers (vs média), e o threshold é relativo
    # à wallet — escala com whales e wallets humildes.
    NOISE_FILTER_RATIO: float = 0.20            # 20% × mediana
    NOISE_FILTER_MIN_HISTORY: int = 20          # mín. trades p/ aplicar filtro
    MEDIAN_WINDOW_WEEKS: int = 4                # janela da mediana

    # --- Circuit breakers (§7.2, §10.1) ---
    RECOVERY_WEEKS_AFTER_STOP: int = 2
    RECOVERY_SIZE_REDUCTION_1X: float = -0.30
    RECOVERY_SIZE_REDUCTION_2X: float = -0.50
    HALT_AFTER_NEGATIVE_WEEKS: int = 3

    # --- Resiliência API (§9.1) ---
    API_RETRY_BACKOFFS_SECONDS: tuple[int, ...] = (5, 30, 300)
    API_MAX_ERRORS_PER_HOUR: int = 3
    DEDUP_WINDOW_MINUTES: int = 5

    # --- Scoring window ---
    SCORING_WINDOW_WEEKS: int = 4
    RECENCY_WINDOW_WEEKS: int = 4


CONST = StrategyConstants()
"""Singleton de constantes — importar como `from polymarket_bot.config import CONST`."""
