"""Local CA and HTTPS certificate lifecycle for DuDu FitDance."""
from __future__ import annotations

import datetime
import os
from pathlib import Path
import subprocess

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import dance_cache as core

CA_NAME = "VRChat DanceAssistant Local CA"
SERVER_NAMES = ("global-cdn.dudufit.dance", "api.pypy.dance")


def certificate_directory() -> Path:
    return Path(core.data_dir()) / "dudu-https"


def certificate_paths() -> tuple[Path, Path, Path]:
    root = certificate_directory()
    return root / "ca.crt", root / "server.crt", root / "server.key"


def _write_private(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _public_bytes(certificate):
    return certificate.public_bytes(serialization.Encoding.PEM)


def _private_bytes(key):
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption())


def _new_certificate():
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, CA_NAME)])
    ca_certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                      .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
                      .not_valid_before(now - datetime.timedelta(days=1))
                      .not_valid_after(now + datetime.timedelta(days=365 * 5))
                      .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                      .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
                      .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                      .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True,
                                                  crl_sign=True, content_commitment=False,
                                                  key_encipherment=False, data_encipherment=False,
                                                  key_agreement=False, encipher_only=False,
                                                  decipher_only=False), critical=True)
                      .sign(ca_key, hashes.SHA256()))
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_certificate = (x509.CertificateBuilder().subject_name(
        x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, SERVER_NAMES[0])]))
        .issuer_name(ca_certificate.subject).public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365 * 2))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name) for name in SERVER_NAMES]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256()))
    return ca_key, ca_certificate, server_key, server_certificate


def _powershell(command: str):
    return subprocess.run(["powershell.exe", "-NoProfile", "-Command", command],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)


def _quoted(path: Path) -> str:
    return str(path).replace("'", "''")


def trust_ca_certificate(path: Path):
    result = _powershell(f"Import-Certificate -FilePath '{_quoted(path)}' -CertStoreLocation Cert:\\CurrentUser\\Root")
    if result.returncode:
        raise RuntimeError("无法信任本机 CA：" + (result.stdout + result.stderr).strip())


def _ca_thumbprint(path: Path) -> str:
    return x509.load_pem_x509_certificate(path.read_bytes()).fingerprint(hashes.SHA1()).hex().upper()


def certificate_ready() -> bool:
    ca_path, certificate_path, key_path = certificate_paths()
    if not all(path.is_file() for path in (ca_path, certificate_path, key_path)):
        return False
    try:
        ca_certificate = x509.load_pem_x509_certificate(ca_path.read_bytes())
        server_certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
        private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        now = datetime.datetime.now(datetime.timezone.utc)
        for certificate in (ca_certificate, server_certificate):
            if not certificate.not_valid_before_utc <= now <= certificate.not_valid_after_utc:
                return False
        server_certificate.verify_directly_issued_by(ca_certificate)
        if private_key.public_key().public_numbers() != server_certificate.public_key().public_numbers():
            return False
        names = set(server_certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName))
        if not set(SERVER_NAMES).issubset(names):
            return False
        thumbprint = _ca_thumbprint(ca_path)
        result = _powershell(f"Test-Path -LiteralPath 'Cert:\\CurrentUser\\Root\\{thumbprint}'")
    except (OSError, ValueError, TypeError, InvalidSignature):
        return False
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _untrust_ca_certificate(path: Path):
    thumbprint = _ca_thumbprint(path)
    return _powershell(f"Remove-Item -LiteralPath 'Cert:\\CurrentUser\\Root\\{thumbprint}' -ErrorAction Stop")


def ensure_certificate() -> tuple[Path, Path]:
    ca_path, certificate_path, key_path = certificate_paths()
    if certificate_ready():
        return certificate_path, key_path
    if ca_path.is_file():
        try:
            _untrust_ca_certificate(ca_path)
        except Exception:
            pass
    ca_key, ca_certificate, server_key, server_certificate = _new_certificate()
    _write_private(ca_path.with_suffix(".key"), _private_bytes(ca_key))
    _write_private(ca_path, _public_bytes(ca_certificate))
    _write_private(certificate_path, _public_bytes(server_certificate))
    _write_private(key_path, _private_bytes(server_key))
    trust_ca_certificate(ca_path)
    if not certificate_ready():
        raise RuntimeError("本机 CA 已生成，但 Windows 未确认信任。")
    return certificate_path, key_path


def remove_certificate():
    ca_path, _, _ = certificate_paths()
    if ca_path.is_file():
        result = _untrust_ca_certificate(ca_path)
        if result.returncode:
            raise RuntimeError("无法移除本机 CA：" + (result.stdout + result.stderr).strip())
    directory = certificate_directory()
    if directory.exists():
        import shutil
        shutil.rmtree(directory)
