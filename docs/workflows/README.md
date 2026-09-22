# Jev Factorio workflows

High-resolution renders of the workflow diagram, based on source revision
`82a026d43fae46c524f64cba6f2fdb3475def398`.

The complete diagram preserves the eight sections from the conversation.
Each section also has its own image for easier reading. Orange dashed nodes
in section diagrams are connections to other sections, not additional logic.

| Diagram | Mermaid source | PNG |
| --- | --- | --- |
| Complete workflow | [Source](mmd/00-complete-workflow.mmd) | [Image](pngs/00-complete-workflow.png) |
| Campaign supervision | [Source](mmd/01-campaign-supervision.mmd) | [Image](pngs/01-campaign-supervision.png) |
| Hierarchical controller | [Source](mmd/02-hierarchical-controller.mmd) | [Image](pngs/02-hierarchical-controller.png) |
| Goals and bootstrap | [Source](mmd/03-goals-and-bootstrap.mmd) | [Image](pngs/03-goals-and-bootstrap.png) |
| Factory dependencies | [Source](mmd/04-factory-dependency-planner.mmd) | [Image](pngs/04-factory-dependency-planner.png) |
| Jev candidate judgment | [Source](mmd/05-jev-candidate-judgment.mmd) | [Image](pngs/05-jev-candidate-judgment.png) |
| Native execution | [Source](mmd/06-native-action-execution.mmd) | [Image](pngs/06-native-action-execution.png) |
| Pending verification | [Source](mmd/07-pending-action-verification.mmd) | [Image](pngs/07-pending-action-verification.png) |
| Automatic repair | [Source](mmd/08-automatic-codex-repair.mmd) | [Image](pngs/08-automatic-codex-repair.png) |

Jev ranks code-generated plans, rather than inventing actions or controlling
individual game ticks. The hybrid policy can choose a deterministic fallback.
The rocket campaign's milestone order is fuel, bootstrap mining, then rocket
launch; production and research dependencies are expanded recursively.

## Re-render

Requires Python 3 with Pillow, Node.js, Mermaid CLI 11.17.0, and a working Chromium installation.
No gameplay commands are executed by the renderer.

```sh
PUPPETEER_SKIP_DOWNLOAD=true npm install --prefix runs/mermaid-render \
  --no-audit --no-fund @mermaid-js/mermaid-cli@11.17.0
.venv/bin/python docs/workflows/render.py \
  --mmdc runs/mermaid-render/node_modules/.bin/mmdc \
  --browser /absolute/path/to/chromium
```

Edit `mmd/00-complete-workflow.mmd` as the source of truth. The renderer regenerates
the eight section sources, uses the shared ELK layout/theme configuration, and
writes white-background PNGs. Section images default to 2x scale; oversized
images are capped at 30,000 pixels per axis and approximately 160 megapixels.
Diagrams are captured in browser tiles and assembled
without resizing the tiles, avoiding browser clipping at large canvas sizes.
Actual dimensions, scales and file sizes are recorded in `pngs/manifest.json`.

If browser libraries are extracted locally under
`runs/mermaid-render/browser-libs`, the renderer detects their library and font
directories. Browser dependencies and temporary SVGs stay outside the docs tree.
