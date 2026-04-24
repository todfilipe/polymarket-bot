"""Wrapper sobre `py-clob-client` com as nossas convenções.

Responsabilidades:
- Derivar EOA a partir da private key.
- Gerar/derivar credenciais L2 (key, secret, passphrase) — acção idempotente.
- Fornecer um `ClobClient` configurado à aplicação.

Modos de signing (§ `signature_type` do py-clob-client):
- EOA (0)             — EOA assina e é também o funder. Simples; requer USDC na EOA.
- POLY_PROXY (1)      — proxy wallet antigo. Deprecated para novos users.
- POLY_GNOSIS_SAFE (2)— proxy Gnosis Safe. Recomendado; deployed pelo Polymarket UI
                        ao primeiro depósito de USDC.

Começamos em EOA mode (mais direto). Trocamos para POLY_GNOSIS_SAFE quando o
Safe estiver deployed (após depósito via UI).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

from polymarket_bot.config import Settings


class SignatureType(IntEnum):
    EOA = 0
    POLY_PROXY = 1
    POLY_GNOSIS_SAFE = 2


@dataclass(frozen=True)
class CredentialsSetupResult:
    eoa_address: str
    api_key: str
    api_secret: str
    api_passphrase: str
    chain_id: int
    clob_host: str
    signature_type: SignatureType
    funder: str


def derive_eoa_address(private_key: str) -> str:
    """Calcula o endereço EOA (0x...) a partir da private key."""
    pk = private_key if private_key.startswith("0x") else f"0x{private_key}"
    return Account.from_key(pk).address


def build_clob_client(
    settings: Settings,
    api_creds: ApiCreds | None = None,
    signature_type: SignatureType = SignatureType.EOA,
    funder: str | None = None,
) -> ClobClient:
    """Devolve um `ClobClient` pronto a autenticar pedidos.

    Se `api_creds` for `None`, o cliente só suporta operações L1 (precisa de ser
    autenticado via `create_or_derive_api_creds` antes de operações L2).
    """
    pk = settings.polygon_private_key.get_secret_value()
    pk = pk if pk.startswith("0x") else f"0x{pk}"
    resolved_funder = funder or (
        settings.proxy_wallet_address or derive_eoa_address(pk)
    )
    return ClobClient(
        host=settings.clob_api_url,
        chain_id=settings.polygon_chain_id,
        key=pk,
        creds=api_creds,
        signature_type=int(signature_type),
        funder=resolved_funder,
    )


def generate_or_derive_credentials(settings: Settings) -> CredentialsSetupResult:
    """Gera ou deriva as credenciais L2 a partir da private key.

    A operação é idempotente: se já existirem credenciais para esta wallet,
    `create_or_derive_api_creds` devolve-as sem as regenerar.
    """
    eoa = derive_eoa_address(settings.polygon_private_key.get_secret_value())
    client = build_clob_client(settings, api_creds=None, signature_type=SignatureType.EOA)
    creds = client.create_or_derive_api_creds()
    return CredentialsSetupResult(
        eoa_address=eoa,
        api_key=creds.api_key,
        api_secret=creds.api_secret,
        api_passphrase=creds.api_passphrase,
        chain_id=settings.polygon_chain_id,
        clob_host=settings.clob_api_url,
        signature_type=SignatureType.EOA,
        funder=eoa,
    )


def load_api_creds(settings: Settings) -> ApiCreds | None:
    """Constrói `ApiCreds` a partir das settings, se todas as três existirem."""
    if not settings.has_clob_credentials:
        return None
    return ApiCreds(
        api_key=settings.clob_api_key.get_secret_value(),      # type: ignore[union-attr]
        api_secret=settings.clob_api_secret.get_secret_value(),  # type: ignore[union-attr]
        api_passphrase=settings.clob_api_passphrase.get_secret_value(),  # type: ignore[union-attr]
    )
