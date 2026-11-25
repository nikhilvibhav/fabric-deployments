"""Per-environment public-key encrypted secrets stored alongside configuration.

Each value is sealed separately with the environment's committed public key, so
contributors can add or rotate values without being able to read existing ones.
Only the holder of the environment's private key can decrypt them.
"""

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from nacl.bindings import crypto_box_SEALBYTES
from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox

from .config import DEFAULTS, ROOT, SEGMENT, relative_path, template_names

# Version 2 added key_id, which records the key that sealed a store's values.
FORMAT_VERSION = 2
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
KEY_ENV = "CAMEL_SECRETS_KEY_FILE"
# Property keys whose literal values should be encrypted secrets instead.
SENSITIVE_PROPERTY = re.compile(
    r"(password|passwd|pwd|secret|token|api[-_.]?key)$", re.IGNORECASE
)
# Values rendered into service definitions/process arguments, or that select paths.
RESERVED = {
    "stage",
    "application",
    "version",
    "application_name",
    "application_execute",
    "deploy_to",
    "release_path",
    "java_bin",
    "jvm_options",
    "camel_log_dir",
}


def secrets_dir(stage: str, root: Path = ROOT) -> Path:
    """Return the validated directory holding secrets for a stage."""
    if not SEGMENT.fullmatch(stage) or stage in {".", ".."}:
        raise ValueError("Invalid stage")
    return root / "secrets" / stage


def public_key_path(stage: str, root: Path = ROOT) -> Path:
    """Return the path to a stage's committed public key."""
    return secrets_dir(stage, root) / "public.key"


def secrets_path(stage: str, application: str, root: Path = ROOT) -> Path:
    """Return the validated encrypted store path for an application."""
    relative_path(application, "application")
    return (
        secrets_dir(stage, root).joinpath(*application.split("/")).with_suffix(".json")
    )


def key_id(public_key: PublicKey) -> str:
    """Return a short fingerprint identifying which key encrypted a store."""
    return hashlib.sha256(bytes(public_key)).hexdigest()[:16]


def stale_store_error(stage: str, application: str) -> ValueError:
    """Explain a store whose key_id does not match the stage's public key."""
    return ValueError(
        f"secrets/{stage}/{application}.json was encrypted with a different key "
        f"than secrets/{stage}/public.key. Merge the latest rekey into your branch, "
        f"or, if a rekey was interruped, restore with: git checkout -- secrets/{stage}"
    )


def encode(data: bytes | PublicKey | PrivateKey) -> str:
    """Encode bytes or key material as base64 text."""
    return base64.b64encode(bytes(data)).decode()


def decode(text: str, label: str) -> bytes:
    """Decode strict base64 text and identify malformed input by label."""
    try:
        return base64.b64decode(text.strip(), validate=True)
    except ValueError:
        raise ValueError(f"Invalid base64 in {label}") from None


def write_atomic(path: Path, text: str) -> None:
    """Write text through a sibling temporary file and atomically replace the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(handle, "w", newline="\n") as output:
            output.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_private_key(path: str | Path, key: PrivateKey) -> None:
    """Create a private key file without overwriting existing files."""
    path = Path(path)
    # O_EXCL never overwrites an existing key; 0600 limits access on POSIX.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(encode(key) + "\n")


def load_public_key(stage: str, root: Path = ROOT) -> PublicKey:
    """Load and validate the 32-byte public key for a stage."""
    path = public_key_path(stage, root)
    if not path.is_file():
        raise ValueError(f"No public key for stage {stage}; run 'secrets keygen' first")
    data = decode(path.read_text(), path.name)
    if len(data) != 32:
        raise ValueError("Public key must be 32 bytes")
    return PublicKey(data)


def load_private_key(path: str | Path, stage: str, root: Path = ROOT) -> PrivateKey:
    """Load a protected private key and verify that it matches the stage key."""
    path = Path(path)
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError(
            "Private key file must not be readable by group or others (chmod 600)"
        )
    data = decode(path.read_text(), "private key file")
    if len(data) != 32:
        raise ValueError("Private key must be 32 bytes")
    key = PrivateKey(data)
    # Prevents using another environment's key, e.g. a QA key for production.
    if bytes(key.public_key) != bytes(load_public_key(stage, root)):
        raise ValueError(f"Private key does not belong to stage {stage}")
    return key


def read_store_file(
    stage: str, application: str, root: Path = ROOT
) -> dict[str, Any] | None:
    """Return an application's whole store document, or None if it has none."""
    path = secrets_path(stage, application, root)
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    if (
        not isinstance(data, dict)
        or data.get("version") != FORMAT_VERSION
        or not isinstance(data.get("key_id"), str)
        or not isinstance(data.get("values"), dict)
    ):
        raise ValueError(f"Unsupported secrets file format: {path.name}")
    return data


