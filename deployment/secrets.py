"""Per-environment public-key encrypted secrets stored alongside configuration.

Each value is sealed separately with the environment's committed public key, so
contributors can add or rotate values without being able to read existing ones.
Only the holder of the environment's private key can decrypt them.
"""

import argparse
import base64
import getpass
import json
import os
import pprint
import re
import stat
import sys
import tempfile
from pathlib import Path

from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox

from .config import ROOT, SEGMENT, relative_path

FORMAT_VERSION = 1
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
KEY_ENV = "CAMEL_SECRETS_KEY_FILE"
# Values rendered into service definitions/process arguments, or that select paths.
RESERVED = {
    "stage", "application", "version", "application_name", "application_execute",
    "deploy_to", "release_path", "java_bin", "jvm_options", "camel_log_dir",
}


def secrets_dir(stage, root=ROOT):
    if not SEGMENT.fullmatch(stage) or stage in {".", ".."}:
        raise ValueError("Invalid stage")
    return root / "secrets" / stage


def public_key_path(stage, root=ROOT):
    return secrets_dir(stage, root) / "public.key"


def secrets_path(stage, application, root=ROOT):
    relative_path(application, "application")
    return secrets_dir(stage, root).joinpath(*application.split("/")).with_suffix(".json")


def encode(data):
    return base64.b64encode(bytes(data)).decode()


def decode(text, label):
    try:
        return base64.b64decode(text.strip(), validate=True)
    except ValueError:
        raise ValueError(f"Invalid base64 in {label}") from None


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(handle, "w", newline="\n") as output:
            output.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_private_key(path, key):
    path = Path(path)
    # O_EXCL never overwrites an existing key; 0600 limits access on POSIX.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(encode(key) + "\n")


def load_public_key(stage, root=ROOT):
    path = public_key_path(stage, root)
    if not path.is_file():
        raise ValueError(f"No public key for stage {stage}; run 'secrets keygen' first")
    data = decode(path.read_text(), path.name)
    if len(data) != 32:
        raise ValueError("Public key must be 32 bytes")
    return PublicKey(data)


def load_private_key(path, stage, root=ROOT):
    path = Path(path)
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Private key file must not be readable by group or others (chmod 600)")
    data = decode(path.read_text(), "private key file")
    if len(data) != 32:
        raise ValueError("Private key must be 32 bytes")
    key = PrivateKey(data)
    # Prevents using another environment's key, e.g. a QA key for production.
    if bytes(key.public_key) != bytes(load_public_key(stage, root)):
        raise ValueError(f"Private key does not belong to stage {stage}")
    return key


def read_store(stage, application, root=ROOT):
    path = secrets_path(stage, application, root)
    if not path.is_file():
        return {}
    data = json.loads(path.read_text())
    if data.get("version") != FORMAT_VERSION or not isinstance(data.get("values"), dict):
        raise ValueError(f"Unsupported secrets file format: {path.name}")
    return data["values"]


def write_store(stage, application, values, root=ROOT):
    path = secrets_path(stage, application, root)
    if not values:
        path.unlink(missing_ok=True)
        return
    write_atomic(path, json.dumps(
        {"version": FORMAT_VERSION, "values": dict(sorted(values.items()))}, indent=2
    ) + "\n")


def validate_name(name):
    if not NAME.fullmatch(name):
        raise ValueError(f"Invalid secret name: {name}")
    if name in RESERVED:
        raise ValueError(f"{name} cannot be a secret: it is visible in service definitions or paths")


def seal(public_key, stage, application, name, value):
    # Binding identity into the plaintext stops ciphertext being moved between apps or names.
    payload = json.dumps({"stage": stage, "application": application, "name": name, "value": value})
    return encode(SealedBox(public_key).encrypt(payload.encode()))


def unseal(private_key, stage, application, name, ciphertext):
    try:
        payload = json.loads(SealedBox(private_key).decrypt(decode(ciphertext, name)))
    except CryptoError:
        raise ValueError(f"Secret {name} cannot be decrypted with this key") from None
    if (payload.get("stage"), payload.get("application"), payload.get("name")) != (stage, application, name):
        raise ValueError(f"Secret {name} was encrypted for a different stage, application or name")
    if not isinstance(payload.get("value"), str):
        raise ValueError(f"Secret {name} has an invalid value")
    return payload["value"]


