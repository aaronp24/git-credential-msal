# SPDX-License-Identifier: MIT

# pip install msal
from msal import PublicClientApplication
from msal import SerializableTokenCache

# pip install pyjwt
import jwt

from datetime import datetime
from datetime import timedelta
from datetime import timezone

# pip install keyring
import keyring

import argparse
import os
import pickle
import re
import subprocess
import sys

# pip install pyxdg
import xdg.BaseDirectory

from importlib.metadata import version, PackageNotFoundError

cache_dir = os.path.join(xdg.BaseDirectory.xdg_cache_home, "git-credential-msal")

wwwauth_bearer_matcher = re.compile(r"^Bearer(?:\s*|\s+.+)")
wwwauth_client_id_matcher = re.compile(
    r""".*msal-client-id(?:="(.*?)"|=(.*?),|=(.*))"""
)
wwwauth_tenant_id_matcher = re.compile(
    r""".*msal-tenant-id(?:="(.*?)"|=(.*?),|=(.*))"""
)


def read_stdin_pairs() -> dict[str, str]:
    kvs = dict()
    for line in sys.stdin:
        [key, value] = line.rstrip().split(sep="=", maxsplit=1)

        # From the git-credential man page: If the key name ends with [], then
        # it's an array. An empty multi-valued attribute assignment clears any
        # previous assignments.
        if key.endswith("[]"):
            key = key[:-2]
            if not key in kvs or value == "":
                kvs[key] = []
            kvs[key].append(value)
        else:
            kvs[key] = value

    return kvs


def authtype_accepted(helper_pairs: dict[str, str]) -> bool:
    if not "capability" in helper_pairs:
        return False

    return "authtype" in helper_pairs["capability"]


def bearer_accepted(helper_pairs: dict[str, str]) -> bool:
    if not "wwwauth" in helper_pairs:
        return False

    for auth in helper_pairs["wwwauth"]:
        if wwwauth_bearer_matcher.match(auth) is not None:
            return True

    return False


def extract_entra_ids_from_wwwauth(helper_pairs: dict[str, str]) -> tuple[str, str]:
    protocol = helper_pairs["protocol"]
    host = helper_pairs["host"]
    url = f"{protocol}://{host}"
    msal_client_id = None
    msal_tenant_id = None

    for auth in helper_pairs["wwwauth"]:
        if wwwauth_bearer_matcher.match(auth) is not None:
            client_id_match = wwwauth_client_id_matcher.match(auth)
            tenant_id_match = wwwauth_tenant_id_matcher.match(auth)

            if client_id_match is None or tenant_id_match is None:
                continue

            for subgroup in client_id_match.groups():
                if subgroup is not None:
                    msal_client_id = subgroup
                    break

            for subgroup in tenant_id_match.groups():
                if subgroup is not None:
                    msal_tenant_id = subgroup
                    break

            if msal_client_id is not None and msal_tenant_id is not None:
                break
            else:
                msal_client_id = None
                msal_tenant_id = None

    return (msal_client_id, msal_tenant_id)


def git_config_get_urlmatch(url: str, key: str) -> str:
    result = subprocess.run(
        ["git", "config", "--get-urlmatch", key, url], capture_output=True
    )

    if result.returncode != 0:
        return None

    config_value = result.stdout.rstrip().decode()
    if config_value == "":
        config_value = None

    return config_value


def extract_entra_ids_from_git_config(helper_pairs: dict[str, str]) -> tuple[str, str]:
    protocol = helper_pairs["protocol"]
    host = helper_pairs["host"]
    url = f"{protocol}://{host}"

    msal_client_id = git_config_get_urlmatch(url, "credential.msalClientId")
    msal_tenant_id = git_config_get_urlmatch(url, "credential.msalTenantId")

    return (msal_client_id, msal_tenant_id)


def get_msal_cache_insecure(name: str) -> str:
    msal_cache_name = f"msal_cache_{name}"
    msal_cache_path = os.path.join(cache_dir, msal_cache_name)
    try:
        with open(msal_cache_path, "r") as f:
            return f.read()
    except:
        return None


def get_msal_cache(name: str) -> SerializableTokenCache:
    cache = SerializableTokenCache()
    data = None
    try:
        data = keyring.get_password("system", name)
    except keyring.errors.NoKeyringError:
        pass
    if not data:
        data = get_msal_cache_insecure(name)

    if data:
        cache.deserialize(data)

    return cache


def put_msal_cache_insecure(name: str, data: str, allow_insecure: bool):
    if not allow_insecure:
        return

    os.makedirs(cache_dir, exist_ok=True)
    msal_cache_name = f"msal_cache_{name}"
    msal_cache_path = os.path.join(cache_dir, msal_cache_name)
    with open(msal_cache_path, "w") as f:
        f.write(data)


def put_msal_cache(name: str, cache: SerializableTokenCache, allow_insecure: bool):
    if cache.has_state_changed:
        data = cache.serialize()
        try:
            keyring.set_password("system", name, data)
        except keyring.errors.NoKeyringError:
            put_msal_cache_insecure(name, data, allow_insecure)


