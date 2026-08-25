"""Generate Python dataclasses from the Fluidefy XSD schemas using xsdata.

Each XSD is generated into its own subpackage under ``fluidefy_models`` because
the schemas share element names (ENTETE, LIGNES, DLIN, ...) with different
structures, so they must not be merged into a single namespace.

Usage:
    python generate_models.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# XSD file -> target package (relative to fluidefy_models)
SCHEMAS: dict[str, str] = {
    "Fluide_Orders.xsd": "orders",
    "Fluide_Invoic.xsd": "invoic",
    "Fluide_Desadv.xsd": "desadv",
    "Fluide_Recadv.xsd": "recadv",
    "Fluide_ProduitElement.xsd": "produit_element",
    "Fluide_ProduitComposition.xsd": "produit_composition",
    "Fluide_ProduitReferencement.xsd": "produit_referencement",
}

ROOT = Path(__file__).resolve().parent
XSD_DIR = ROOT.parent.parent / "XSD"
OUT_DIR = ROOT.parent / "app" / "fluidefy_models"
PACKAGE_PREFIX = "app.fluidefy_models"


def main() -> int:
    if not XSD_DIR.is_dir():
        print(f"XSD directory not found: {XSD_DIR}", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "__init__.py").touch()

    for xsd_name, pkg in SCHEMAS.items():
        xsd_path = XSD_DIR / xsd_name
        if not xsd_path.is_file():
            print(f"WARNING: missing schema {xsd_path}, skipping", file=sys.stderr)
            continue

        package = f"{PACKAGE_PREFIX}.{pkg}"
        print(f"Generating {xsd_name} -> {package}")
        result = subprocess.run(
            [sys.executable, "-m", "xsdata", "generate", str(xsd_path), "--package", package],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(result.stdout, file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            return result.returncode

    print("Done. Generated models under", OUT_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
