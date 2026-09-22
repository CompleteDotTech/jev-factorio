"""Split the complete Mermaid workflow and render high-resolution PNGs."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import xml.etree.ElementTree as ET


SECTIONS = {
    "SUP": "01-campaign-supervision",
    "CTRL": "02-hierarchical-controller",
    "GOAL_TREE": "03-goals-and-bootstrap",
    "FACTORY": "04-factory-dependency-planner",
    "JEV": "05-jev-candidate-judgment",
    "NATIVE": "06-native-action-execution",
    "VERIFY": "07-pending-action-verification",
    "REPAIR": "08-automatic-codex-repair",
}
NODE = re.compile(r'\b([A-Z][A-Z0-9_]*)\s*(\["[^"]*"\]|\{"[^"]*"\})')


def split_sections(directory: Path) -> list[Path]:
    complete = directory / "mmd/00-complete-workflow.mmd"
    source = complete.read_text()
    definitions = dict(NODE.findall(source))
    paths = [complete]
    for section, filename in SECTIONS.items():
        match = re.search(
            rf"^subgraph {section}\[.*?^end\s*$", source, re.MULTILINE | re.DOTALL
        )
        if match is None:
            raise ValueError(f"Missing workflow section: {section}")
        body = match.group()
        local = {name for name, _ in NODE.findall(body)}
        without_labels = re.sub(r'"[^"]*"|\|[^|]*\|', "", body)
        referenced = set(re.findall(r"\b[A-Z][A-Z0-9_]*\b", without_labels))
        external = sorted((referenced & definitions.keys()) - local)
        continuation = "\n".join(
            f"    {name}{definitions[name]}" for name in external
        )
        if external:
            continuation = (
                "\n" + continuation + "\n"
                + "classDef continuation fill:#fff7ed,stroke:#c2410c,stroke-dasharray:5 4;\n"
                + f"class {','.join(external)} continuation;\n"
            )
        path = directory / "mmd" / f"{filename}.mmd"
        path.write_text(("flowchart TD\n\n" + body + "\n" + continuation).rstrip() + "\n")
        paths.append(path)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mmdc", required=True)
    parser.add_argument("--browser", required=True)
    parser.add_argument("--scale", type=float, default=2)
    parser.add_argument("--only", help="Render just this source filename stem")
    arguments = parser.parse_args()
    if not math.isfinite(arguments.scale) or arguments.scale <= 0:
        parser.error("--scale must be positive and finite")
    directory = Path(__file__).resolve().parent
    workspace = directory.parents[1]
    temporary = workspace / "runs/mermaid-render/tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    output = directory / "pngs"
    output.mkdir(exist_ok=True)
    browser_config = temporary / "puppeteer.json"
    browser_config.write_text(json.dumps({
        "executablePath": str(Path(arguments.browser).resolve()),
        "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        "timeout": 120000,
        "protocolTimeout": 240000,
    }))
    environment = {**os.environ, "TMPDIR": str(temporary)}
    libraries = workspace / "runs/mermaid-render/browser-libs"
    if libraries.exists():
        environment["LD_LIBRARY_PATH"] = os.pathsep.join([
            str(libraries / "usr/lib/x86_64-linux-gnu"),
            str(libraries / "lib/x86_64-linux-gnu"),
            environment.get("LD_LIBRARY_PATH", ""),
        ])
        fonts = ET.Element("fontconfig")
        ET.SubElement(fonts, "dir").text = str(libraries / "usr/share/fonts")
        ET.SubElement(fonts, "cachedir").text = str(temporary / "font-cache")
        font_config = temporary / "fonts.conf"
        ET.ElementTree(fonts).write(font_config, encoding="utf-8", xml_declaration=True)
        environment["FONTCONFIG_FILE"] = str(font_config)
    manifest = output / "manifest.json"
    records = json.loads(manifest.read_text()) if arguments.only and manifest.exists() else []
    for source in split_sections(directory):
        if arguments.only and source.stem != arguments.only:
            continue
        config_path = directory / "mermaid-config.json"
        viewport_width = 2400
        if source.stem == "00-complete-workflow":
            config = json.loads(config_path.read_text())
            config["flowchart"]["useMaxWidth"] = True
            config_path = temporary / "complete-config.json"
            config_path.write_text(json.dumps(config))
            viewport_width = 12000
        common = [
            str(Path(arguments.mmdc).resolve()), "-i", str(source),
            "-c", str(config_path),
            "-p", str(browser_config), "-b", "white",
            "-w", str(viewport_width), "-H", "1600",
        ]
        vector = temporary / f"{source.stem}.svg"
        subprocess.run(common + ["-o", str(vector)], check=True, env=environment, timeout=300)
        viewbox = ET.parse(vector).getroot().attrib["viewBox"].split()
        width, height = map(float, viewbox[2:])
        scale = min(
            arguments.scale, 30000 / max(width, height),
            math.sqrt(160_000_000 / (width * height)),
        )
        raster = output / f"{source.stem}.png"
        from PIL import Image

        dimensions = (math.ceil(width * scale), math.ceil(height * scale))
        tiles = temporary / f"{source.stem}-tiles"
        subprocess.run([
            "node", str(directory / "render-svg-tiles.mjs"),
            str(Path(arguments.mmdc).resolve()), str(vector), str(browser_config),
            str(tiles), str(dimensions[0]), str(dimensions[1]),
        ], check=True, env=environment, timeout=600)
        image = Image.new("RGB", dimensions, "white")
        for tile in json.loads((tiles / "tiles.json").read_text()):
            with Image.open(tiles / tile["filename"]) as part:
                image.paste(part, (tile["left"], tile["top"]))
        image.save(raster)
        image.close()
        with raster.open("rb") as stream:
            header = stream.read(24)
        if header[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError(f"Invalid PNG: {raster}")
        pixels = struct.unpack(">II", header[16:24])
        record = {
            "source": str(source.relative_to(directory)),
            "png": str(raster.relative_to(directory)),
            "width": pixels[0], "height": pixels[1],
            "scale": scale, "bytes": raster.stat().st_size,
        }
        records = [entry for entry in records if entry["source"] != record["source"]]
        records.append(record)
        print(json.dumps(record), flush=True)
    records.sort(key=lambda entry: entry["source"])
    manifest.write_text(json.dumps(records, indent=2) + "\n")


if __name__ == "__main__":
    main()
