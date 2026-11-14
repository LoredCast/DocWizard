"""ID token verification (§13.4) with joserfc against the provider's JWKS.

Only asymmetric algorithms are accepted (RS256, PS256, ES256, EdDSA): never
``none``, never HMAC. The claims checked are iss, aud, azp, exp, iat (with a
small clock skew) and nonce.
"""

from __future__ import annotations

import time

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

ALGORITHMS = ["RS256", "PS256", "ES256", "EdDSA"]
SKEW = 120


class TokenError(Exception):
    pass


class UnknownKey(TokenError):
    """The token names a key the JWKS doesn't have: refresh the JWKS and retry."""


def verify(token: str, jwks: dict, *, issuer: str, client_id: str, nonce: str, now: float | None = None) -> dict:
    try:
        keys = KeySet.import_key_set(jwks)
    except (JoseError, ValueError, TypeError, KeyError) as exc:
        raise TokenError(f"The provider's keys can't be read: {exc}") from None
    try:
        decoded = jwt.decode(token, keys, algorithms=ALGORITHMS)
    except JoseError as exc:
        if exc.error in ("invalid_key_id", "missing_key"):
            raise UnknownKey(str(exc)) from None
        raise TokenError(f"The ID token is not valid: {exc}") from None
    except (ValueError, TypeError) as exc:
        raise TokenError(f"The ID token is not valid: {exc}") from None
    claims = decoded.claims
    now = time.time() if now is None else now
    if claims.get("iss") != issuer:
        raise TokenError("The ID token comes from another issuer.")
    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if client_id not in audiences:
        raise TokenError("The ID token is for another client.")
    if len(audiences) > 1 and claims.get("azp") != client_id:
        raise TokenError("The ID token's authorized party is another client.")
    if "azp" in claims and claims["azp"] != client_id:
        raise TokenError("The ID token's authorized party is another client.")
    exp, iat = claims.get("exp"), claims.get("iat")
    if not isinstance(exp, (int, float)) or exp < now - SKEW:
        raise TokenError("The ID token expired.")
    if not isinstance(iat, (int, float)) or iat > now + SKEW:
        raise TokenError("The ID token was issued in the future.")
    if not nonce or claims.get("nonce") != nonce:
        raise TokenError("The ID token doesn't belong to this login.")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise TokenError("The ID token has no subject.")
    return claims
