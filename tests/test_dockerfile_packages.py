"""Every image ships the whole shared package tree.

A new module under ``packages/`` used to break an image that copied only the
subdirectories it knew about: the API container booted with
``ModuleNotFoundError: No module named 'packages.version'`` while the local test
suite was green. Copying the tree once removes that class of failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# The web image builds a Node bundle and never imports Python packages.
PYTHON_IMAGES = [
    path for path in sorted(ROOT.glob("Dockerfile*")) if "web-studio" not in path.name
]
DOCKERFILES = PYTHON_IMAGES


def test_the_repository_still_has_images_to_check():
    assert DOCKERFILES


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda path: path.name)
def test_an_image_copies_the_whole_package_tree(dockerfile: Path):
    lines = [line.strip() for line in dockerfile.read_text().splitlines()]
    copies = [line for line in lines if line.startswith("COPY packages")]
    assert "COPY packages ./packages" in copies, dockerfile.name
    # No hand-listed subset may remain beside the tree copy.
    assert copies == ["COPY packages ./packages"], dockerfile.name