def get_http_cache(name: str) -> dict:
    http_cache_name = f"http_cache_{name}"
    http_cache_path = os.path.join(cache_dir, http_cache_name)
    try:
        with open(http_cache_path, "rb") as f:
            http_cache = pickle.load(f)
    except:
        http_cache = {}
    return http_cache


def put_http_cache(name: str, http_cache: dict):
    http_cache_name = f"http_cache_{name}"
    os.makedirs(cache_dir, exist_ok=True)
    http_cache_path = os.path.join(cache_dir, http_cache_name)
    pickle.dump(http_cache, open(http_cache_path, "wb"))


def jwt_expired_value(token: str) -> int:
    # Let the server verify the JWT signature
    # Only checking exp for caching purposes
    token_decoded = jwt.decode(token, options={"verify_signature": False})

    token_exp = token_decoded["exp"]
    # Convert JWT exp NumericDate attribute to Python datetime object
    token_exp_datetime = datetime.fromisoformat("1970-01-01T00:00:00Z") + timedelta(
        seconds=token_exp
    )

    return int(token_exp_datetime.timestamp())


def msal_acquire_oidc_id_token(
    client_id: str,
    tenant_id: str,
    device_code: bool = False,
    allow_insecure: bool = False,
) -> str:
    scopes = ["email openid User.Read"]
    id_token = None
    cache_name = f"{tenant_id}_{client_id}"
    keyring_name = f"git-credential-msal_{cache_name}"
    cache = get_msal_cache(keyring_name)
    http_cache = get_http_cache(cache_name)

    app = PublicClientApplication(
        client_id=client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache,
        http_cache=http_cache,
    )
    acquire_tokens_result = None

    accounts = app.get_accounts()
    if len(accounts) > 0:
        account = accounts[0]
        acquire_tokens_result = app.acquire_token_silent(scopes=scopes, account=account)

    if acquire_tokens_result is None:
        if device_code:
            device_code_flow = app.initiate_device_flow(scopes=scopes)
            print(device_code_flow["message"], file=sys.stderr)
            acquire_tokens_result = app.acquire_token_by_device_flow(
                device_code_flow,
            )
        else:
            acquire_tokens_result = app.acquire_token_interactive(
                scopes=scopes,
                success_template="""<html><body><script>setTimeout(function(){window.close()}, 500);</script></body></html>""",
            )

    if "error" in acquire_tokens_result:
        print("Error: " + acquire_tokens_result["error"], file=sys.stderr)
        print(
            "Description: " + acquire_tokens_result["error_description"],
            file=sys.stderr,
        )
    else:
        accounts = app.get_accounts()
        account = accounts[0]

        # OIDC Id token
        id_token = cache.find(
            cache.CredentialType.ID_TOKEN,
            query={
                "home_account_id": account["home_account_id"],
            },
        )[0]["secret"]

    put_msal_cache(keyring_name, cache, allow_insecure)
    put_http_cache(keyring_name, http_cache)
    return id_token


def main():
    parser = argparse.ArgumentParser(
        prog="git-credential-msal",
        description="git-credential-helper for Microsoft SSO auth flows using MSAL",
    )
    parser.add_argument("command")
    parser.add_argument("-d", "--device-code", action="store_true")
    parser.add_argument("-i", "--insecure", action="store_true")
    try:
        parser.add_argument(
            "-v", "--version", action="version", version=version("git_credential_msal")
        )
    except PackageNotFoundError:
        # package is not installed
        pass
    args = parser.parse_args()

    # The credential helper can only provide credentials
    # It cannot consume credentials from users to store
    if args.command != "get":
        exit(0)

    helper_pairs = read_stdin_pairs()

    # Make sure the git implementation supports the `authtype` token.
    if not authtype_accepted(helper_pairs):
        exit(0)

    # Make sure the server specified that a Bearer token is acceptable.
    if not bearer_accepted(helper_pairs):
        exit(0)

    client_id, tenant_id = extract_entra_ids_from_git_config(helper_pairs)

    # Check if the server provide MSAL client id and tenant id if the user does not
    # have this information stored through git config.
    if client_id is None or tenant_id is None:
        client_id, tenant_id = extract_entra_ids_from_wwwauth(helper_pairs)

    if client_id is None:
        print(
            "Missing Microsoft Entra client id needed by git-credential-msal",
            file=sys.stderr,
        )
    if tenant_id is None:
        print(
            "Missing Microsoft Entra tenant id needed by git-credential-msal",
            file=sys.stderr,
        )
    if client_id is None or tenant_id is None:
        exit(0)

    # Chrome prints "Opening in existing browser session" to stdout, confusing
    # git-credential. Work around this by making stdout non-inheritable.
    os.set_inheritable(1, False)

    id_token = msal_acquire_oidc_id_token(
        client_id, tenant_id, device_code=args.device_code, allow_insecure=args.insecure
    )
    expiry = jwt_expired_value(id_token)

    print("capability[]=authtype")
    print("authtype=Bearer")
    print(f"credential={id_token}")
    print(f"password_expiry_utc={expiry}")


if __name__ == "__main__":
    main()
