"""Bounded OCI archive verification without extraction or trust in exporter metadata."""

import hashlib
import json
import tarfile
from pathlib import Path
from typing import Any


def file_sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inspect(
    path: Path, platform: str, entrypoint: list[str], labels: dict[str, str]
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 2_000_000_000:
        raise ValueError("bounded regular OCI archive required")
    with tarfile.open(path, mode="r:") as archive:
        entries: dict[str, tarfile.TarInfo] = {}
        total = 0
        for member in archive:
            name = member.name.removeprefix("./")
            if (
                name in entries
                or not (member.isfile() or member.isdir())
                or ".." in name.split("/")
            ):
                raise ValueError("invalid OCI archive member")
            if member.isdir():
                continue
            total += member.size
            if total > 2_000_000_000 or len(entries) >= 10000:
                raise ValueError("OCI archive exceeds budget")
            entries[name] = member

        def read(name: str) -> bytes:
            item = entries[name]
            if item.size > 2_000_000:
                raise ValueError("OCI metadata exceeds budget")
            stream = archive.extractfile(item)
            if stream is None:
                raise ValueError("missing OCI entry")
            return stream.read()

        def blob(descriptor: dict[str, Any], *, metadata: bool = False) -> bytes:
            from ..control.contracts import sha

            identity = sha(descriptor["digest"])
            name = "blobs/sha256/" + identity[7:]
            item = entries[name]
            if (
                descriptor.get("urls")
                or type(descriptor["size"]) is not int
                or item.size != descriptor["size"]
            ):
                raise ValueError("OCI descriptor differs")
            stream = archive.extractfile(item)
            hasher = hashlib.sha256()
            if stream is None:
                raise ValueError("missing OCI blob")
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
            if hasher.hexdigest() != identity[7:]:
                raise ValueError("OCI blob digest differs")
            return read(name) if metadata else b""

        if json.loads(read("oci-layout")) != {"imageLayoutVersion": "1.0.0"}:
            raise ValueError("unsupported OCI layout")
        index = json.loads(read("index.json"))
        if index.get("schemaVersion") != 2 or len(index.get("manifests", [])) != 1:
            raise ValueError("single-platform OCI image required")
        descriptor = index["manifests"][0]
        if descriptor["mediaType"] != "application/vnd.oci.image.manifest.v1+json":
            raise ValueError("nested indexes/attestations are not image manifests")
        manifest = json.loads(blob(descriptor, metadata=True))
        if manifest.get("schemaVersion") != 2 or not 1 <= len(manifest.get("layers", [])) <= 128:
            raise ValueError("bounded image layers required")
        for layer in manifest["layers"]:
            if layer["mediaType"] not in {
                "application/vnd.oci.image.layer.v1.tar",
                "application/vnd.oci.image.layer.v1.tar+gzip",
            }:
                raise ValueError("unsupported/foreign image layer")
            blob(layer)
        config = json.loads(blob(manifest["config"], metadata=True))
        settings = config["config"]
        if (
            platform != config["os"] + "/" + config["architecture"]
            or settings.get("User") != "65532:65532"
            or settings.get("WorkingDir") != "/app"
            or settings.get("Entrypoint") != entrypoint
            or settings.get("Cmd") not in (None, [])
            or any(settings.get("Labels", {}).get(k) != v for k, v in labels.items())
        ):
            raise ValueError("image execution recipe differs")
        return {"image_digest": descriptor["digest"], "archive_sha256": file_sha(path)}