def read_store(stage: str, application: str, root: Path = ROOT) -> dict[str, str]:
    """Return an application's encrypted values keyed by secret name."""
    data = read_store_file(stage, application, root)
    return {} if data is None else data["values"]


def store_text(values: dict[str, str], public_key: PublicKey) -> str:
    """Serialize a store, recording which key its values were sealed with."""
    document = {
        "version": FORMAT_VERSION,
        # Rekey rewrites this line in every store, so Git reports a conflict
        # with any branch that changed secrets instead of silently dropping it.
        "key_id": key_id(public_key),
        "values": dict(sorted(values.items())),
    }
    return json.dumps(document, indent=2) + "\n"


def write_store(
    stage: str,
    application: str,
    values: dict[str, str],
    root: Path = ROOT,
    public_key: PublicKey | None = None,
) -> None:
    """Write an application's store, stamped with the key its values use.

    Without public_key the stage's current key is used, and an existing store
    must already match it, so values sealed with an older key are never relabeled.
    """
    path = secrets_path(stage, application, root)
    if public_key is None:
        public_key = load_public_key(stage, root)
        existing = read_store_file(stage, application, root)
        if existing is not None and existing["key_id"] != key_id(public_key):
            raise stale_store_error(stage, application)
    if not values:
        path.unlink(missing_ok=True)
        return
    write_atomic(path, store_text(values, public_key))


def validate_name(name: str) -> None:
    """Reject invalid or deployment-reserved secret names."""
    if not NAME.fullmatch(name):
        raise ValueError(f"Invalid secret name: {name}")
    if name in RESERVED:
        raise ValueError(
            f"{name} cannot be a secret: it is visible in service definitions or paths"
        )


def bound_payload(stage: str, application: str, name: str, value: str) -> bytes:
    """Serialize a secret with its stage, application, and name identity."""
    # Binding identity into the plaintext stops ciphertext being moved between apps or names.
    return json.dumps(
        {"stage": stage, "application": application, "name": name, "value": value}
    ).encode()


def seal(
    public_key: PublicKey, stage: str, application: str, name: str, value: str
) -> str:
    """Encrypt a value in a sealed box with its identity bound into the payload."""
    return encode(
        SealedBox(public_key).encrypt(bound_payload(stage, application, name, value))
    )


def unseal(
    private_key: PrivateKey, stage: str, application: str, name: str, ciphertext: str
) -> str:
    """Decrypt a secret and verify its bound identity and string value."""
    try:
        payload = json.loads(SealedBox(private_key).decrypt(decode(ciphertext, name)))
    except CryptoError:
        raise ValueError(f"Secret {name} cannot be decrypted with this key") from None
    if (payload.get("stage"), payload.get("application"), payload.get("name")) != (
        stage,
        application,
        name,
    ):
        raise ValueError(
            f"Secret {name} was encrypted for a different stage, application or name"
        )
    if not isinstance(payload.get("value"), str):
        raise TypeError(f"Secret {name} has an invalid value")
    return payload["value"]


