"""Setup de credenciais CLOB — correr uma vez.

Uso:
    python scripts/setup_credentials.py

Lê a `POLYGON_PRIVATE_KEY` do `.env`, chama a Polymarket CLOB para gerar
(ou derivar, se já existirem) as credenciais L2 `api_key` / `api_secret` /
`api_passphrase`, e imprime as linhas exatas para o utilizador copiar para o
`.env`. Não escreve automaticamente no `.env` — o utilizador controla a
substituição.

A operação é idempotente: correr várias vezes devolve sempre as mesmas creds.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Permite correr standalone sem instalar o package
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from polymarket_bot.api import generate_or_derive_credentials  # noqa: E402
from polymarket_bot.config import get_settings  # noqa: E402


DIVIDER = "=" * 64


def main() -> int:
    settings = get_settings()

    print(DIVIDER)
    print("  Polymarket CLOB — setup de credenciais")
    print(DIVIDER)
    print(f"  Chain ID : {settings.polygon_chain_id} ({'Polygon' if settings.polygon_chain_id == 137 else 'Amoy testnet'})")
    print(f"  Host     : {settings.clob_api_url}")
    print(f"  Modo     : {'DRY-RUN' if settings.is_dry_run else 'LIVE'}")
    print(DIVIDER)
    print("A contactar a Polymarket para criar/derivar credenciais...")
    print()

    try:
        result = generate_or_derive_credentials(settings)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERRO] Falhou a gerar credenciais: {exc}", file=sys.stderr)
        print(
            "\nCausas comuns:"
            "\n  - Private key inválida no .env"
            "\n  - Chain ID incorreto (137 para mainnet, 80002 para Amoy)"
            "\n  - Sem ligação à internet / Polymarket offline",
            file=sys.stderr,
        )
        return 1

    print("Credenciais obtidas com sucesso.")
    print()
    print(DIVIDER)
    print("  Endereço EOA (do teu ficheiro .env)")
    print(DIVIDER)
    print(f"  {result.eoa_address}")
    print()
    print("Este é o endereço que vai assinar as ordens e receber USDC.")
    print("Envia USDC (Polygon) para este endereço para depositares capital.")
    print()
    print(DIVIDER)
    print("  COPIA ESTAS LINHAS PARA O TEU .env")
    print(DIVIDER)
    print()
    print(f"CLOB_API_KEY={result.api_key}")
    print(f"CLOB_API_SECRET={result.api_secret}")
    print(f"CLOB_API_PASSPHRASE={result.api_passphrase}")
    print(f"PROXY_WALLET_ADDRESS={result.funder}")
    print()
    print(DIVIDER)
    print("  Próximos passos")
    print(DIVIDER)
    print("  1. Copia as 4 linhas acima para o .env (substitui as vazias).")
    print("  2. Envia USDC (Polygon) para o endereço EOA.")
    print("  3. Mantém LIVE_MODE=false — ainda estamos em paper trading.")
    print("  4. Guarda a private key em local seguro — não partilhar.")
    print(DIVIDER)
    return 0


if __name__ == "__main__":
    sys.exit(main())