def decrypt_all(stage, application, key_file, root=ROOT):
    sealed = read_store(stage, application, root)
    if not sealed:
        return {}
    if not key_file:
        raise ValueError(
            f"Encrypted secrets exist for {stage}/{application}; supply --secrets-key or {KEY_ENV}"
        )
    private_key = load_private_key(key_file, stage, root)
    result = {}
    for name, ciphertext in sealed.items():
        validate_name(name)
        result[name] = unseal(private_key, stage, application, name, ciphertext)
    return result


def keygen(stage, private_key_out, root=ROOT):
    public_path = public_key_path(stage, root)
    if public_path.exists():
        raise ValueError(f"Stage {stage} already has a key; use 'secrets rekey' to rotate it")
    key = PrivateKey.generate()
    write_private_key(private_key_out, key)
    write_atomic(public_path, encode(key.public_key) + "\n")


def rekey(stage, old_key_file, new_private_key_out, root=ROOT):
    old_key = load_private_key(old_key_file, stage, root)
    stores = {}
    for path in sorted(secrets_dir(stage, root).rglob("*.json")):
        application = path.relative_to(secrets_dir(stage, root)).with_suffix("").as_posix()
        # Decrypt everything first so a bad entry aborts before any file changes.
        stores[application] = {
            name: unseal(old_key, stage, application, name, ciphertext)
            for name, ciphertext in read_store(stage, application, root).items()
        }
    new_key = PrivateKey.generate()
    write_private_key(new_private_key_out, new_key)
    for application, values in stores.items():
        write_store(stage, application, {
            name: seal(new_key.public_key, stage, application, name, value)
            for name, value in values.items()
        }, root)
    write_atomic(public_key_path(stage, root), encode(new_key.public_key) + "\n")
    return len(stores)


def read_value(source):
    if source == "-":
        value = sys.stdin.read()
        return value[:-1] if value.endswith("\n") else value
    if not sys.stdin.isatty():
        raise ValueError("Use --value-from - to read a value from standard input")
    value = getpass.getpass("Secret value: ")
    if value != getpass.getpass("Confirm value: "):
        raise ValueError("Values do not match")
    return value


def parser():
    result = argparse.ArgumentParser(prog="python -m deployment secrets",
                                     description="Manage encrypted per-environment secrets")
    commands = result.add_subparsers(dest="command", required=True)
    keygen_parser = commands.add_parser("keygen", help="Create a stage key pair")
    keygen_parser.add_argument("--stage", required=True)
    keygen_parser.add_argument("--private-key-out", required=True)
    rekey_parser = commands.add_parser("rekey", help="Rotate a stage key and re-encrypt its secrets")
    rekey_parser.add_argument("--stage", required=True)
    rekey_parser.add_argument("--old-key", required=True)
    rekey_parser.add_argument("--private-key-out", required=True)
    for name, help_text in (("set", "Encrypt and store a value"),
                            ("unset", "Remove a value"),
                            ("list", "List secret names (never values)"),
                            ("verify", "Check every value decrypts with a key")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--stage", required=True)
        command.add_argument("--application", required=True)
        if name in {"set", "unset"}:
            command.add_argument("--name", required=True)
        if name == "set":
            command.add_argument("--value-from", choices=("-",),
                                 help="Read from standard input instead of prompting")
        if name == "verify":
            command.add_argument("--secrets-key", default=os.environ.get(KEY_ENV), required=False)
    return result


def run(argv, root=ROOT):
    args = parser().parse_args(argv)
    if args.command == "keygen":
        keygen(args.stage, args.private_key_out, root)
        print(f"Created secrets/{args.stage}/public.key; commit it. "
              f"Store {args.private_key_out} securely and back it up offline.")
    elif args.command == "rekey":
        count = rekey(args.stage, args.old_key, args.private_key_out, root)
        print(f"Re-encrypted {count} application file(s) for {args.stage}. Commit the changes, "
              "then destroy the old key. Rotate the secret values too if the old key leaked.")
    elif args.command == "set":
        validate_name(args.name)
        public_key = load_public_key(args.stage, root)
        value = read_value(args.value_from)
        values = read_store(args.stage, args.application, root)
        values[args.name] = seal(public_key, args.stage, args.application, args.name, value)
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
    elif args.command == "verify":
        if not args.secrets_key:
            raise ValueError(f"Supply --secrets-key or {KEY_ENV}")
        values = decrypt_all(args.stage, args.application, args.secrets_key, root)
        print(f"{len(values)} secret(s) decrypted successfully for {args.stage}/{args.application}")
        pprint.pprint(values, indent=2, expand=True, sort_dicts=False)