def decrypt_all(
    stage: str, application: str, key_file: str | Path | None, root: Path = ROOT
) -> dict[str, str]:
    """Decrypt every stored secret for an application using its stage key."""
    data = read_store_file(stage, application, root)
    if data is None or not data["values"]:
        return {}
    if not key_file:
        raise ValueError(
            f"Encrypted secrets exist for {stage}/{application}; supply --secrets-key or {KEY_ENV}"
        )
    private_key = load_private_key(key_file, stage, root)
    if data["key_id"] != key_id(private_key.public_key):
        raise stale_store_error(stage, application)
    result = {}
    for name, ciphertext in data["values"].items():
        validate_name(name)
        result[name] = unseal(private_key, stage, application, name, ciphertext)
    return result


def keygen(stage: str, private_key_out: str | Path, root: Path = ROOT) -> None:
    """Create a stage's key pair; only public.key is written to the repository."""
    public_path = public_key_path(stage, root)
    if public_path.exists():
        raise ValueError(
            f"Stage {stage} already has a key; use secrets rekey to rotate it"
        )
    key = PrivateKey.generate()
    write_private_key(private_key_out, key)
    write_atomic(public_path, encode(key.public_key) + "\n")


def rekey(
    stage: str,
    old_key_file: str | Path,
    new_private_key_out: str | Path,
    root: Path = ROOT,
) -> int:
    """Re-encrypt every store for a stage with a new key pair.

    Everything is decrypted, re-encrypted and verified in-memory before any file
    changes. If writing is interrupted, key_id makes the mix of old and new stores
    visible to 'secrets check', and Git restores the previous state.
    """
    if Path(new_private_key_out).exists():
        raise FileExistsError(f"{new_private_key_out} already exists")
    old_key = load_private_key(old_key_file, stage, root)
    new_key = PrivateKey.generate()
    directory = secrets_dir(stage, root)
    new_stores = {}
    for path in sorted(directory.rglob("*.json")):
        application = path.relative_to(directory).with_suffix("").as_posix()
        values = {
            name: unseal(old_key, stage, application, name, ciphertext)
            for name, ciphertext in read_store(stage, application, root).items()
        }
        sealed = {
            name: seal(new_key.public_key, stage, application, name, value)
            for name, value in values.items()
        }
        # Prove the new key opens every value before anything is written.
        for name, value in values.items():
            if unseal(new_key, stage, application, name, sealed[name]) != value:
                raise ValueError(f"Re-encryption did not verify: {application}/{name}")
        new_stores[path] = store_text(sealed, new_key.public_key)
    write_private_key(new_private_key_out, new_key)
    for path, text in new_stores.items():
        write_atomic(path, text)
    # public.key goes last: until it changes, 'secrets check' flags a partial rekey.
    write_atomic(public_key_path(stage, root), encode(new_key.public_key) + "\n")
    return len(new_stores)


def plaintext_properties(content: bytes) -> Iterator[str]:
    """Yield keys of sensitive-looking properties that still hold a literal value."""
    for line in content.decode("utf-8", "replace").splitlines():
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if (
            separator
            and value
            and not key.startswith(("#", "!"))
            and SENSITIVE_PROPERTY.search(key)
            # Runtime references and URLs (e.g. token endpoints) are not secrets.
            and not value.startswith(("${", "{{", "http://", "https://"))
        ):
            yield key


