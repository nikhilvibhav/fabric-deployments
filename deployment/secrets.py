"""Per-environment public-key encrypted secrets stored alongside configuration.

Each value is sealed separately with the environment's committed public key, so
contributors can add or rotate values without being able to read existing ones.
Only the holder of the environment's private key can decrypt them.
"""

import argparse
import base64
import getpass
import hmac
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from pathlib import Path

from nacl.bindings import crypto_box_SEALBYTES
from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox

from .config import DEFAULTS, ROOT, SEGMENT, relative_path, template_names

FORMAT_VERSION = 1
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
GENERATION_ID = re.compile(r"[0-9a-f]{32}")
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


def secrets_dir(stage, root=ROOT):
    """Return the validated directory holding secrets for a stage."""
    if not SEGMENT.fullmatch(stage) or stage in {".", ".."}:
        raise ValueError("Invalid stage")
    return root / "secrets" / stage


def active_data_dir(stage, root=ROOT):
    """Return the active generation directory, or the legacy stage directory."""
    stage_root = secrets_dir(stage, root)
    current_file = stage_root / "CURRENT"

    # Existing stages use the legacy layout until first generation is activated.
    if not current_file.exists():
        return stage_root

    generation = current_file.read_text(encoding="ascii").strip()
    if not GENERATION_ID.fullmatch(generation):
        raise ValueError(f"Invalid CURRENT file for stage {stage}")

    directory = stage_root / ".generations" / generation
    if not directory.is_dir():
        raise ValueError(f"Active secrets generation is missing for stage {stage}")
    return directory


def _secrets_path_in(data_dir, application):
    """Return an application's store path within a particular directory."""
    relative_path(application, "application")
    return data_dir.joinpath(*application.split("/")).with_suffix(".json")


def public_key_path(stage, root=ROOT):
    """Return the path to a stage's committed public key."""
    return active_data_dir(stage, root) / "public.key"


def secrets_path(stage, application, root=ROOT):
    """Return the validated encrypted store path for an application."""
    return _secrets_path_in(active_data_dir(stage, root), application)


def encode(data):
    """Encode bytes or key material as base64 text."""
    return base64.b64encode(bytes(data)).decode()


def decode(text, label):
    """Decode strict base64 text and identify malformed input by label."""
    try:
        return base64.b64decode(text.strip(), validate=True)
    except ValueError:
        raise ValueError(f"Invalid base64 in {label}") from None


def write_atomic(path, text):
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


def write_private_key(path, key):
    """Create a private key file without overwriting existing files."""
    path = Path(path)
    # O_EXCL never overwrites an existing key; 0600 limits access on POSIX.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(encode(key) + "\n")


def load_public_key(stage, root=ROOT):
    """Load and validate the 32-byte public key for a stage."""
    path = public_key_path(stage, root)
    if not path.is_file():
        raise ValueError(f"No public key for stage {stage}; run 'secrets keygen' first")
    data = decode(path.read_text(), path.name)
    if len(data) != 32:
        raise ValueError("Public key must be 32 bytes")
    return PublicKey(data)


def load_private_key(path, stage, root=ROOT):
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


def _read_store_in(data_dir, application):
    """Read and validate an application's store from a specific directory."""
    path = _secrets_path_in(data_dir, application)
    if not path.is_file():
        return {}
    data = json.loads(path.read_text())
    if (
        not isinstance(data, dict)
        or data.get("version") != FORMAT_VERSION
        or not isinstance(data.get("values"), dict)
    ):
        raise ValueError(f"Unsupported secrets file format: {path.name}")
    return data["values"]


def read_store(stage, application, root=ROOT):
    """Read an application's store from its active generation."""
    return _read_store_in(active_data_dir(stage, root), application)


def _write_store_in(data_dir, application, values):
    """Persist encrypted values in sorted JSON into a specific directory."""
    path = _secrets_path_in(data_dir, application)
    if not values:
        path.unlink(missing_ok=True)
        return
    write_atomic(
        path,
        json.dumps(
            {"version": FORMAT_VERSION, "values": dict(sorted(values.items()))},
            indent=2,
        )
        + "\n",
    )


def write_store(stage, application, values, root=ROOT):
    """Persist encrypted values in sorted JSON into the application's active directory."""
    _write_store_in(active_data_dir(stage, root), application, values)


def validate_name(name):
    """Reject invalid or deployment-reserved secret names."""
    if not NAME.fullmatch(name):
        raise ValueError(f"Invalid secret name: {name}")
    if name in RESERVED:
        raise ValueError(
            f"{name} cannot be a secret: it is visible in service definitions or paths"
        )


def bound_payload(stage, application, name, value):
    """Serialize a secret with its stage, application, and name identity."""
    # Binding identity into the plaintext stops ciphertext being moved between apps or names.
    return json.dumps(
        {"stage": stage, "application": application, "name": name, "value": value}
    ).encode()


def seal(public_key, stage, application, name, value):
    """Encrypt a value in a sealed box with its identity bound into the payload."""
    return encode(
        SealedBox(public_key).encrypt(bound_payload(stage, application, name, value))
    )


def unseal(private_key, stage, application, name, ciphertext):
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


