# OrcaMCP

An MCP server that lets Claude (or any MCP client) slice with **your own OrcaSlicer, your own user presets, and any of your printers**.

> Ask Claude things like:
> - *"Slice this STL on my 0.6 nozzle with PETG and tell me grams and time."*
> - *"Which of my printers prints this fastest?"*
> - *"Switch to the Kobra and slice every STL in this folder."*

## Why another OrcaSlicer MCP?

- **Uses your own presets with stock OrcaSlicer.** No special build is needed.
- **Handles user presets that inherit from system presets.** In OrcaSlicer 2.5 the CLI can't load these on its own: it fails with *"Preset type could not be resolved"* or *"Preset was not found in the loaded bundle"*. OrcaMCP keeps a private working copy of the system bundles and your user presets, and refreshes it before every slice.
- **Works with several printers.** You choose the active printer once, and it is remembered. Processes and filaments are matched to that printer, and you can compare the same part across all your printers.
- **Safe.** It never writes to your OrcaSlicer folder and never overwrites files. Saved outputs get `_2`, `_3`… suffixes.
- **Built for production.** It can slice whole folders into a CSV, and it can clone a part N times to get per-copy figures.

## Tools

| Tool | What it does |
|---|---|
| `list_printers` | Lists your printers (user printer presets) with nozzle, base model, default presets, how many presets fit each one, and which one is **active**. |
| `set_printer` | Sets the active printer by name or part of it, e.g. `"kobra"` or `"0.6"`. It is remembered until you change it; `"auto"` goes back to the default. |
| `list_profiles` | Lists your printer, process and filament presets. With `printer`, it lists only the ones compatible with that printer. |
| `model_info` | Gives size, volume, facet count and whether the mesh is closed, without slicing. |
| `slice` | Slices one or more files on one plate and returns grams, time and the presets used. Options: `printer` (for this slice only), `process`, `filament`, `copies`, `auto_orient`, and `save_to` to keep the `.gcode` and `.gcode.3mf`. |
| `slice_folder` | Slices every model in a folder separately and writes `_orca_slicing.csv`. |
| `compare_printers` | Slices the same model on several (or all) printers and reports grams and time for each, plus which is fastest and which uses least material. |

You can name presets by fragments, e.g. `"0.20 balanced"`, `"petg"` or `"ad5m 0.6"`. When you don't name a process or filament, the printer's own defaults are used, or the first compatible user preset.

### How the printer is chosen

1. The `printer` argument of a call.
2. The printer set with `set_printer`.
3. The *Default printer* setting.
4. The user printer preset with the simplest name.

> In OrcaSlicer each nozzle is a separate printer preset. So "AD5M Pro 0.4" and "AD5M Pro 0.6" count as two printers here.

## Install

### Claude Desktop (recommended)

1. Download `orcamcp-<version>.mcpb` from [Releases](https://github.com/rexcacayo/orcamcp/releases).
2. In Claude Desktop, open *Settings → Extensions → Advanced settings → Install extension* and choose the file.
3. Leave the fields empty to auto-detect OrcaSlicer. Optionally, set a default printer.

You don't need to install Python yourself: the extension uses Claude Desktop's built-in `uv` runtime.

### Other MCP clients

```json
{
  "mcpServers": {
    "orca": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/rexcacayo/orcamcp", "orcamcp"]
    }
  }
}
```

### Requirements

- OrcaSlicer 2.x. Open it at least once so that its system presets exist in its data folder.
- At least one user printer preset.
- On headless Linux, `xvfb-run`. It is used automatically.

### Configuration

All settings are optional environment variables.

| Variable | Default |
|---|---|
| `ORCA_MCP_ORCA_EXE` | Auto-detected (Windows, macOS, Linux/AppImage). |
| `ORCA_MCP_ORCA_DATA` | `%APPDATA%\OrcaSlicer`, `~/Library/Application Support/OrcaSlicer` or `~/.config/OrcaSlicer`. |
| `ORCA_MCP_DEFAULT_PRINTER` | The user printer with the simplest name. |
| `ORCA_MCP_WORKDIR` | `%LOCALAPPDATA%\orca-mcp` or `~/.cache/orca-mcp`. Holds the private preset copy and the active printer. |
| `ORCA_MCP_TIMEOUT_MIN` | `30` |

Grams and time are OrcaSlicer's estimates.

## Development

```
uv run src/orca_mcp.py --test path/to/model.stl   # list presets, model info, slice
mcpb pack . orcamcp.mcpb                          # build the Claude Desktop extension
```

## Español

OrcaMCP es un servidor MCP para laminar desde Claude con **tu OrcaSlicer, tus perfiles de usuario y cualquiera de tus impresoras**. Puedes elegir la impresora activa, comparar la misma pieza en varias impresoras, laminar carpetas enteras a un CSV y guardar el G-code.

---

Tested with OrcaSlicer 2.5.1 on Windows and Linux. Made by Ricardo Lugaresi Studio (Tenerife, Canary Islands). MIT license.