def check_store(  # noqa: C901
    stage: str,
    application: str,
    root: Path,
    errors: list[str],
    warnings: list[str],
    expected_key_id: str | None = None,
) -> None:
    """Validate one application's encrypted store and report template/property issues."""
    label = f"{stage}/{application}"
    try:
        data = read_store_file(stage, application, root)
    except (ValueError, OSError) as exc:
        errors.append(f"{label}: {exc}")
        return
    sealed = {} if data is None else data["values"]
    if data is not None and expected_key_id and data["key_id"] != expected_key_id:
        errors.append(
            f"{label} encrypted with a different key than public.key; "
            "merge the latest rekey or restore an interruped one with git checkout"
        )
    try:
        used, files = template_names(stage, application, root)
    except (ValueError, OSError) as exc:
        errors.append(f"{label}:{exc}")
        return
    if not sealed:
        errors.append(f"{label}: no encrypted secrets")
    for name, ciphertext in sorted(sealed.items()):
        try:
            validate_name(name)
            # Ciphertext shorter than this cannot hold an empty value bound to this identity.
            minimum = crypto_box_SEALBYTES + len(
                bound_payload(stage, application, name, "")
            )
            if not isinstance(ciphertext, str) or len(decode(ciphertext, name)) < (
                minimum
            ):
                raise ValueError(
                    f"Secret {name} is not a sealed value for this stage, application and name"
                )
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
        if name not in used:
            errors.append(f"{label}: secret {name} is not used by any [[{name}]]")
    provided = set(sealed) | set(DEFAULTS) | RESERVED | {"gelf_host"}
    for name in sorted(used - provided):
        warnings.append(
            f"{label}: [[{name}]] is not an encrypted secret; "
            "it must come from the environment or --vars-file"
        )
    for filename, content in files.items():
        if filename.endswith(".properties"):
            warnings.extend(
                f"{label}: {filename} has a plaintext value for {key}"
                for key in plaintext_properties(content)
            )