def decrypt_all(stage, application, key_file, root=ROOT):
    """Decrypt every stored secret for an application using its stage key."""
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
    """Generate a stage key pair, storing the private key separately from the public key."""
    public_path = public_key_path(stage, root)
    if public_path.exists():
        raise ValueError(
            f"Stage {stage} already has a key; use 'secrets rekey' to rotate it"
        )
    key = PrivateKey.generate()
    write_private_key(private_key_out, key)
    write_atomic(public_path, encode(key.public_key) + "\n")


def rekey(stage, old_key_file, new_private_key_out, root=ROOT):
    """Build and verify a new secrets generation, then activate it with CURRENT."""
    stage_root = secrets_dir(stage, root)
    data_dir, store_paths = _active_store_files(stage, root)

    # Read and decrypt every current store before creating or activating anything.
    old_key = load_private_key(old_key_file, stage, root)
    stores = {}
    for path in store_paths:
        application = path.relative_to(data_dir).with_suffix("").as_posix()
        sealed = _read_store_in(data_dir, application)
        # Decrypt everything first so a bad entry aborts before any file changes.
        stores[application] = {
            name: unseal(old_key, stage, application, name, ciphertext)
            for name, ciphertext in sealed.items()
        }

    generation = uuid.uuid4().hex
    generations_dir = stage_root / ".generations"
    temporary_dir = generations_dir / (".tmp-" + generation)
    final_dir = generations_dir / generation

    new_key = PrivateKey.generate()

    # Build the new generation without changing the active one.
    write_atomic(temporary_dir / "public.key", encode(new_key.public_key) + "\n")

    for application, values in stores.items():
        encrypted = {
            name: seal(new_key.public_key, stage, application, name, value)
            for name, value in values.items()
        }
        _write_store_in(temporary_dir, application, encrypted)

    # Read the staged stores back to prove the new key decrypts them correctly.
    for application, expected_values in stores.items():
        staged_values = _read_store_in(temporary_dir, application)
        if set(staged_values) != set(expected_values):
            raise ValueError(f"Staged secrets did not verify for {stage}/{application}")

        for name, expected_value in expected_values.items():
            actual_value = unseal(
                new_key, stage, application, name, staged_values[name]
            )
            if actual_value != expected_value:
                raise ValueError(
                    f"Staged secrets did not verify: {stage}/{application}/{name}"
                )

    # Save the private key before activation. Keep it outside of the repository.
    write_private_key(new_private_key_out, new_key)

    # Publish the complete generation, then switch the active pointer in CURRENT.
    if final_dir.exists():
        raise FileExistsError(f"Secrets generation already exists: {generation}")
    os.rename(temporary_dir, final_dir)
    write_atomic(stage_root / "CURRENT", generation + "\n")

    return len(stores)


def plaintext_properties(content):
    """Keys of sensitive-looking properties that still hold a literal value."""
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


def check_store(stage, application, root, errors, warnings):
    """Validate one application's encrypted store and report template/property issues."""
    label = f"{stage}/{application}"
    try:
        sealed = read_store(stage, application, root)
        used, files = template_names(stage, application, root)
    except (ValueError, OSError) as exc:
        errors.append(f"{label}: {exc}")
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


def _active_store_files(stage, root=ROOT):
    """Return the active data dir and its application store files."""
    data_dir = active_data_dir(stage, root)
    stage_root = secrets_dir(stage, root)
    paths = sorted(data_dir.rglob("*.json"))

    # No CURRENT means legacy layout; ignore inactive generations and staging files
    if data_dir == stage_root:
        paths = [
            path
            for path in paths
            if path.relative_to(stage_root).parts[0] != ".generations"
        ]

    return data_dir, paths


def check(stage=None, application=None, root=ROOT):
    """Validate secrets stores without a private key; returns (errors, warnings)."""
    errors, warnings = [], []
    if stage:
        stages = [stage]
    elif (root / "secrets").is_dir():
        stages = sorted(
            path.name for path in (root / "secrets").iterdir() if path.is_dir()
        )
    else:
        stages = []
    for stage_name in stages:
        data_dir, store_paths = _active_store_files(stage_name, root)
        applications = (
            [application]
            if application
            else [
                path.relative_to(data_dir).with_suffix("").as_posix()
                for path in store_paths
            ]
        )
        if applications:
            try:
                load_public_key(stage_name, root)
            except ValueError as exc:
                errors.append(f"{stage_name}: {exc}")
        for name in applications:
            check_store(stage_name, name, root, errors, warnings)
    return errors, warnings


def compare_value(name, actual):
    """Prompt securely for an expected value and compare it without timing leakage."""
    if not sys.stdin.isatty():
        raise ValueError("--compare needs an interactive terminal")
    expected = getpass.getpass(f"Expected value for {name}: ")
    if not hmac.compare_digest(expected.encode(), actual.encode()):
        raise ValueError(f"{name} does not match the expected value")


def read_value(source):
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


def parser():
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


def run(argv, root=ROOT):
    """Execute a secret-management command against the selected repository root."""
    args = parser().parse_args(argv)
    if args.command == "keygen":
        keygen(args.stage, args.private_key_out, root)
        print(
            f"Created secrets/{args.stage}/public.key; commit it. "
            f"Store {args.private_key_out} securely and back it up offline."
        )
    elif args.command == "rekey":
        count = rekey(args.stage, args.old_key, args.private_key_out, root)
        print(
            f"Re-encrypted {count} application file(s) for {args.stage}. Commit the changes, "
            "then destroy the old key. Rotate the secret values too if the old key leaked."
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
