"""Pre-pull the build-check images and create the package-cache volumes, so the first code
recommendation does not spend minutes pulling an image or downloading dependencies.

The remediation agent's build check compiles each edited service inside the image its own Dockerfile
builds with (remediation/source.py). That image is pulled on first use, and Go/C#/Java downloads land
in a named Docker volume kept between checks. Running this once (for example after scripts\\app-up.bat,
or by hand) moves both costs off the critical path.

    python scripts/prewarm.py            pull every service's builder image and make the cache volumes
    python scripts/prewarm.py --images   only pull images (skip the cache volumes)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from remediation.policy import SERVICE_DOCS  # noqa: E402
from remediation.source import BUILD_CACHES, builder_image, ensure_checkout  # noqa: E402


def _docker(*args: str) -> int:
    try:
        return subprocess.run(["docker", *args], check=False).returncode
    except FileNotFoundError:
        print("docker CLI not found; nothing to prewarm", file=sys.stderr)
        return 1


def builder_images(checkout: Path, docs: dict) -> dict[str, str]:
    """{image: a service that uses it} for every service with source and a Dockerfile FROM line."""
    images: dict[str, str] = {}
    for name, svc in (docs.get("services") or {}).items():
        code = svc.get("code") or {}
        if not code.get("dir"):
            continue
        dockerfile = checkout / code["dir"] / "Dockerfile"
        if not dockerfile.exists():
            continue
        image = builder_image(dockerfile)
        if image:
            images.setdefault(image, name)
    return images


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--images", action="store_true", help="only pull images; skip cache volumes")
    args = parser.parse_args(argv)

    docs = yaml.safe_load(SERVICE_DOCS.read_text(encoding="utf-8")) or {}
    checkout = ensure_checkout(docs["source"])
    images = builder_images(checkout, docs)
    print(f"pulling {len(images)} builder image(s)")
    failed = 0
    for image, service in sorted(images.items()):
        print(f"  {image}  (for {service})")
        if _docker("pull", image) != 0:
            failed += 1
    if not args.images:
        volumes = sorted({volume for mounts in BUILD_CACHES.values() for volume, _ in mounts})
        for volume in volumes:
            _docker("volume", "create", volume)
        print(f"ensured cache volume(s): {', '.join(volumes)}")
    if failed:
        print(f"{failed} image(s) could not be pulled", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