def check(
    stage: str | None = None, application: str | None = None, root: Path = ROOT
) -> tuple[list[str], list[str]]:
    """Validate secrets stores without a private key; returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    if stage:
        stages = [stage]
    elif (root / "secrets").is_dir():
        stages = sorted(
            path.name for path in (root / "secrets").iterdir() if path.is_dir()
        )
    else:
        stages = []
    for stage_name in stages:
        directory = secrets_dir(stage_name, root)
        applications = (
            [application]
            if application
            else [
                path.relative_to(directory).with_suffix("").as_posix()
                for path in sorted(directory.rglob("*.json"))
            ]
        )
        expected_key_id = None
        if applications:
            try:
                expected_key_id = key_id(load_public_key(stage_name, root))
            except ValueError as exc:
                errors.append(f"{stage_name}: {exc}")
        for name in applications:
            check_store(stage_name, name, root, errors, warnings, expected_key_id)
    return errors, warnings


def compare_value(name: str, actual: str) -> None:
    """Prompt securely for an expected value and compare it without timing leakage."""
    if not sys.stdin.isatty():
        raise ValueError("--compare needs an interactive terminal")
    expected = getpass.getpass(f"Expected value for {name}: ")
    if not hmac.compare_digest(expected.encode(), actual.encode()):
        raise ValueError(f"{name} does not match the expected value")


def read_value(source: str | None) -> str:
    """Read a secret from standard input or prompt twice for confirmation."""
    if source == "-":
        value = sys.stdin.read()
        return value.removesuffix("\n")
    if not sys.stdin.isatty():
        raise ValueError("Use --value-from - to read a value from standard input")
    value = getpass.getpass("Secret value: ")
    if value != getpass.getpass("Confirm value: "):
        raise ValueError("Values do not match")
    return value


def parser() -> argparse.ArgumentParser:
    """Build the argument parser for secret-management subcommands."""
    result = argparse.ArgumentParser(
        prog="python -m deployment secrets",
        description="Manage encrypted per-environment secrets",
    )
    commands = result.add_subparsers(dest="command", required=True)
    keygen_parser = commands.add_parser("keygen", help="Create a stage key pair")
    keygen_parser.add_argument("--stage", required=True)
    keygen_parser.add_argument("--private-key-out", required=True)
    rekey_parser = commands.add_parser(
        "rekey", help="Rotate a stage key and re-encrypt its secrets"
    )
    rekey_parser.add_argument("--stage", required=True)
    rekey_parser.add_argument("--old-key", required=True)
    rekey_parser.add_argument("--private-key-out", required=True)
    check_parser = commands.add_parser(
        "check", help="Validate secrets against templates without a key"
    )
    check_parser.add_argument("--stage", help="Default: every stage")
    check_parser.add_argument("--application", help="Default: every application")
    for name, help_text in (
        ("set", "Encrypt and store a value"),
        ("unset", "Remove a value"),
        ("list", "List secret names (never values)"),
        ("verify", "Check every value decrypts with a key"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--stage", required=True)
        command.add_argument("--application", required=True)
        if name in {"set", "unset"}:
            command.add_argument("--name", required=True)
        if name == "set":
            command.add_argument(
                "--value-from",
                choices=("-",),
                help="Read from standard input instead of prompting",
            )
        if name == "verify":
            command.add_argument(
                "--secrets-key", default=os.environ.get(KEY_ENV), required=False
            )
            command.add_argument("--name", help="Verify only this secret")
            command.add_argument(
                "--compare",
                action="store_true",
                help="Prompt for the expected value of --name and report match or mismatch",
            )
    return result


def run(argv: list[str], root: Path = ROOT) -> None:  # noqa: C901, PLR0912
    """Execute a secret-management command against the selected repository root."""
    args = parser().parse_args(argv)
    if args.command == "keygen":
        keygen(args.stage, args.private_key_out, root)
        print(
            f"Created secrets/{args.stage}/public.key; commit it.\n"
            f"Store {args.private_key_out} securely and back it up offline."
        )
    elif args.command == "rekey":
        count = rekey(args.stage, args.old_key, args.private_key_out, root)
        print(
            f"Re-encrypted {count} application file(s) for {args.stage}. Commit all "
            f"changes under secrets/{args.stage} together. Rotate the secret values too if the old key leaked."
        )
    elif args.command == "set":
        validate_name(args.name)
        public_key = load_public_key(args.stage, root)
        value = read_value(args.value_from)
        values = read_store(args.stage, args.application, root)
        values[args.name] = seal(
            public_key, args.stage, args.application, args.name, value
        )
        write_store(args.stage, args.application, values, root)
        print(f"Encrypted {args.name} for {args.stage}/{args.application}")
    elif args.command == "unset":
        values = read_store(args.stage, args.application, root)
        if args.name not in values:
            raise ValueError(f"{args.name} is not set")
        del values[args.name]
        write_store(args.stage, args.application, values, root)
        print(f"Removed {args.name} from {args.stage}/{args.application}")
    elif args.command == "list":
        for name in sorted(read_store(args.stage, args.application, root)):
            print(name)
    elif args.command == "check":
        if args.application and not args.stage:
            raise ValueError("--application requires --stage")
        errors, warnings = check(args.stage, args.application, root)
        for message in warnings:
            print(f"WARNING {message}")
        for message in errors:
            print(f"ERROR {message}")
        if errors:
            raise ValueError(f"Secrets check found {len(errors)} error(s)")
        print(f"Secrets check passed with {len(warnings)} warning(s)")
    elif args.command == "verify":
        if not args.secrets_key:
            raise ValueError(f"Supply --secrets-key or {KEY_ENV}")
        if args.compare and not args.name:
            raise ValueError("--compare requires --name")
        # Decrypting every value also proves the whole file is consistent.
        values = decrypt_all(args.stage, args.application, args.secrets_key, root)
        if args.name and args.name not in values:
            raise ValueError(f"{args.name} is not set")
        # Never print values: this output can end up in terminals and CI logs.
        if args.compare:
            compare_value(args.name, values[args.name])
            print(f"{args.name}: matches the expected value")
            return
        for name in [args.name] if args.name else sorted(values):
            print(f"{name}: decrypted")
        print(
            f"{len(values)} secret(s) decrypted successfully for {args.stage}/{args.application}"
        )
